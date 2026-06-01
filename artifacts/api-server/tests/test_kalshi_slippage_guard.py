"""Tests for the pre-submit orderbook liquidity + slippage guard.

Covers:
  * Cache layer: snapshot/delta application, BUY/SELL × YES/NO walks,
    binary-book transform for taker buys, and bus-routing of WS frames.
  * Lifecycle integration: pass + reject paths, degraded-allow on cache
    miss, no-op without model edge, threshold=0 disable, and that the
    guard fires BEFORE the live KalshiClient is opened.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.kalshi import lifecycle
from app.kalshi import orderbook as kalshi_orderbook
from app.kalshi.bus import bus


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Same teardown as test_kalshi_lifecycle but also clears the orderbook cache."""
    lifecycle._resting.clear()
    lifecycle._kill._halted = False
    lifecycle._kill._reason = None
    lifecycle._kill._at = None
    lifecycle._resting_lock = asyncio.Lock()
    lifecycle._kill._lock = asyncio.Lock()
    bus._lock = asyncio.Lock()
    bus._subs.clear()

    # Re-create the cache singleton so its internal lock is bound to the
    # current loop on every test (matches the lifecycle's strategy).
    kalshi_orderbook.orderbook_cache = kalshi_orderbook.OrderbookCache()
    lifecycle._reset_orderbook_guard_state()

    written: list[dict[str, Any]] = []

    async def _fake_write_lifecycle(**kwargs):
        written.append(kwargs)
        return len(written)

    monkeypatch.setattr(lifecycle, "_write_lifecycle", _fake_write_lifecycle)
    yield written


def _settings(
    threshold_pct: float = 50.0,
    mode: str = "paper",
    *,
    rest_fallback: bool = False,
    max_age_secs: float = 5.0,
    allow_empty_side: bool = True,
    rest_full_limit: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        kalshi_api_key_id="kid",
        kalshi_private_key="pem",
        kalshi_api_base_url="https://demo-api.kalshi.co/trade-api/v2",
        trading_mode=mode,
        slippage_guard_max_edge_pct=threshold_pct,
        kalshi_orderbook_max_age_secs=max_age_secs,
        # Default off in tests so the cache-miss path stays degraded-allow
        # without making a real network call. Tests that exercise the
        # REST fallback opt in explicitly + monkeypatch KalshiClient.
        kalshi_orderbook_rest_fallback_enabled=rest_fallback,
        # Mirrors the production default (True): a book that exists but has
        # no liquidity on the requested side is allowed through as a resting
        # limit rather than hard-rejected. Reject-path tests opt out with
        # allow_empty_side=False.
        slippage_guard_allow_empty_side=allow_empty_side,
        # NOTE: defaults to False *in tests* (production default is True) so
        # the legacy slice/trim path stays exercised by the existing
        # slice-assertion tests.  The full-rest behaviour (don't trim a
        # self-protecting limit order on a thin book) is covered explicitly
        # by its own test which sets this True.
        slippage_guard_rest_full_limit=rest_full_limit,
    )


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

def test_orderbook_cache_apply_snapshot_and_delta():
    cache = kalshi_orderbook.OrderbookCache()

    async def go():
        await cache.apply_snapshot(
            "X-1",
            yes=[[40, 100], [41, 50]],
            no=[[55, 25]],
        )
        await cache.apply_delta("X-1", side="yes", price_cents=40, delta=10)
        await cache.apply_delta("X-1", side="yes", price_cents=40, delta=-2)
        return await cache.snapshot("X-1")

    snap = _run(go())
    assert snap == {"yes": {40: 108, 41: 50}, "no": {55: 25}}


def test_buy_yes_crosses_no_book_with_binary_transform():
    """BUY YES is a taker against NO bids. Best taker yes-price comes
    from the *highest* no_price (cheapest cost = 100 - top_no_bid).
    """
    cache = kalshi_orderbook.OrderbookCache()

    async def go():
        # Top of no-book at 65¢ (=> taker can BUY YES at 35¢) for 5 contracts;
        # then 60¢ (=> 40¢) for 100. Buying 12 yes walks from 65¢ to 60¢.
        await cache.apply_snapshot("X", yes=[[30, 999]], no=[[65, 5], [60, 100]])
        return await cache.worst_fill_price("X", action="buy", side="yes", count=12)

    worst, filled = _run(go())
    assert filled == 12
    # Worst (highest) yes-price paid = 100 - 60 = 40¢.
    assert worst == 40


def test_buy_no_crosses_yes_book_with_binary_transform():
    cache = kalshi_orderbook.OrderbookCache()

    async def go():
        # Top yes-bid at 70 (=> BUY NO at 30¢) for 4; then 50 (=> 50¢) for 100.
        await cache.apply_snapshot("X", yes=[[70, 4], [50, 100]], no=[[80, 999]])
        return await cache.worst_fill_price("X", action="buy", side="no", count=10)

    worst, filled = _run(go())
    assert filled == 10
    assert worst == 50  # 100 - 50


def test_sell_yes_hits_yes_book_no_transform():
    """SELL YES hits the standing YES bids. Worst (lowest) price received
    is the deepest bid we walked.
    """
    cache = kalshi_orderbook.OrderbookCache()

    async def go():
        await cache.apply_snapshot("X", yes=[[42, 3], [40, 100]], no=[[55, 1]])
        return await cache.worst_fill_price("X", action="sell", side="yes", count=10)

    worst, filled = _run(go())
    assert filled == 10
    assert worst == 40  # received as low as 40¢ on the deepest fill


def test_sell_no_hits_no_book_no_transform():
    cache = kalshi_orderbook.OrderbookCache()

    async def go():
        await cache.apply_snapshot("X", yes=[[40, 1]], no=[[60, 2], [55, 100]])
        return await cache.worst_fill_price("X", action="sell", side="no", count=10)

    worst, filled = _run(go())
    assert filled == 10
    assert worst == 55


def test_worst_fill_partial_when_book_too_thin():
    cache = kalshi_orderbook.OrderbookCache()

    async def go():
        await cache.apply_snapshot("X", yes=[[40, 1]], no=[[60, 3]])
        # BUY YES eats the no-book, only 3 contracts available there.
        return await cache.worst_fill_price("X", action="buy", side="yes", count=10)

    worst, filled = _run(go())
    assert filled == 3
    assert worst == 100 - 60


def test_worst_fill_unknown_book():
    cache = kalshi_orderbook.OrderbookCache()
    worst, filled = _run(
        cache.worst_fill_price("MISSING", action="buy", side="yes", count=1)
    )
    assert worst is None and filled == 0


def test_bus_consumer_routes_snapshot_and_delta(monkeypatch):
    """End-to-end: publish a Kalshi-shaped snapshot+delta and confirm the
    cache reflects them after the consumer drains the bus.
    """
    async def go():
        await kalshi_orderbook.start_orderbook_consumer()
        try:
            await bus.publish({
                "type": "kalshi_event",
                "msg": {
                    "type": "orderbook_snapshot",
                    "msg": {
                        "market_ticker": "X-WS",
                        "yes": [[30, 10], [31, 20]],
                        "no": [[60, 5]],
                    },
                },
            })
            await bus.publish({
                "type": "kalshi_event",
                "msg": {
                    "type": "orderbook_delta",
                    "msg": {
                        "market_ticker": "X-WS",
                        "side": "yes",
                        "price": 30,
                        "delta": 5,
                    },
                },
            })
            for _ in range(5):
                await asyncio.sleep(0)
            return await kalshi_orderbook.orderbook_cache.snapshot("X-WS")
        finally:
            await kalshi_orderbook.stop_orderbook_consumer()

    snap = _run(go())
    assert snap == {"yes": {30: 15, 31: 20}, "no": {60: 5}}


# ---------------------------------------------------------------------------
# Lifecycle integration — pass + reject paths
# ---------------------------------------------------------------------------

def test_slippage_guard_passes_with_sufficient_depth(monkeypatch, _reset):
    """Plenty of depth at our limit price: the guard does not fire and the
    paper order short-circuits to filled.

    BUY YES at 40¢ ⇒ taker needs no-bids at 60¢ (=100-40). Snapshot puts
    500 contracts of no-bid at exactly 60¢, so all 10 yes-buys land at
    40¢ — zero slippage.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-PASS", yes=[[40, 500]], no=[[60, 500]],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-PASS",
            side="yes",
            action="buy",
            count=10,
            limit_price_cents=40,
            client_order_id="pass-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"
    states = [w["event_type"] for w in _reset]
    assert "rejected" not in states
    assert states == ["submitted", "filled"]


def test_slippage_guard_slices_buy_when_full_size_pushes_past_edge(monkeypatch, _reset):
    """4¢ model edge, BUY YES limit=40¢. The no-book has 1 contract at
    60¢ (=> 1 fills at 40¢, 0¢ slippage) and 100 at 50¢ (=> 50¢ each, 10¢
    slip = 250% of edge). Slicing keeps only the 1 contract at 60¢.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-SLICE",
            yes=[[40, 1]],
            no=[[60, 1], [50, 100]],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-SLICE",
            side="yes",
            action="buy",
            count=10,
            limit_price_cents=40,
            client_order_id="slice-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"
    assert rec.filled_count == 1  # sliced from 10 → 1
    rejected = [w for w in _reset if w["event_type"] == "rejected"]
    assert rejected == []
    submitted = [w for w in _reset if w["event_type"] == "submitted"]
    assert len(submitted) == 1
    slice_md = submitted[0]["metadata"]["context"]["sliced_by_slippage_guard"]
    assert slice_md["original_count"] == 10
    assert slice_md["sliced_count"] == 1
    assert slice_md["limit_price_cents"] == 40
    assert slice_md["sliced_slippage_cents"] == 0
    assert slice_md["worst_acceptable_price_cents"] == 42  # 40 + 50% * 4¢
    # The submitted row must record the SLICED count, so dashboards
    # see "filled 1 of 10" rather than the original ask.
    assert submitted[0]["contracts"] == 1


def test_slippage_guard_slices_sell_when_book_too_low(monkeypatch, _reset):
    """SELL YES at limit 60¢. Yes-book bids: 60¢×1 then 40¢×100. Selling
    all 10 walks down to 40¢ (20¢ slip = 400% of 5¢ edge), but slicing
    to 1 contract clears at 60¢ exactly.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-SELL", yes=[[60, 1], [40, 100]], no=[[10, 100]],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-SELL", side="yes", action="sell", count=10,
            limit_price_cents=60, client_order_id="sell-slice-1",
            context={"model_edge_cents": 5.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"
    assert rec.filled_count == 1
    submitted = [w for w in _reset if w["event_type"] == "submitted"]
    slice_md = submitted[0]["metadata"]["context"]["sliced_by_slippage_guard"]
    assert slice_md["original_count"] == 10
    assert slice_md["sliced_count"] == 1
    assert slice_md["worst_acceptable_price_cents"] == 58  # 60 - 50% * 5¢ → ceil(57.5)
    assert slice_md["sliced_slippage_cents"] == 0


def test_slippage_guard_rests_full_size_for_limit_on_thin_book(monkeypatch, _reset):
    """Production default (slippage_guard_rest_full_limit=True): a thin book
    that would otherwise trim a 10-lot down to its 3-contract fillable slice
    instead rests the FULL 10 lots.

    A limit order is self-protecting — every fill is bounded by its own limit
    price, which already sits inside the edge-erosion budget — so trimming it
    needlessly shrinks a Kelly-sized order to a sub-dollar position on thin
    sports / tennis books.  The unfilled remainder simply rests.
    """
    monkeypatch.setattr(
        lifecycle,
        "get_settings",
        lambda: _settings(threshold_pct=50.0, rest_full_limit=True),
    )

    async def go():
        # Same thin book as the slice test (only 3 of 10 fillable), but the
        # full-rest flag is on, so NO slicing happens.
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-RESTFULL", yes=[[40, 999]], no=[[60, 3]],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-RESTFULL", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="restfull-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    # The order kept its full requested size — the guard never trimmed it.
    assert rec.request.count == 10
    submitted = [w for w in _reset if w["event_type"] == "submitted"]
    assert submitted, "a submitted lifecycle event must be written"
    # The guard did NOT attach slice metadata because it never trimmed.
    ctx = submitted[0]["metadata"]["context"]
    assert "sliced_by_slippage_guard" not in ctx


def test_slippage_guard_slices_when_book_lacks_depth(monkeypatch, _reset):
    """Book covers only 3 of 10 requested contracts and they're all at the
    top price. Spec wants a 3-contract slice instead of a silent skip.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-THIN", yes=[[40, 999]], no=[[60, 3]],  # buy yes -> no book has 3
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-THIN", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="thin-slice-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"
    assert rec.filled_count == 3
    submitted = [w for w in _reset if w["event_type"] == "submitted"]
    slice_md = submitted[0]["metadata"]["context"]["sliced_by_slippage_guard"]
    assert slice_md["original_count"] == 10
    assert slice_md["sliced_count"] == 3
    assert "sliced 3 of 10" in slice_md["detail"]


def test_slippage_guard_full_reject_when_no_acceptable_levels(monkeypatch, _reset):
    """Book exists but every level violates the worst-acceptable price:
    no slice possible, so reject (the spec's first test case).
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        # BUY YES at 40¢, edge=2¢ → max_slip=1¢ → ceiling=41¢. The no-book
        # has only one level at 30¢ (=> taker pays 70¢ ≫ 41¢). Slice = 0.
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-NOSLICE", yes=[[40, 999]], no=[[30, 100]],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-NOSLICE", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="noslice-1",
            context={"model_edge_cents": 2.0},
        )
        return await lifecycle.submit_order(req)

    with pytest.raises(lifecycle.OrderRejected, match="slippage guard"):
        _run(go())
    md = [w for w in _reset if w["event_type"] == "rejected"][0]["metadata"]
    assert md["reason"] == "slippage_guard"
    assert md["worst_acceptable_price_cents"] == 41
    assert "violates worst-acceptable price" in md["detail"]


def test_slippage_guard_full_reject_when_side_empty(monkeypatch, _reset):
    """Book is known but the requested side has zero liquidity at any
    price → no slice possible, reject — *with the empty-side allow flag
    turned off* (the legacy hard-reject behaviour operators can restore
    via AXIOM_SLIPPAGE_GUARD_ALLOW_EMPTY_SIDE=false).
    """
    monkeypatch.setattr(
        lifecycle,
        "get_settings",
        lambda: _settings(threshold_pct=50.0, allow_empty_side=False),
    )

    async def go():
        # BUY YES needs the no-book; we publish only yes-bids.
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-EMPTY", yes=[[40, 100]], no=[],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-EMPTY", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="empty-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    with pytest.raises(lifecycle.OrderRejected, match="slippage guard"):
        _run(go())
    md = [w for w in _reset if w["event_type"] == "rejected"][0]["metadata"]
    assert md["reason"] == "slippage_guard"
    assert md["filled_available"] == 0
    assert "no liquidity" in md["detail"]


def test_slippage_guard_allows_empty_side_when_flag_on(_reset):
    """The loosened default: a known book whose *requested side* is empty
    is allowed through (guard returns None) so the order can rest as a
    limit instead of hard-rejecting. This is what lets thin sports /
    tennis / weather books trade.
    """
    async def go():
        # BUY YES needs the no-book; publish only yes-bids → empty side.
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-ALLOW-EMPTY", yes=[[40, 100]], no=[],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-ALLOW-EMPTY", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="allow-empty-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle._check_slippage_guard(
            req, settings=_settings(threshold_pct=50.0, allow_empty_side=True),
        )

    assert _run(go()) is None


def test_slippage_guard_rejects_empty_side_when_flag_off(_reset):
    """With the flag off the guard restores the legacy hard-reject: it
    returns a metadata dict whose detail flags the empty requested side.
    """
    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-REJ-EMPTY", yes=[[40, 100]], no=[],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-REJ-EMPTY", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="rej-empty-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle._check_slippage_guard(
            req, settings=_settings(threshold_pct=50.0, allow_empty_side=False),
        )

    md = _run(go())
    assert md is not None
    assert md["filled_available"] == 0
    assert "no liquidity on requested side" in md["detail"]


def test_slippage_guard_empty_side_reject_survives_full_rest_flag(_reset):
    """Operator-control regression guard: the production-default full-rest
    path (``rest_full_limit=True``) must NOT override an explicit empty-side
    hard-reject (``allow_empty_side=False``).  An empty requested side reaches
    the full-rest branch with ``book_known=False``; the branch is gated on
    ``book_known`` so the order still hard-rejects instead of resting.
    """
    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-REJ-EMPTY-RFL", yes=[[40, 100]], no=[],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-REJ-EMPTY-RFL", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="rej-empty-rfl-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle._check_slippage_guard(
            req,
            settings=_settings(
                threshold_pct=50.0,
                allow_empty_side=False,
                rest_full_limit=True,
            ),
        )

    md = _run(go())
    assert md is not None
    assert md["filled_available"] == 0
    assert "no liquidity on requested side" in md["detail"]


def test_slippage_guard_empty_side_allow_default_when_setting_absent(_reset):
    """Defence-in-depth: a settings object that predates the flag (no
    ``slippage_guard_allow_empty_side`` attribute at all) must default to
    allow, matching the env-loader default, via the getattr fallback.
    """
    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-ALLOW-DEFAULT", yes=[[40, 100]], no=[],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-ALLOW-DEFAULT", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="allow-default-1",
            context={"model_edge_cents": 4.0},
        )
        legacy = _settings(threshold_pct=50.0)
        delattr(legacy, "slippage_guard_allow_empty_side")
        return await lifecycle._check_slippage_guard(req, settings=legacy)

    assert _run(go()) is None


def test_slippage_guard_skips_when_no_cached_book(monkeypatch, _reset):
    """No WS snapshot for the ticker: degraded-mode pass-through.

    We MUST NOT block trading just because the WS hasn't delivered an
    orderbook yet. The lifecycle proceeds and the paper order fills.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        req = lifecycle.OrderRequest(
            market_ticker="X-UNKNOWN", side="yes", action="buy", count=5,
            limit_price_cents=40, client_order_id="miss-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"
    rejected = [w for w in _reset if w["event_type"] == "rejected"]
    assert rejected == []


def test_slippage_guard_skips_when_no_model_edge(monkeypatch, _reset):
    """If the request doesn't carry a model edge, the guard cannot
    compute erosion %, so it stays out of the way.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-NOCTX", yes=[[40, 999]], no=[[60, 1], [10, 100]],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-NOCTX", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="noctx-1",
            context={},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"


def test_slippage_guard_disabled_by_zero_threshold(monkeypatch, _reset):
    """Threshold = 0 disables the guard entirely (operator escape hatch)."""
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=0.0))

    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-OFF", yes=[[40, 999]], no=[[60, 1], [10, 100]],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-OFF", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="off-1",
            context={"model_edge_cents": 1.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"


def test_slippage_guard_blocks_live_path_before_wire(monkeypatch, _reset):
    """In live mode the guard must fire BEFORE we open the KalshiClient.

    Uses an empty-side book with the allow flag off so the guard takes
    its hard-reject branch (the scenario this test guards against is a
    reject leaking past the guard into a live wire call).
    """
    monkeypatch.setattr(
        lifecycle,
        "get_settings",
        lambda: _settings(threshold_pct=50.0, mode="live", allow_empty_side=False),
    )

    class _Boom:
        async def __aenter__(self):  # pragma: no cover — should not be called
            raise AssertionError("KalshiClient must not be opened when guard rejects")

        async def __aexit__(self, *exc):  # pragma: no cover
            return None

    monkeypatch.setattr(lifecycle.kalshi_client, "KalshiClient", lambda **kw: _Boom())

    async def go():
        # No no-bids at all → BUY YES has zero acceptable depth so the
        # guard returns a full reject (slice_count == 0). The KalshiClient
        # must never be opened in that case.
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-LIVE", yes=[[40, 999]], no=[],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-LIVE", side="yes", action="buy", count=5,
            limit_price_cents=40, client_order_id="live-rej-1",
            context={"model_edge_cents": 2.0},
        )
        return await lifecycle.submit_order(req)

    with pytest.raises(lifecycle.OrderRejected, match="slippage guard"):
        _run(go())
    rej = [w for w in _reset if w["event_type"] == "rejected"]
    assert rej and rej[0]["metadata"]["reason"] == "slippage_guard"


def test_end_to_end_ws_frame_to_guard_decision(monkeypatch, _reset):
    """Integration: feed Kalshi-shaped WS frames through the bus →
    cache → lifecycle guard, and verify the guard decision matches
    what the cache projects. Catches any silent regression in the wiring
    between ws.py, bus, the consumer, and submit_order.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        await kalshi_orderbook.start_orderbook_consumer()
        try:
            # Snapshot: 1 acceptable contract at no=60 (taker yes-cost 40),
            # then 999 at no=30 (taker cost 70). Slicing keeps just the 1.
            await bus.publish({
                "type": "kalshi_event",
                "msg": {
                    "type": "orderbook_snapshot",
                    "msg": {
                        "market_ticker": "X-E2E",
                        "yes": [[40, 999]],
                        "no": [[60, 1], [30, 999]],
                    },
                },
            })
            for _ in range(5):
                await asyncio.sleep(0)
            req = lifecycle.OrderRequest(
                market_ticker="X-E2E", side="yes", action="buy", count=5,
                limit_price_cents=40, client_order_id="e2e-1",
                context={"model_edge_cents": 4.0},
            )
            return await lifecycle.submit_order(req)
        finally:
            await kalshi_orderbook.stop_orderbook_consumer()

    rec = _run(go())
    assert rec.state == "filled"
    assert rec.filled_count == 1
    submitted = [w for w in _reset if w["event_type"] == "submitted"]
    slice_md = submitted[0]["metadata"]["context"]["sliced_by_slippage_guard"]
    assert slice_md["original_count"] == 5
    assert slice_md["sliced_count"] == 1


# ---------------------------------------------------------------------------
# Cache freshness + REST fallback (task #148)
# ---------------------------------------------------------------------------

def test_orderbook_cache_records_last_update_timestamp():
    """Snapshots and deltas both bump the per-ticker timestamp so the
    freshness check has something to compare against.
    """
    cache = kalshi_orderbook.OrderbookCache()

    async def go():
        assert await cache.last_updated_at("X") is None
        await cache.apply_snapshot("X", yes=[[40, 1]], no=[[60, 1]])
        t1 = await cache.last_updated_at("X")
        assert t1 is not None
        await asyncio.sleep(0.01)
        await cache.apply_delta("X", side="yes", price_cents=40, delta=1)
        t2 = await cache.last_updated_at("X")
        return t1, t2

    t1, t2 = _run(go())
    assert t2 is not None and t2 >= t1


def test_orderbook_cache_is_fresh_window():
    cache = kalshi_orderbook.OrderbookCache()

    async def go():
        # Unknown ticker is never fresh.
        assert await cache.is_fresh("MISSING", 5.0) is False
        await cache.apply_snapshot("X", yes=[[40, 1]], no=[[60, 1]])
        # Just-applied is fresh under any positive window.
        assert await cache.is_fresh("X", 60.0) is True
        # max_age <= 0 disables the check (any cached snapshot counts).
        assert await cache.is_fresh("X", 0.0) is True
        # Pretend the snapshot is ancient by rewriting its timestamp.
        cache._updated_at["X"] = 0.0  # epoch
        assert await cache.is_fresh("X", 5.0) is False
        return True

    assert _run(go()) is True


class _FakeKalshiClient:
    """Stands in for `kalshi_client.KalshiClient` in fallback tests."""

    instances: list["_FakeKalshiClient"] = []

    def __init__(self, *, settings=None, response=None, raise_exc: Exception | None = None):
        self._response = response
        self._raise = raise_exc
        self.calls: list[str] = []
        _FakeKalshiClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_orderbook(self, ticker: str):
        self.calls.append(ticker)
        if self._raise is not None:
            raise self._raise
        return self._response


def test_slippage_guard_uses_rest_fallback_when_cache_missing(monkeypatch, _reset):
    """Cache empty + REST fallback enabled: lifecycle pulls a fresh
    orderbook over REST, applies it as a snapshot, and the guard runs
    on that data. Counter increments under `rest_fallback`.
    """
    monkeypatch.setattr(
        lifecycle, "get_settings",
        lambda: _settings(threshold_pct=50.0, rest_fallback=True),
    )

    rest_body = {"orderbook": {"yes": [[40, 999]], "no": [[60, 500]]}}
    _FakeKalshiClient.instances.clear()
    monkeypatch.setattr(
        lifecycle.kalshi_client, "KalshiClient",
        lambda **kw: _FakeKalshiClient(response=rest_body),
    )

    async def go():
        req = lifecycle.OrderRequest(
            market_ticker="X-FB", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="fb-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"
    assert _FakeKalshiClient.instances and _FakeKalshiClient.instances[0].calls == ["X-FB"]
    state = lifecycle.orderbook_guard_state()
    assert state["rest_fallback"] == 1
    assert state["cache_fresh"] == 0
    assert state["degraded_allow"] == 0
    assert state["last_source"] == "fallback"

    # The REST snapshot is now in-cache, so a second submit takes the cache path.
    async def go2():
        req = lifecycle.OrderRequest(
            market_ticker="X-FB", side="yes", action="buy", count=5,
            limit_price_cents=40, client_order_id="fb-2",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    _run(go2())
    state2 = lifecycle.orderbook_guard_state()
    assert state2["cache_fresh"] == 1
    assert state2["rest_fallback"] == 1


def test_slippage_guard_rest_fallback_failure_degraded_allows(monkeypatch, _reset):
    """REST fallback enabled but the call errors: degraded-allow path
    fires, the trade proceeds, and `rest_failed`/`degraded_allow`
    counters both go up.
    """
    monkeypatch.setattr(
        lifecycle, "get_settings",
        lambda: _settings(threshold_pct=50.0, rest_fallback=True),
    )

    _FakeKalshiClient.instances.clear()
    boom = RuntimeError("network down")
    monkeypatch.setattr(
        lifecycle.kalshi_client, "KalshiClient",
        lambda **kw: _FakeKalshiClient(raise_exc=boom),
    )

    async def go():
        req = lifecycle.OrderRequest(
            market_ticker="X-FB-FAIL", side="yes", action="buy", count=3,
            limit_price_cents=40, client_order_id="fb-fail-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"  # paper degraded-allow
    state = lifecycle.orderbook_guard_state()
    assert state["rest_failed"] == 1
    assert state["degraded_allow"] == 1
    assert state["rest_fallback"] == 0
    assert state["last_source"] == "degraded"
    assert state["last_error"] and "network down" in state["last_error"]


def test_slippage_guard_refreshes_stale_cache_via_rest(monkeypatch, _reset):
    """Cache has data but it's older than the configured max age:
    the guard bypasses the stale view and pulls fresh data via REST.

    The fresh REST book is too thin to fill the full requested size
    inside the slippage budget, so the guard slices the order down to
    the largest count that still fits (here: 1 contract at the limit
    price, 0¢ slippage).  Asserting on the slice metadata is the
    durable shape of "REST refresh happened and changed the answer".
    """
    monkeypatch.setattr(
        lifecycle, "get_settings",
        lambda: _settings(threshold_pct=50.0, rest_fallback=True, max_age_secs=1.0),
    )

    # REST returns a much thinner book than the (stale) cache.
    rest_body = {"orderbook": {"yes": [[40, 999]], "no": [[60, 1], [50, 100]]}}
    _FakeKalshiClient.instances.clear()
    monkeypatch.setattr(
        lifecycle.kalshi_client, "KalshiClient",
        lambda **kw: _FakeKalshiClient(response=rest_body),
    )

    submitted: list[dict[str, Any]] = []

    async def go():
        # Seed a deep, fresh-looking cache then backdate its timestamp
        # so the guard considers it stale.
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-STALE", yes=[[40, 999]], no=[[60, 999]],
        )
        kalshi_orderbook.orderbook_cache._updated_at["X-STALE"] = 0.0
        req = lifecycle.OrderRequest(
            market_ticker="X-STALE", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="stale-1",
            context={"model_edge_cents": 4.0},
        )
        async def _capture(req_, **kw):
            submitted.append({"req": req_, **kw})
        monkeypatch.setattr(lifecycle, "_record", _capture)
        return await lifecycle.submit_order(req)

    rec = _run(go())
    # Order went through but at a reduced size: the REST refresh forced
    # the guard to slice 10 → 1.
    assert rec.state == "filled"
    assert rec.filled_count == 1
    assert _FakeKalshiClient.instances and _FakeKalshiClient.instances[0].calls == ["X-STALE"]
    state = lifecycle.orderbook_guard_state()
    assert state["rest_fallback"] == 1
    assert state["cache_fresh"] == 0


def test_slippage_guard_degraded_allow_when_rest_disabled(monkeypatch, _reset):
    """Cache empty AND REST fallback disabled: degraded-allow, with the
    `degraded_allow` counter incremented and `last_source` set.
    """
    monkeypatch.setattr(
        lifecycle, "get_settings",
        lambda: _settings(threshold_pct=50.0, rest_fallback=False),
    )

    async def go():
        req = lifecycle.OrderRequest(
            market_ticker="X-NONE", side="yes", action="buy", count=5,
            limit_price_cents=40, client_order_id="none-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    rec = _run(go())
    assert rec.state == "filled"
    state = lifecycle.orderbook_guard_state()
    assert state["degraded_allow"] == 1
    assert state["rest_fallback"] == 0
    assert state["last_source"] == "degraded"
