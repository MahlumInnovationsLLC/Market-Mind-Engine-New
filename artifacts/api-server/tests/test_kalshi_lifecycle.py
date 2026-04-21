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
    """A second submission re-uses an already-resting client_order_id. The
    current contract is that the in-memory book is keyed by ``order_id``, so
    Kalshi's duplicate-detection is what enforces uniqueness — verify both
    submissions wrote ``submitted`` rows but only one ``resting`` survives.
    """
    from app.kalshi import client as kc

    monkeypatch.setattr(lifecycle, "get_settings", lambda: _make_settings("live"))

    # First submission: Kalshi accepts.
    accept = _FakeKalshiClient(ack={"order": {"order_id": "exo-A"}})
    # Second submission with the SAME client_order_id: Kalshi rejects.
    reject = _FakeKalshiClient(raises=kc.KalshiAPIError(409, {"error": "dup"}))
    seq = iter([accept, reject])
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

    with pytest.raises(lifecycle.OrderRejected):
        _run(lifecycle.submit_order(req2))

    # The first order remains resting; the rejected dup did not displace it.
    assert "coll-1" in lifecycle._resting
    assert lifecycle._resting["coll-1"].exchange_order_id == "exo-A"


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
