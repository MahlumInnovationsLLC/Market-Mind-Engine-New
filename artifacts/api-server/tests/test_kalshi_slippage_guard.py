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

    written: list[dict[str, Any]] = []

    async def _fake_write_lifecycle(**kwargs):
        written.append(kwargs)
        return len(written)

    monkeypatch.setattr(lifecycle, "_write_lifecycle", _fake_write_lifecycle)
    yield written


def _settings(threshold_pct: float = 50.0, mode: str = "paper") -> SimpleNamespace:
    return SimpleNamespace(
        kalshi_api_key_id="kid",
        kalshi_private_key="pem",
        kalshi_api_base_url="https://demo-api.kalshi.co/trade-api/v2",
        trading_mode=mode,
        slippage_guard_max_edge_pct=threshold_pct,
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


def test_slippage_guard_rejects_when_book_pushes_past_edge(monkeypatch, _reset):
    """4¢ model edge, BUY YES limit=40¢. The no-book has 1 contract at
    60¢ (=> first 1 fills at 40¢) and the rest at 50¢ (=> 9 fills at 50¢).
    Worst yes-price paid = 50¢ ⇒ 10¢ slippage = 250% of edge ⇒ rejected.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-REJ",
            yes=[[40, 1]],
            no=[[60, 1], [50, 100]],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-REJ",
            side="yes",
            action="buy",
            count=10,
            limit_price_cents=40,
            client_order_id="rej-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    with pytest.raises(lifecycle.OrderRejected, match="slippage guard"):
        _run(go())

    rejected = [w for w in _reset if w["event_type"] == "rejected"]
    assert len(rejected) == 1
    md = rejected[0]["metadata"]
    assert md["reason"] == "slippage_guard"
    assert md["worst_fill_price_cents"] == 50
    assert md["limit_price_cents"] == 40
    assert md["slippage_cents"] == 10
    assert md["model_edge_cents"] == 4.0
    assert md["edge_erased_pct"] > 50.0
    assert md["filled_available"] == 10


def test_slippage_guard_rejects_sell_when_book_too_low(monkeypatch, _reset):
    """SELL YES at limit 60¢. Yes-book bids: 60¢×1 then 40¢×100.
    Worst received = 40¢ ⇒ 20¢ slippage ⇒ rejected.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-SELL", yes=[[60, 1], [40, 100]], no=[[10, 100]],
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-SELL", side="yes", action="sell", count=10,
            limit_price_cents=60, client_order_id="sell-rej-1",
            context={"model_edge_cents": 5.0},
        )
        return await lifecycle.submit_order(req)

    with pytest.raises(lifecycle.OrderRejected, match="slippage guard"):
        _run(go())
    md = [w for w in _reset if w["event_type"] == "rejected"][0]["metadata"]
    assert md["worst_fill_price_cents"] == 40
    assert md["limit_price_cents"] == 60
    assert md["slippage_cents"] == 20


def test_slippage_guard_rejects_when_book_lacks_depth(monkeypatch, _reset):
    """Book covers only 3 of the 10 requested contracts: liquidity reject."""
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0))

    async def go():
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-THIN", yes=[[40, 999]], no=[[60, 3]],  # buy yes -> no book has 3
        )
        req = lifecycle.OrderRequest(
            market_ticker="X-THIN", side="yes", action="buy", count=10,
            limit_price_cents=40, client_order_id="thin-1",
            context={"model_edge_cents": 4.0},
        )
        return await lifecycle.submit_order(req)

    with pytest.raises(lifecycle.OrderRejected, match="slippage guard"):
        _run(go())
    md = [w for w in _reset if w["event_type"] == "rejected"][0]["metadata"]
    assert md["reason"] == "slippage_guard"
    assert md["filled_available"] == 3
    assert "insufficient depth" in md["detail"]


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
    """In live mode the guard must fire BEFORE we open the KalshiClient."""
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _settings(threshold_pct=50.0, mode="live"))

    class _Boom:
        async def __aenter__(self):  # pragma: no cover — should not be called
            raise AssertionError("KalshiClient must not be opened when guard rejects")

        async def __aexit__(self, *exc):  # pragma: no cover
            return None

    monkeypatch.setattr(lifecycle.kalshi_client, "KalshiClient", lambda **kw: _Boom())

    async def go():
        # BUY YES at 40 needs 60¢ no-bids. We supply 1 at 60 then 100 at 20
        # ⇒ worst yes-cost = 80 ⇒ 40¢ slippage on a 2¢ edge = 2000% erased.
        await kalshi_orderbook.orderbook_cache.apply_snapshot(
            "X-LIVE", yes=[[40, 999]], no=[[60, 1], [20, 100]],
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
            # Snapshot makes BUY YES at 40¢ require no-bids at >=60¢. Only
            # 1 contract at 60¢; the rest at 30¢ (worst yes-cost = 70¢).
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
            try:
                await lifecycle.submit_order(req)
                return None
            except lifecycle.OrderRejected as e:
                return str(e)
        finally:
            await kalshi_orderbook.stop_orderbook_consumer()

    err = _run(go())
    assert err is not None and "slippage guard" in err
    md = [w for w in _reset if w["event_type"] == "rejected"][0]["metadata"]
    assert md["reason"] == "slippage_guard"
    assert md["worst_fill_price_cents"] == 70  # 100 - 30
