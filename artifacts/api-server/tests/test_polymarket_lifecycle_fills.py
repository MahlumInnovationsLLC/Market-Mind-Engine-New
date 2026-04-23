"""Unit tests for the Polymarket WS fill router.

The lifecycle module persists each transition by calling ``db.fetchval`` and
publishes events on an in-process bus. Tests stub the DB writer + the
fill-idempotency helper, drain the bus into a list, and pre-populate the
resting book so no real WS / wallet is needed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.polymarket import lifecycle
from app.polymarket.bus import bus


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def reset(monkeypatch):
    lifecycle._resting.clear()
    lifecycle._resting_lock = asyncio.Lock()
    bus._lock = asyncio.Lock()
    bus._subs.clear()

    written: list[dict[str, Any]] = []

    async def _fake_write_lifecycle(**kwargs):
        written.append(kwargs)
        return len(written)

    seen: set[tuple[str, str]] = set()

    async def _fake_record_fill(*, order_id, fill_id, count, price_cents):
        key = (order_id, fill_id)
        if key in seen:
            return False
        seen.add(key)
        return True

    monkeypatch.setattr(lifecycle, "_write_lifecycle", _fake_write_lifecycle)
    monkeypatch.setattr(lifecycle, "_record_fill_idempotent", _fake_record_fill)

    # Stub alerts so live_trade dispatch is a recorded no-op. Patch the
    # real module's attributes (not just sys.modules) so the inline
    # ``from ..notifications import alerts`` lookup inside the lifecycle
    # picks up the stubs even when pytest restores sys.modules between
    # tests in this file.
    fired: list[dict[str, Any]] = []

    async def _dedupe_or_skip(_alert, _key, *, ttl_seconds):
        return True

    from app.notifications import alerts as _real_alerts
    monkeypatch.setattr(_real_alerts, "dedupe_or_skip", _dedupe_or_skip, raising=False)
    monkeypatch.setattr(_real_alerts, "live_trade", lambda **kw: fired.append(kw), raising=False)
    if not hasattr(_real_alerts, "_hash_reason"):
        monkeypatch.setattr(
            _real_alerts, "_hash_reason",
            lambda raw: f"h{abs(hash(raw)) % 10**8}", raising=False,
        )

    return SimpleNamespace(written=written, fired=fired, seen=seen)


def _seed_resting(*, order_id="cli-1", exch_id="0xVENUE", count=10) -> None:
    req = lifecycle.OrderRequest(
        market_id="0xCOND",
        token_id="123token",
        side="yes",
        action="buy",
        count=count,
        limit_price_cents=45,
        client_order_id=order_id,
        vertical="sports",
    )
    rec = lifecycle.OrderRecord(
        order_id=order_id,
        request=req,
        paper=False,
        state="resting",
        exchange_order_id=exch_id,
    )
    lifecycle._resting[order_id] = rec


def test_trade_fills_resting_order_and_fires_alert(reset):
    _seed_resting(count=10, exch_id="0xVENUE")
    msg = {
        "type": "polymarket_event",
        "msg": {
            "event_type": "trade",
            "id": "trade-A",
            "market": "0xCOND",
            "asset_id": "123token",
            "side": "BUY",
            "size": "10",
            "price": "0.45",
            "taker_order_id": "0xVENUE",
            "maker_orders": [],
        },
    }
    _run(lifecycle.handle_ws_fill(msg))

    rec = lifecycle._resting.get("cli-1")
    assert rec is None  # fully filled, popped from book
    assert reset.written[-1]["event_type"] == "filled"
    assert reset.written[-1]["price_cents"] == 45
    assert reset.fired and reset.fired[0]["order_id"] == "cli-1"


def test_trade_replay_is_deduped(reset):
    _seed_resting(count=10, exch_id="0xVENUE")
    msg = {
        "type": "polymarket_event",
        "msg": {
            "event_type": "trade",
            "id": "trade-A",
            "size": "5",
            "price": "0.50",
            "taker_order_id": "0xVENUE",
        },
    }

    async def _scenario():
        await lifecycle.handle_ws_fill(msg)
        await lifecycle.handle_ws_fill(msg)  # replay

    _run(_scenario())

    # One filled row only — the second call short-circuits on idempotency.
    filled_rows = [w for w in reset.written if w["event_type"] == "filled"]
    assert len(filled_rows) == 1
    # Resting record should still hold 5 of 10.
    rec = lifecycle._resting.get("cli-1")
    assert rec is not None and rec.filled_count == 5
    # Alert fires only once (first fill).
    assert len(reset.fired) == 1


def test_partial_then_complete_weighted_average(reset):
    _seed_resting(count=10, exch_id="0xVENUE")

    async def _scenario():
        await lifecycle.handle_ws_fill({
            "type": "polymarket_event",
            "msg": {
                "event_type": "trade", "id": "t1",
                "size": "4", "price": "0.40",
                "taker_order_id": "0xVENUE",
            },
        })
        await lifecycle.handle_ws_fill({
            "type": "polymarket_event",
            "msg": {
                "event_type": "trade", "id": "t2",
                "size": "6", "price": "0.50",
                "taker_order_id": "0xVENUE",
            },
        })

    _run(_scenario())
    # (40*4 + 50*6) / 10 = 46
    assert lifecycle._resting.get("cli-1") is None
    # Alert fires only on first fill, not on the closing one.
    assert len(reset.fired) == 1


def test_order_update_event_does_not_count_as_fill(reset):
    """Trades are the single source of truth; `order` UPDATE must not
    advance filled_count or the same execution would be counted twice
    when its corresponding `trade` event arrives."""
    _seed_resting(count=10, exch_id="0xVENUE")
    _run(lifecycle.handle_ws_fill({
        "type": "polymarket_event",
        "msg": {
            "event_type": "order",
            "type": "UPDATE",
            "id": "0xVENUE",
            "size_matched": "3",
            "price": "0.45",
        },
    }))
    rec = lifecycle._resting.get("cli-1")
    assert rec is not None and rec.filled_count == 0
    assert all(w["event_type"] != "filled" for w in reset.written)


def test_order_cancellation_removes_resting_order(reset):
    _seed_resting(count=10, exch_id="0xVENUE")
    _run(lifecycle.handle_ws_fill({
        "type": "polymarket_event",
        "msg": {
            "event_type": "order",
            "type": "CANCELLATION",
            "id": "0xVENUE",
        },
    }))
    assert lifecycle._resting.get("cli-1") is None
    assert reset.written[-1]["event_type"] == "canceled"


def test_order_update_then_trade_does_not_double_count(reset):
    """Regression: when an `order UPDATE` and the matching `trade` both
    arrive for the same execution, only the trade should advance
    filled_count. Order is `update -> trade`."""
    _seed_resting(count=10, exch_id="0xVENUE")

    async def _scenario():
        await lifecycle.handle_ws_fill({
            "type": "polymarket_event",
            "msg": {
                "event_type": "order", "type": "UPDATE",
                "id": "0xVENUE", "size_matched": "4", "price": "0.40",
            },
        })
        await lifecycle.handle_ws_fill({
            "type": "polymarket_event",
            "msg": {
                "event_type": "trade", "id": "trade-X",
                "size": "4", "price": "0.40",
                "taker_order_id": "0xVENUE",
            },
        })

    _run(_scenario())
    rec = lifecycle._resting.get("cli-1")
    assert rec is not None and rec.filled_count == 4
    assert rec.avg_fill_price_cents == 40


def test_trade_then_order_update_does_not_double_count(reset):
    """Regression: same as above but the trade arrives before the order
    UPDATE. The order UPDATE must not re-credit the same fill."""
    _seed_resting(count=10, exch_id="0xVENUE")

    async def _scenario():
        await lifecycle.handle_ws_fill({
            "type": "polymarket_event",
            "msg": {
                "event_type": "trade", "id": "trade-Y",
                "size": "3", "price": "0.55",
                "taker_order_id": "0xVENUE",
            },
        })
        await lifecycle.handle_ws_fill({
            "type": "polymarket_event",
            "msg": {
                "event_type": "order", "type": "UPDATE",
                "id": "0xVENUE", "size_matched": "3", "price": "0.55",
            },
        })

    _run(_scenario())
    rec = lifecycle._resting.get("cli-1")
    assert rec is not None and rec.filled_count == 3
    assert rec.avg_fill_price_cents == 55


def test_unknown_order_id_is_dropped(reset):
    _seed_resting(count=10, exch_id="0xVENUE")
    _run(lifecycle.handle_ws_fill({
        "type": "polymarket_event",
        "msg": {
            "event_type": "trade", "id": "tX",
            "size": "1", "price": "0.50",
            "taker_order_id": "0xUNKNOWN",
        },
    }))
    rec = lifecycle._resting.get("cli-1")
    assert rec is not None and rec.filled_count == 0
    assert not reset.fired


def test_non_polymarket_messages_ignored(reset):
    _seed_resting(count=10, exch_id="0xVENUE")
    _run(lifecycle.handle_ws_fill({"type": "kalshi_event", "msg": {}}))
    rec = lifecycle._resting.get("cli-1")
    assert rec is not None and rec.filled_count == 0


def test_attach_ws_fill_handler_drains_bus(reset):
    _seed_resting(count=2, exch_id="0xVENUE")

    async def _scenario():
        task = await lifecycle.attach_ws_fill_handler()
        # Yield once so the task's bus.stream() subscription is registered
        # before we publish; otherwise the publish offers to zero subs.
        for _ in range(5):
            await asyncio.sleep(0)
            if bus.subscriber_count > 0:
                break
        try:
            await bus.publish({
                "type": "polymarket_event",
                "msg": {
                    "event_type": "trade", "id": "t1",
                    "size": "2", "price": "0.55",
                    "taker_order_id": "0xVENUE",
                },
            })
            for _ in range(20):
                await asyncio.sleep(0.01)
                if "cli-1" not in lifecycle._resting:
                    break
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass

    _run(_scenario())
    assert lifecycle._resting.get("cli-1") is None
