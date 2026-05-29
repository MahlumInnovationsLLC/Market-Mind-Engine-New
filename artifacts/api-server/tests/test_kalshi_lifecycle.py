"""Unit tests for ``app.kalshi.lifecycle``: paper short-circuit, kill switch,
validation failures, and sequence-id (client_order_id) collisions on the wire.

The lifecycle module persists each transition by calling ``db.fetchval`` and
publishes events on an in-process bus. Tests stub the DB writer, drain the
bus into a list, and patch ``KalshiClient`` so no real httpx connection is
opened. No real Kalshi credentials are required.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.kalshi import lifecycle
from app.kalshi import orderbook as kalshi_orderbook
from app.kalshi.bus import bus


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _run(coro):
    # Each call gets a fresh loop. Earlier tests in the suite may have closed
    # the default loop, so don't rely on ``get_event_loop``.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _reset_lifecycle(monkeypatch):
    """Clear in-memory state and stub DB writes before every test."""
    # Drain the resting book.
    lifecycle._resting.clear()
    lifecycle._in_flight.clear()
    # Aggregate counters surfaced on /api/health/snapshot leak across
    # tests by design (they're module-level). Reset the dedupe counter so
    # assertions on its value start from a known-empty state.
    lifecycle._reset_submit_dedupe_state()
    # Reset the kill switch to a known clean state.
    lifecycle._kill._halted = False
    lifecycle._kill._reason = None
    lifecycle._kill._at = None
    # Re-bind asyncio primitives to the current loop. asyncio.Lock created in
    # one loop cannot be awaited from another, and our _run helper spins up a
    # fresh loop per call.
    lifecycle._resting_lock = asyncio.Lock()
    lifecycle._kill._lock = asyncio.Lock()
    bus._lock = asyncio.Lock()
    bus._subs.clear()

    # Reset the in-process orderbook cache so paper-fill tests don't leak
    # books across each other. The lifecycle paper branch consults this
    # cache via ``orderbook_cache.snapshot`` — leaving stale books from a
    # previous test would silently flip the new code path between the
    # cold-cache fallback and the honest-walk simulator.
    kalshi_orderbook.orderbook_cache._books = {}
    kalshi_orderbook.orderbook_cache._updated_at = {}
    kalshi_orderbook.orderbook_cache._lock = asyncio.Lock()

    # Capture every persisted lifecycle row instead of hitting Postgres.
    written: list[dict[str, Any]] = []

    async def _fake_write_lifecycle(**kwargs):
        written.append(kwargs)
        return len(written)

    monkeypatch.setattr(lifecycle, "_write_lifecycle", _fake_write_lifecycle)

    # The new partial-fill rollup helpers also touch Postgres — stub them.
    # Tests that need to exercise dedupe replays override `seen_fills`.
    # ``recorded_fills`` captures the full kwargs (including ``source``) so
    # paper-fill provenance assertions can introspect what was persisted.
    seen_fills: set[tuple[str, str]] = set()
    recorded_fills: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []

    async def _fake_record_fill(**kwargs):
        recorded_fills.append(kwargs)
        key = (kwargs["order_id"], kwargs["fill_id"])
        if key in seen_fills:
            return False
        seen_fills.add(key)
        return True

    async def _fake_upsert_position(**kwargs):
        positions.append(kwargs)

    monkeypatch.setattr(lifecycle, "_record_fill_idempotent", _fake_record_fill)
    monkeypatch.setattr(lifecycle, "_upsert_position_rollup", _fake_upsert_position)
    # Expose side-channel state on the module so tests can introspect.
    lifecycle._test_seen_fills = seen_fills           # type: ignore[attr-defined]
    lifecycle._test_recorded_fills = recorded_fills   # type: ignore[attr-defined]
    lifecycle._test_positions = positions             # type: ignore[attr-defined]

    # Best-effort: silence the alerts module so notifications don't blow up
    # without Stripe / SMTP envs.
    yield written


def _make_settings(trading_mode: str = "paper") -> SimpleNamespace:
    return SimpleNamespace(
        kalshi_api_key_id="kid",
        kalshi_private_key="pem",
        kalshi_api_base_url="https://demo-api.kalshi.co/trade-api/v2",
        trading_mode=trading_mode,
    )


async def _collect_during(coro):
    q = await bus.subscribe()
    try:
        try:
            result = await coro
        except Exception as e:
            result = e
        # Give the event loop a tick to let any pending publishes flush.
        await asyncio.sleep(0)
        msgs: list[dict[str, Any]] = []
        while not q.empty():
            msgs.append(q.get_nowait())
        return result, msgs
    finally:
        # Best-effort cleanup; bus has no public unsubscribe so we just drop the queue.
        bus._subs.discard(q)


# ---------------------------------------------------------------------------
# Paper short-circuit
# ---------------------------------------------------------------------------

def test_paper_order_short_circuits_to_filled(monkeypatch, _reset_lifecycle):
    """Cold orderbook cache (no book for the ticker) → simulator returns
    ``no_book`` → lifecycle falls back to the legacy limit-price fill,
    tagged ``source='paper_sim_no_book_fallback'`` for audit. This is
    the degraded-allow path; existing pre-#751 paper assertions should
    still hold so backtests / unit tests without a WS feed don't break.
    """
    written = _reset_lifecycle
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))

    req = lifecycle.OrderRequest(
        market_ticker="X-1",
        side="yes",
        action="buy",
        count=3,
        limit_price_cents=42,
        client_order_id="paper-1",
    )

    record, events = _run(_collect_during(lifecycle.submit_order(req)))

    assert record.state == "filled"
    assert record.paper is True
    assert record.filled_count == 3
    assert record.avg_fill_price_cents == 42
    # Two persisted rows: submitted, filled.
    states = [w["event_type"] for w in written]
    assert states == ["submitted", "filled"]
    # Both bus events fired in order.
    bus_events = [m["event"] for m in events if m.get("type") == "order_lifecycle"]
    assert bus_events == ["submitted", "filled"]
    # Provenance: order_fills row tagged as cold-cache fallback so the
    # 24h audit query in runbook §17 can split it from honest paper_sim.
    fills = lifecycle._test_recorded_fills  # type: ignore[attr-defined]
    assert len(fills) == 1
    assert fills[0]["source"] == "paper_sim_no_book_fallback"
    assert fills[0]["count"] == 3 and fills[0]["price_cents"] == 42


# ---------------------------------------------------------------------------
# Honest paper-fill path (Task #751): orderbook cache populated → simulator
# walks the half-book and returns realistic fill numbers, NOT the legacy
# 100%-at-limit shortcut.
# ---------------------------------------------------------------------------

def _seed_book(ticker: str, *, yes=None, no=None) -> None:
    """Populate the in-process orderbook cache directly. Bypasses the WS
    consumer because tests don't run a real Kalshi feed."""
    kalshi_orderbook.orderbook_cache._books[ticker] = {
        "yes": dict(yes or {}),
        "no":  dict(no  or {}),
    }
    import time as _t
    kalshi_orderbook.orderbook_cache._updated_at[ticker] = _t.time()


def test_paper_fill_uses_orderbook_walk_when_cache_is_warm(
    monkeypatch, _reset_lifecycle,
):
    """Warm cache → simulator walks the no-book and the persisted fill
    reflects the *VWAP*, not the limit price. ``order_fills.source``
    is ``'paper_sim'``, and the lifecycle metadata exposes the walked
    levels so operators can audit the simulation."""
    written = _reset_lifecycle
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))

    # BUY YES walks the no-book: best bid 45 (size 4) → taker pays 55¢;
    # next bid 40 (size 100) → taker pays 60¢. Buying 6 contracts:
    #   4 @ 55 + 2 @ 60 = 220 + 120 = 340; VWAP = (340 + 3) // 6 = 57.
    _seed_book("X-WARM", no={45: 4, 40: 100})

    req = lifecycle.OrderRequest(
        market_ticker="X-WARM", side="yes", action="buy", count=6,
        limit_price_cents=70, client_order_id="paper-warm-1",
    )
    record, _ = _run(_collect_during(lifecycle.submit_order(req)))

    assert record.state == "filled"
    assert record.filled_count == 6
    # VWAP, NOT the limit price (legacy code would have returned 70).
    assert record.avg_fill_price_cents == 57

    fills = lifecycle._test_recorded_fills  # type: ignore[attr-defined]
    assert len(fills) == 1
    assert fills[0]["source"] == "paper_sim"
    assert fills[0]["price_cents"] == 57

    filled_row = next(w for w in written if w["event_type"] == "filled")
    sim_meta = filled_row["metadata"]["paper_sim"]
    assert sim_meta["status"] == "filled"
    assert sim_meta["filled_qty"] == 6
    assert sim_meta["vwap_price_cents"] == 57
    assert sim_meta["residual_qty"] == 0
    assert sim_meta["levels_walked"] == [(55, 4), (60, 2)]
    assert sim_meta["book_present"] is True


def test_paper_partial_fill_records_residual_and_emits_filled(
    monkeypatch, _reset_lifecycle,
):
    """Limited depth at the simulator's limit → partial fill. The
    lifecycle still emits ``filled`` (not ``rejected``) because *some*
    quantity crossed; the residual lands in the metadata for the
    orchestrator's retry-next-tick logic."""
    _reset_lifecycle  # pin fixture
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))

    # no-bids: 40 (size 3) → taker 60¢; 30 (size 100) → taker 70¢.
    # Limit 65¢ → only the first level qualifies, fills 3 of 10.
    _seed_book("X-PART", no={40: 3, 30: 100})

    req = lifecycle.OrderRequest(
        market_ticker="X-PART", side="yes", action="buy", count=10,
        limit_price_cents=65, client_order_id="paper-part-1",
    )
    record, events = _run(_collect_during(lifecycle.submit_order(req)))

    assert record.state == "filled"
    assert record.filled_count == 3
    assert record.avg_fill_price_cents == 60

    fills = lifecycle._test_recorded_fills  # type: ignore[attr-defined]
    assert len(fills) == 1
    assert fills[0]["source"] == "paper_sim"
    assert fills[0]["count"] == 3

    bus_events = [m["event"] for m in events if m.get("type") == "order_lifecycle"]
    assert bus_events == ["submitted", "filled"]


def test_paper_no_liquidity_rejects_with_audit_reason(
    monkeypatch, _reset_lifecycle,
):
    """Book exists but the side a BUY YES needs (the no-book) is empty
    → simulator returns ``no_liquidity`` → lifecycle records a real
    rejection, NOT the cold-cache fallback. No fill row is written."""
    written = _reset_lifecycle
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))

    # yes-book has depth, no-book is empty → BUY YES has no liquidity.
    _seed_book("X-NOLIQ", yes={70: 100}, no={})

    req = lifecycle.OrderRequest(
        market_ticker="X-NOLIQ", side="yes", action="buy", count=5,
        limit_price_cents=80, client_order_id="paper-noliq-1",
    )
    record, events = _run(_collect_during(lifecycle.submit_order(req)))

    assert record.state == "rejected"
    assert record.filled_count == 0
    assert record.avg_fill_price_cents is None

    fills = lifecycle._test_recorded_fills  # type: ignore[attr-defined]
    assert fills == [], "no order_fills row should be written on a paper reject"

    states = [w["event_type"] for w in written]
    assert states == ["submitted", "rejected"]
    rejected_row = next(w for w in written if w["event_type"] == "rejected")
    assert rejected_row["metadata"]["reason"] == "paper_no_liquidity"
    assert rejected_row["metadata"]["paper_sim"]["status"] == "no_liquidity"

    bus_events = [m for m in events if m.get("type") == "order_lifecycle"]
    rejected_event = next(m for m in bus_events if m["event"] == "rejected")
    assert rejected_event["reason"] == "paper_no_liquidity"


def test_paper_limit_unfillable_rejects_when_book_too_expensive(
    monkeypatch, _reset_lifecycle,
):
    """Every bid in the cross-book violates the limit → reject with
    reason ``paper_limit_unfillable`` (distinct from ``no_liquidity``
    for triage)."""
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))

    # No-bids 30 (taker 70¢), 25 (taker 75¢). Limit 60¢ rejects both.
    _seed_book("X-LIMIT", no={30: 100, 25: 50})

    req = lifecycle.OrderRequest(
        market_ticker="X-LIMIT", side="yes", action="buy", count=5,
        limit_price_cents=60, client_order_id="paper-limit-1",
    )
    record, _ = _run(_collect_during(lifecycle.submit_order(req)))

    assert record.state == "rejected"
    assert record.filled_count == 0
    fills = lifecycle._test_recorded_fills  # type: ignore[attr-defined]
    assert fills == []


def test_force_paper_overrides_live_mode(monkeypatch, _reset_lifecycle):
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("live"))
    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="no", action="buy", count=1,
        limit_price_cents=20, client_order_id="paper-2",
    )
    record, _ = _run(_collect_during(lifecycle.submit_order(req, force_paper=True)))
    assert record.paper is True
    assert record.state == "filled"


# ---------------------------------------------------------------------------
# Operator override (Elite live-execute toggle): system_settings.risk.trading_mode
# beats the env var when ``force_paper`` isn't explicitly passed. We patch the
# DB-backed resolver so the resolution is deterministic without Postgres.
# ---------------------------------------------------------------------------

def _patch_trading_mode_override(monkeypatch, db_mode: str | None, env_mode: str = "paper"):
    """Stub ``app.risk.trading_mode.resolve_trading_mode`` to mirror what the
    real helper would return for the given DB row + env combination."""
    from app.risk import trading_mode as _tm

    async def _fake_resolve(env_default=None):
        if db_mode is not None:
            return db_mode
        return (env_default or env_mode).lower()

    monkeypatch.setattr(_tm, "resolve_trading_mode", _fake_resolve)
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings(env_mode))


def test_operator_override_live_routes_to_real_exchange(monkeypatch, _reset_lifecycle):
    """Override = 'live' + force_paper=None -> paper=False (real order path)."""
    _patch_trading_mode_override(monkeypatch, db_mode="live", env_mode="paper")

    placed: list[Any] = []

    class _FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def submit_order(self, payload):
            placed.append(payload)
            return {"order": {"order_id": "EX-1", "status": "resting"}}

    monkeypatch.setattr(lifecycle.kalshi_client, "KalshiClient", _FakeClient)

    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=33, client_order_id="live-1",
    )
    record, _ = _run(_collect_during(lifecycle.submit_order(req)))
    assert record.paper is False, "live override must produce a real (non-paper) order"
    assert placed, "expected the live exchange client to be invoked"


def test_operator_override_paper_keeps_paper(monkeypatch, _reset_lifecycle):
    """Override = 'paper' + force_paper=None -> paper=True even if env is live."""
    _patch_trading_mode_override(monkeypatch, db_mode="paper", env_mode="live")
    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=2,
        limit_price_cents=44, client_order_id="paper-3",
    )
    record, _ = _run(_collect_during(lifecycle.submit_order(req)))
    assert record.paper is True
    assert record.state == "filled"


def test_operator_override_absent_falls_back_to_env_paper(monkeypatch, _reset_lifecycle):
    """No override row + env=paper + force_paper=None -> paper=True."""
    _patch_trading_mode_override(monkeypatch, db_mode=None, env_mode="paper")
    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=12, client_order_id="paper-4",
    )
    record, _ = _run(_collect_during(lifecycle.submit_order(req)))
    assert record.paper is True
    assert record.state == "filled"


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

def test_kill_switch_rejects_orders(monkeypatch, _reset_lifecycle):
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))
    # Stub alert dispatch so halt() doesn't reach for SMTP / Postmark.
    _stub_real_alerts(monkeypatch)

    lifecycle.halt(reason="testing")
    assert lifecycle.is_halted() is True

    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=50, client_order_id="kill-1",
    )
    with pytest.raises(lifecycle.OrderRejected) as exc:
        _run(lifecycle.submit_order(req))
    assert "kill switch" in str(exc.value)

    lifecycle.resume()
    assert lifecycle.is_halted() is False


def test_kill_switch_state_round_trip(monkeypatch, _reset_lifecycle):
    _stub_real_alerts(monkeypatch)
    state = lifecycle.halt(reason="r1")
    assert state["halted"] is True and state["reason"] == "r1"
    assert lifecycle.kill_switch_state()["halted"] is True
    lifecycle.resume()
    assert lifecycle.kill_switch_state()["halted"] is False


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("count", [0, -3])
def test_submit_rejects_non_positive_count(monkeypatch, _reset_lifecycle, count):
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))
    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=count,
        limit_price_cents=50, client_order_id=f"bad-count-{count}",
    )
    with pytest.raises(lifecycle.OrderRejected, match="count"):
        _run(lifecycle.submit_order(req))
    rejected = [w for w in _reset_lifecycle if w["event_type"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["metadata"]["reason"] == "bad_count"


@pytest.mark.parametrize("price", [0, 100, -1, 200])
def test_submit_rejects_out_of_range_limit_price(monkeypatch, _reset_lifecycle, price):
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))
    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=price, client_order_id=f"bad-price-{price}",
    )
    with pytest.raises(lifecycle.OrderRejected, match="1..99"):
        _run(lifecycle.submit_order(req))
    rejected = [w for w in _reset_lifecycle if w["event_type"] == "rejected"]
    assert rejected[-1]["metadata"]["reason"] == "bad_price"


def test_to_kalshi_payload_requires_limit_price():
    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=None, type="limit", client_order_id="np",
    )
    with pytest.raises(ValueError, match="limit_price_cents"):
        req.to_kalshi_payload()


def test_to_kalshi_payload_routes_yes_vs_no_price():
    yes = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=2,
        limit_price_cents=33, client_order_id="y",
    ).to_kalshi_payload()
    no = lifecycle.OrderRequest(
        market_ticker="X-1", side="no", action="sell", count=2,
        limit_price_cents=66, client_order_id="n",
    ).to_kalshi_payload()
    assert yes["yes_price"] == 33 and "no_price" not in yes
    assert no["no_price"] == 66 and "yes_price" not in no


# ---------------------------------------------------------------------------
# Live path + sequence-id (client_order_id) collisions
# ---------------------------------------------------------------------------

class _FakeKalshiClient:
    """Stand-in for ``KalshiClient`` used in live-path tests."""

    def __init__(self, *, ack=None, raises: Exception | None = None) -> None:
        self.ack = ack or {"order": {"order_id": "exo-xyz"}}
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def submit_order(self, payload):
        self.calls.append(payload)
        if self.raises is not None:
            raise self.raises
        return self.ack

    async def cancel_order(self, order_id):
        self.calls.append({"cancel": order_id})
        return {"ok": True}


def test_live_order_records_resting(monkeypatch, _reset_lifecycle):
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("live"))
    fake = _FakeKalshiClient(ack={"order": {"order_id": "exo-1"}})
    monkeypatch.setattr(lifecycle.kalshi_client, "KalshiClient", lambda **kw: fake)

    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=2,
        limit_price_cents=55, client_order_id="live-1",
    )
    record, events = _run(_collect_during(lifecycle.submit_order(req)))

    assert record.state == "resting"
    assert record.exchange_order_id == "exo-1"
    assert "live-1" in lifecycle._resting
    assert fake.calls == [req.to_kalshi_payload()]
    states = [w["event_type"] for w in _reset_lifecycle]
    assert states == ["submitted", "resting"]
    bus_events = [m["event"] for m in events if m.get("type") == "order_lifecycle"]
    assert bus_events == ["submitted", "resting"]


def test_live_sequence_id_collision_records_rejection(monkeypatch, _reset_lifecycle):
    """When Kalshi rejects a duplicate ``client_order_id`` with a 4xx, the
    lifecycle must persist a ``rejected`` row, emit on the bus, raise
    ``OrderRejected``, and leave nothing resting in memory.
    """
    from app.kalshi import client as kc

    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("live"))
    err = kc.KalshiAPIError(409, {"error": "duplicate client_order_id"})
    fake = _FakeKalshiClient(raises=err)
    monkeypatch.setattr(lifecycle.kalshi_client, "KalshiClient", lambda **kw: fake)

    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=33, client_order_id="dup-1",
    )
    result, events = _run(_collect_during(lifecycle.submit_order(req)))

    assert isinstance(result, lifecycle.OrderRejected)
    assert "Kalshi rejected order" in str(result)
    assert "dup-1" not in lifecycle._resting

    rejected = [w for w in _reset_lifecycle if w["event_type"] == "rejected"]
    assert len(rejected) == 1
    md = rejected[0]["metadata"]
    assert md["reason"] == "exchange_reject"
    assert "duplicate client_order_id" in md["error"]

    bus_events = [m["event"] for m in events if m.get("type") == "order_lifecycle"]
    assert bus_events == ["submitted", "rejected"]


def test_live_collision_against_in_memory_resting(monkeypatch, _reset_lifecycle):
    """A second submission re-uses an already-resting client_order_id.

    `submit_order` now catches the collision in-process — before signing any
    REST call — and writes a `rejected` row with reason
    `duplicate_client_order_id`. The KalshiClient must NOT be constructed
    a second time.
    """
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("live"))

    # Only the first submission should reach the wire. Provide exactly one
    # client; iterator exhaustion would raise StopIteration if the dedupe
    # gate let the second submission through.
    accept = _FakeKalshiClient(ack={"order": {"order_id": "exo-A"}})
    seq = iter([accept])
    monkeypatch.setattr(lifecycle.kalshi_client, "KalshiClient", lambda **kw: next(seq))

    req1 = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=50, client_order_id="coll-1",
    )
    req2 = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=50, client_order_id="coll-1",
    )
    rec1 = _run(lifecycle.submit_order(req1))
    assert rec1.state == "resting"
    assert "coll-1" in lifecycle._resting

    with pytest.raises(lifecycle.OrderRejected, match="duplicate client_order_id"):
        _run(lifecycle.submit_order(req2))

    # The first order remains resting; the rejected dup did not displace it.
    assert "coll-1" in lifecycle._resting
    assert lifecycle._resting["coll-1"].exchange_order_id == "exo-A"

    # A `rejected` lifecycle row was written with the dedupe reason, and no
    # second `submitted` row hit the audit trail (no wire call attempted).
    rejects = [
        w for w in _reset_lifecycle
        if w["event_type"] == "rejected"
        and (w.get("metadata") or {}).get("reason") == "duplicate_client_order_id"
    ]
    assert len(rejects) == 1
    submitted = [w for w in _reset_lifecycle if w["event_type"] == "submitted"]
    assert len(submitted) == 1

    # The aggregate dedupe-rejection counter (surfaced on
    # /api/health/snapshot) ticks up exactly once per blocked submission
    # and remembers the offending client_order_id + when it last fired.
    state = lifecycle.submit_dedupe_state()
    assert state["rejected"] == 1
    assert state["last_client_order_id"] == "coll-1"
    assert isinstance(state["last_rejected_at"], float)
    assert state["last_rejected_at"] > 0


def test_submit_dedupe_counter_accumulates_across_rejections(
    monkeypatch, _reset_lifecycle,
):
    """Each blocked duplicate ticks the counter and refreshes the last-id stamp."""
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("live"))
    lifecycle._reset_submit_dedupe_state()

    accept = _FakeKalshiClient(ack={"order": {"order_id": "exo-Z"}})
    seq = iter([accept])
    monkeypatch.setattr(lifecycle.kalshi_client, "KalshiClient", lambda **kw: next(seq))

    base = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=50, client_order_id="dedupe-1",
    )
    rec = _run(lifecycle.submit_order(base))
    assert rec.state == "resting"

    # Three subsequent attempts all hit the dedupe gate. The first reuses
    # the resting id; the next two reuse it again. None of them should
    # construct another KalshiClient (the iter is exhausted) — so if any
    # leaked through, StopIteration would surface here.
    for _ in range(3):
        with pytest.raises(lifecycle.OrderRejected, match="duplicate client_order_id"):
            _run(lifecycle.submit_order(
                lifecycle.OrderRequest(
                    market_ticker="X-1", side="yes", action="buy", count=1,
                    limit_price_cents=50, client_order_id="dedupe-1",
                )
            ))

    state = lifecycle.submit_dedupe_state()
    assert state["rejected"] == 3
    assert state["last_client_order_id"] == "dedupe-1"

    # A non-colliding submission must NOT bump the counter.
    other = lifecycle.OrderRequest(
        market_ticker="X-2", side="yes", action="buy", count=1,
        limit_price_cents=50, client_order_id="dedupe-other",
    )
    # Force-paper to short-circuit before any wire call (no fake client queued).
    _run(lifecycle.submit_order(other, force_paper=True))
    assert lifecycle.submit_dedupe_state()["rejected"] == 3


def test_submit_dedupe_counter_starts_clean():
    """The reset helper zeroes every field for deterministic test runs."""
    lifecycle._submit_dedupe.update(
        {"rejected": 7, "last_rejected_at": 123.0, "last_client_order_id": "x"}
    )
    lifecycle._reset_submit_dedupe_state()
    assert lifecycle.submit_dedupe_state() == {
        "rejected": 0,
        "last_rejected_at": None,
        "last_client_order_id": None,
    }


# ---------------------------------------------------------------------------
# Cancel + WS fill routing
# ---------------------------------------------------------------------------

def test_cancel_resting_paper_order(monkeypatch, _reset_lifecycle):
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))
    # Manually seed a resting paper record (paper orders normally short-circuit).
    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=1,
        limit_price_cents=50, client_order_id="rest-1",
    )
    rec = lifecycle.OrderRecord(order_id="rest-1", request=req, paper=True, state="resting")
    lifecycle._resting["rest-1"] = rec

    out = _run(lifecycle.cancel_order("rest-1"))
    assert out["state"] == "canceled"
    assert "rest-1" not in lifecycle._resting


def test_cancel_unknown_raises():
    with pytest.raises(lifecycle.OrderRejected, match="no resting order"):
        _run(lifecycle.cancel_order("ghost"))


def test_handle_ws_fill_marks_resting_filled(monkeypatch, _reset_lifecycle):
    _stub_real_alerts(monkeypatch)
    req = lifecycle.OrderRequest(
        market_ticker="X-1", side="yes", action="buy", count=2,
        limit_price_cents=44, client_order_id="ws-1",
    )
    lifecycle._resting["ws-1"] = lifecycle.OrderRecord(
        order_id="ws-1", request=req, paper=False, state="resting",
        exchange_order_id="exo-ws-1",
    )
    msg = {
        "type": "kalshi_event",
        "msg": {
            "type": "fill",
            "msg": {"client_order_id": "ws-1", "count": 2, "yes_price": 47, "trade_id": "t1"},
        },
    }
    _run(lifecycle.handle_ws_fill(msg))
    assert "ws-1" not in lifecycle._resting
    filled = [w for w in _reset_lifecycle if w["event_type"] == "filled"]
    assert filled and filled[0]["price_cents"] == 47


# ---------------------------------------------------------------------------
# GTC partial-fill rollup (task #103)
# ---------------------------------------------------------------------------

def _stub_alerts(monkeypatch):
    _stub_real_alerts(monkeypatch)


async def _async_true(*a, **kw):
    return True


def _stub_real_alerts(monkeypatch):
    """Patch attributes on the *real* ``app.notifications.alerts`` module.

    The lifecycle module reaches the alerts dispatch via
    ``from ..notifications import alerts as _alerts`` inside function
    bodies. Per Python's ``from X import Y`` semantics, that lookup
    resolves through the parent package's *attribute* (set when
    ``app.notifications`` was first imported), NOT through
    ``sys.modules``. Replacing the entry in ``sys.modules`` is therefore
    a no-op for those imports — the real ``live_trade`` would still run
    and schedule a Postmark ``send`` background task. When the test's
    short-lived event loop closes (we spin a fresh one per ``_run``),
    the unstarted ``_send_one`` coroutines inside ``asyncio.gather``
    are garbage-collected without ever being awaited, polluting the
    pytest run with ``RuntimeWarning: coroutine '_send_one' was never
    awaited`` (task #265).

    Patching the attributes directly on the real module routes both
    ``import`` styles through our stubs and stops the leak at the
    source.
    """
    from app.notifications import alerts as _alerts

    monkeypatch.setattr(_alerts, "halt", lambda **kw: None, raising=False)
    monkeypatch.setattr(_alerts, "live_trade", lambda **kw: None, raising=False)
    monkeypatch.setattr(_alerts, "trade_confirmation", lambda **kw: None,
                        raising=False)
    monkeypatch.setattr(_alerts, "dedupe_or_skip", _async_true, raising=False)


def _seed_resting(client_order_id: str, *, count: int, side="yes", price=50):
    req = lifecycle.OrderRequest(
        market_ticker="MKT-1", side=side, action="buy", count=count,
        limit_price_cents=price, client_order_id=client_order_id,
    )
    lifecycle._resting[client_order_id] = lifecycle.OrderRecord(
        order_id=client_order_id, request=req, paper=False, state="resting",
        exchange_order_id=f"exo-{client_order_id}",
    )


def _ws(coid: str, count: int, price: int, trade_id: str) -> dict[str, Any]:
    return {
        "type": "kalshi_event",
        "msg": {
            "type": "fill",
            "msg": {"client_order_id": coid, "count": count,
                    "yes_price": price, "trade_id": trade_id},
        },
    }


def test_partial_fills_roll_up_with_weighted_average(monkeypatch, _reset_lifecycle):
    """Three partials (3@50, 4@60, 3@70) for a 10-contract GTC order roll
    up to one position with quantity=10 and weighted-average price 60¢."""
    _stub_alerts(monkeypatch)
    _seed_resting("gtc-1", count=10)

    async def go():
        await lifecycle.handle_ws_fill(_ws("gtc-1", 3, 50, "t1"))
        await lifecycle.handle_ws_fill(_ws("gtc-1", 4, 60, "t2"))
        await lifecycle.handle_ws_fill(_ws("gtc-1", 3, 70, "t3"))

    _run(go())

    # Order is fully filled and removed from the resting book.
    assert "gtc-1" not in lifecycle._resting
    # Three audit rows in order_lifecycle, one per partial.
    filled = [w for w in _reset_lifecycle if w["event_type"] == "filled"]
    assert len(filled) == 3

    # Exactly one rolled-up position upsert PER fill, but each upsert
    # carries the running rollup. The FINAL upsert is what the PnL
    # pipeline observes.
    positions = lifecycle._test_positions
    assert len(positions) == 3
    final = positions[-1]
    assert final["order_id"] == "gtc-1"
    assert final["market_id"] == "MKT-1"
    assert final["side"] == "yes"
    assert final["quantity"] == 10
    # Weighted avg: (3*50 + 4*60 + 3*70) / 10 = (150+240+210)/10 = 60
    assert final["avg_price_cents"] == 60
    assert final["over_filled"] is False


def test_overfill_partials_warn_and_do_not_double_count(monkeypatch, _reset_lifecycle, caplog):
    """3 partials totalling > requested size: capped at requested, flag set,
    warning logged, position quantity does NOT exceed the order size."""
    _stub_alerts(monkeypatch)
    _seed_resting("gtc-of", count=5)

    async def go():
        await lifecycle.handle_ws_fill(_ws("gtc-of", 2, 40, "t1"))
        await lifecycle.handle_ws_fill(_ws("gtc-of", 2, 50, "t2"))
        # Third partial pushes over: 2+2+3=7 > 5. Only 1 should be accepted.
        await lifecycle.handle_ws_fill(_ws("gtc-of", 3, 90, "t3"))

    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="app.kalshi.lifecycle"):
        _run(go())

    final = lifecycle._test_positions[-1]
    assert final["quantity"] == 5
    assert final["over_filled"] is True
    # Weighted avg counts the ACCEPTED slice of the third fill (1 contract,
    # not 3): (2*40 + 2*50 + 1*90) / 5 = (80+100+90)/5 = 54
    assert final["avg_price_cents"] == 54
    # Warning surfaced for the over-fill event.
    assert any("over-fill" in r.getMessage() for r in caplog.records)
    # Resting record is gone (treated as fully filled at the cap).
    assert "gtc-of" not in lifecycle._resting


def test_duplicate_fill_event_is_suppressed(monkeypatch, _reset_lifecycle):
    """A re-delivered partial (same trade_id) must not double-count the
    rollup or write a second order_lifecycle row."""
    _stub_alerts(monkeypatch)
    _seed_resting("gtc-dup", count=4)

    async def go():
        await lifecycle.handle_ws_fill(_ws("gtc-dup", 2, 40, "t1"))
        # Exact replay of the same fill — should be a no-op.
        await lifecycle.handle_ws_fill(_ws("gtc-dup", 2, 40, "t1"))
        await lifecycle.handle_ws_fill(_ws("gtc-dup", 2, 60, "t2"))

    _run(go())

    final = lifecycle._test_positions[-1]
    assert final["quantity"] == 4
    # Weighted avg over the two REAL fills: (2*40 + 2*60)/4 = 50
    assert final["avg_price_cents"] == 50
    # Only two filled audit rows (the replay was suppressed).
    filled = [w for w in _reset_lifecycle if w["event_type"] == "filled"]
    assert len(filled) == 2
    # Position upsert also fired only twice.
    assert len(lifecycle._test_positions) == 2


# ---------------------------------------------------------------------------
# Kalshi time-in-force payload mapping (live-trading canary fix, Task #952).
# Kalshi's POST /portfolio/orders 400s on a literal "GTC"/"IOC" time_in_force
# ("oneof" validation): resting GTC is the default (omit the field) and the
# only explicit accepted value is "fill_or_kill".
# ---------------------------------------------------------------------------

def _payload_req(tif: str) -> "lifecycle.OrderRequest":
    return lifecycle.OrderRequest(
        market_ticker="X-1",
        side="yes",
        action="buy",
        count=1,
        type="limit",
        limit_price_cents=68,
        client_order_id="tif-1",
        time_in_force=tif,
    )


def test_to_kalshi_payload_omits_time_in_force_for_gtc():
    payload = _payload_req("GTC").to_kalshi_payload()
    assert "time_in_force" not in payload  # resting default == omit


def test_to_kalshi_payload_maps_fok_to_fill_or_kill():
    payload = _payload_req("FOK").to_kalshi_payload()
    assert payload["time_in_force"] == "fill_or_kill"


def test_to_kalshi_payload_rejects_ioc():
    # IOC has no Kalshi equivalent — reject rather than silently degrade to
    # a resting GTC order on the live book.
    with pytest.raises(ValueError):
        _payload_req("IOC").to_kalshi_payload()
