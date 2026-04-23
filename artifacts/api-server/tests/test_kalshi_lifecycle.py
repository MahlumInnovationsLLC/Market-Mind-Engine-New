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

    # Capture every persisted lifecycle row instead of hitting Postgres.
    written: list[dict[str, Any]] = []

    async def _fake_write_lifecycle(**kwargs):
        written.append(kwargs)
        return len(written)

    monkeypatch.setattr(lifecycle, "_write_lifecycle", _fake_write_lifecycle)

    # The new partial-fill rollup helpers also touch Postgres — stub them.
    # Tests that need to exercise dedupe replays override `seen_fills`.
    seen_fills: set[tuple[str, str]] = set()
    positions: list[dict[str, Any]] = []

    async def _fake_record_fill(**kwargs):
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
    lifecycle._test_seen_fills = seen_fills      # type: ignore[attr-defined]
    lifecycle._test_positions = positions        # type: ignore[attr-defined]

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
# Kill switch
# ---------------------------------------------------------------------------

def test_kill_switch_rejects_orders(monkeypatch, _reset_lifecycle):
    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("paper"))
    # Stub alert dispatch so halt() doesn't reach for SMTP.
    import sys
    fake_alerts = SimpleNamespace(halt=lambda **kw: None,
                                  live_trade=lambda **kw: None)
    monkeypatch.setitem(sys.modules, "app.notifications.alerts", fake_alerts)

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
    import sys
    monkeypatch.setitem(sys.modules, "app.notifications.alerts",
                        SimpleNamespace(halt=lambda **kw: None,
                                        live_trade=lambda **kw: None))
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
    import sys
    monkeypatch.setitem(sys.modules, "app.notifications.alerts",
                        SimpleNamespace(halt=lambda **kw: None,
                                        live_trade=lambda **kw: None))
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
    import sys
    monkeypatch.setitem(sys.modules, "app.notifications.alerts",
                        SimpleNamespace(halt=lambda **kw: None,
                                        live_trade=lambda **kw: None,
                                        _hash_reason=lambda s: f"h{abs(hash(s)) % 10000}",
                                        dedupe_or_skip=_async_true))


async def _async_true(*a, **kw):
    return True


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
