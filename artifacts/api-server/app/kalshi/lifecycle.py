"""Order lifecycle state machine + paper-mode interceptor + kill switch.

Both paper and live orders flow through `submit_order`:

    submit_order(req)
        |
        v
    [kill switch] -> reject
        |
        v
    write `submitted` row to order_lifecycle (paper flag in metadata)
        |
        v
    if paper:                       if live:
       simulate fill                 client.submit_order(payload)
       write `filled` row            on ack: write `resting` row
                                     (real fills arrive via WS -> handle_ws_fill)

Every transition publishes a message on the in-process bus, so a connected
WebSocket client sees real-time order events for both modes.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Literal

from .. import db
from ..config import get_settings
from .bus import bus
from . import client as kalshi_client

log = logging.getLogger(__name__)


Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
EventType = Literal["submitted", "resting", "filled", "canceled", "rejected", "settled"]


# --- kill switch ----------------------------------------------------------

class _KillSwitch:
    def __init__(self) -> None:
        self._halted = False
        self._reason: str | None = None
        self._at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def state(self) -> dict[str, Any]:
        return {
            "halted": self._halted,
            "reason": self._reason,
            "since": self._at.isoformat() if self._at else None,
        }

    def trip(self, reason: str | None = None) -> dict[str, Any]:
        was_halted = self._halted
        self._halted = True
        self._reason = reason or "operator halt"
        self._at = datetime.now(timezone.utc)
        log.warning("KILL SWITCH tripped: %s", self._reason)
        # Fire halt alert only on the rising edge so re-tripping doesn't spam.
        if not was_halted:
            try:
                from ..notifications import alerts as _alerts
                _alerts.halt(reason=self._reason)
            except Exception as e:  # pragma: no cover — notification must not break halt
                log.warning("halt alert dispatch failed: %s", e)
        return self.state

    def reset(self) -> dict[str, Any]:
        self._halted = False
        self._reason = None
        self._at = None
        log.info("KILL SWITCH reset")
        return self.state


_kill = _KillSwitch()


def is_halted() -> bool:
    return _kill.halted


def halt(reason: str | None = None) -> dict[str, Any]:
    return _kill.trip(reason)


def resume() -> dict[str, Any]:
    return _kill.reset()


def kill_switch_state() -> dict[str, Any]:
    return _kill.state


# --- order types ----------------------------------------------------------

@dataclass
class OrderRequest:
    market_ticker: str
    side: Side                  # "yes" | "no"
    action: Action              # "buy" | "sell"
    count: int                  # contracts
    limit_price_cents: int | None = None  # required for limit orders
    type: Literal["limit", "market"] = "limit"
    time_in_force: Literal["GTC", "IOC", "FOK"] = "GTC"
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    vertical: str = "sports"
    # Pre-trade context (swarm consensus, model confidence, etc.). Stored in metadata.
    context: dict[str, Any] = field(default_factory=dict)

    def to_kalshi_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ticker": self.market_ticker,
            "action": self.action,
            "side": self.side,
            "count": int(self.count),
            "type": self.type,
            "client_order_id": self.client_order_id,
            "time_in_force": self.time_in_force,
        }
        if self.type == "limit":
            if self.limit_price_cents is None:
                raise ValueError("limit_price_cents required for limit orders")
            # Kalshi expects yes_price/no_price for limit orders, in cents 1-99
            if self.side == "yes":
                payload["yes_price"] = int(self.limit_price_cents)
            else:
                payload["no_price"] = int(self.limit_price_cents)
        return payload


@dataclass
class OrderRecord:
    order_id: str                  # internal id == client_order_id
    request: OrderRequest
    paper: bool
    state: EventType
    exchange_order_id: str | None = None
    filled_count: int = 0
    avg_fill_price_cents: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = {
            "order_id": self.order_id,
            "paper": self.paper,
            "state": self.state,
            "exchange_order_id": self.exchange_order_id,
            "filled_count": self.filled_count,
            "avg_fill_price_cents": self.avg_fill_price_cents,
            "created_at": self.created_at.isoformat(),
            "request": {**asdict(self.request)},
        }
        return d


# --- in-memory order book (resting orders) -------------------------------

_resting: dict[str, OrderRecord] = {}
_resting_lock = asyncio.Lock()


async def list_resting_orders() -> list[OrderRecord]:
    async with _resting_lock:
        return list(_resting.values())


# --- persistence ---------------------------------------------------------

async def _write_lifecycle(
    *,
    order_id: str,
    market_id: str,
    event_type: EventType,
    side: Side,
    price_cents: int,
    contracts: int,
    vertical: str,
    metadata: dict[str, Any],
) -> int | None:
    """Insert a row into the existing `order_lifecycle` table. Returns row id or None on failure."""
    import json
    try:
        rid = await db.fetchval(
            """
            INSERT INTO order_lifecycle
                (order_id, market_id, event_type, side, price_cents,
                 spread_cents, volume, offset_cents, contracts,
                 is_maker, vertical, metadata)
            VALUES ($1, $2, $3, $4, $5, 2, 0, 0, $6, false, $7, $8::jsonb)
            RETURNING id
            """,
            order_id, market_id, event_type, side, int(price_cents),
            int(contracts), vertical, json.dumps(metadata),
        )
        return int(rid) if rid is not None else None
    except Exception as e:
        log.warning("order_lifecycle insert failed (%s/%s): %s", order_id, event_type, e)
        return None


async def _emit(event: dict[str, Any]) -> None:
    await bus.publish({"type": "order_lifecycle", **event})


# --- main entry ----------------------------------------------------------

class OrderRejected(RuntimeError):
    pass


async def submit_order(req: OrderRequest, *, force_paper: bool | None = None) -> OrderRecord:
    """Submit an order through the lifecycle.

    `force_paper=True` always paper-trades. `force_paper=None` defers to
    `AXIOM_TRADING_MODE` (paper unless explicitly set to "live").
    """
    s = get_settings()
    paper = True if force_paper is True else (s.trading_mode != "live") if force_paper is None else False

    if _kill.halted:
        await _record(req, paper=paper, state="rejected", price=req.limit_price_cents or 0,
                      extra={"reason": "kill_switch", "kill": _kill.state})
        raise OrderRejected(f"kill switch active: {_kill.state.get('reason')}")

    # Defensive validation
    if req.count <= 0:
        await _record(req, paper=paper, state="rejected", price=req.limit_price_cents or 0,
                      extra={"reason": "bad_count"})
        raise OrderRejected("count must be > 0")
    if req.type == "limit" and not (1 <= int(req.limit_price_cents or 0) <= 99):
        await _record(req, paper=paper, state="rejected", price=req.limit_price_cents or 0,
                      extra={"reason": "bad_price"})
        raise OrderRejected("limit_price_cents must be 1..99")

    record = OrderRecord(
        order_id=req.client_order_id,
        request=req,
        paper=paper,
        state="submitted",
    )

    await _record(req, paper=paper, state="submitted", price=req.limit_price_cents or 0)
    await _emit({"event": "submitted", "order": record.to_dict()})

    if paper:
        # Short-circuit BEFORE the wire. Treat the order as immediately
        # filled at the limit price (or 50¢ for market orders) so downstream
        # P&L / settlement logic has something to chew on.
        fill_price = int(req.limit_price_cents or 50)
        record.state = "filled"
        record.filled_count = req.count
        record.avg_fill_price_cents = fill_price
        await _record(req, paper=True, state="filled", price=fill_price,
                      extra={"simulated": True})
        await _emit({"event": "filled", "order": record.to_dict()})
        return record

    # ---- live path ------------------------------------------------------
    try:
        async with kalshi_client.KalshiClient(settings=s) as c:
            ack = await c.submit_order(req.to_kalshi_payload())
    except Exception as e:
        record.state = "rejected"
        await _record(req, paper=False, state="rejected", price=req.limit_price_cents or 0,
                      extra={"reason": "exchange_reject", "error": str(e)[:500]})
        await _emit({"event": "rejected", "order": record.to_dict(), "error": str(e)[:500]})
        raise OrderRejected(f"Kalshi rejected order: {e}") from e

    order_obj = (ack or {}).get("order") or ack or {}
    record.exchange_order_id = order_obj.get("order_id") or order_obj.get("id")
    record.state = "resting"
    async with _resting_lock:
        _resting[record.order_id] = record

    await _record(req, paper=False, state="resting", price=req.limit_price_cents or 0,
                  extra={"exchange_order_id": record.exchange_order_id, "ack": order_obj})
    await _emit({"event": "resting", "order": record.to_dict()})
    return record


async def cancel_order(client_order_id: str) -> dict[str, Any]:
    """Cancel a resting order. Paper orders fill instantly so are never resting."""
    async with _resting_lock:
        rec = _resting.get(client_order_id)
    if rec is None:
        raise OrderRejected(f"no resting order with id {client_order_id}")
    s = get_settings()
    if rec.paper:
        rec.state = "canceled"
    else:
        try:
            async with kalshi_client.KalshiClient(settings=s) as c:
                await c.cancel_order(rec.exchange_order_id or client_order_id)
        except Exception as e:
            log.warning("cancel failed for %s: %s", client_order_id, e)
            raise OrderRejected(f"cancel failed: {e}") from e
        rec.state = "canceled"

    async with _resting_lock:
        _resting.pop(client_order_id, None)

    await _record(rec.request, paper=rec.paper, state="canceled",
                  price=rec.request.limit_price_cents or 0,
                  extra={"exchange_order_id": rec.exchange_order_id})
    await _emit({"event": "canceled", "order": rec.to_dict()})
    return rec.to_dict()


async def cancel_all_resting() -> int:
    async with _resting_lock:
        ids = list(_resting.keys())
    n = 0
    for oid in ids:
        try:
            await cancel_order(oid)
            n += 1
        except Exception as e:
            log.warning("cancel_all: failed for %s: %s", oid, e)
    return n


async def handle_ws_fill(message: dict[str, Any]) -> None:
    """Best-effort: when the WS bus emits a fill event, mark the matching
    resting order as filled and write the `filled` row.
    """
    payload = (message or {}).get("msg", {})
    if not isinstance(payload, dict):
        return
    if (payload.get("type") or payload.get("channel")) != "fill":
        return
    fill = payload.get("msg") or payload.get("data") or {}
    coid = fill.get("client_order_id") or fill.get("clientOrderId")
    if not coid:
        return
    async with _resting_lock:
        rec = _resting.get(coid)
    if rec is None:
        return
    count = int(fill.get("count") or 0)
    price = int(fill.get("yes_price") if rec.request.side == "yes" else fill.get("no_price") or 0)
    rec.filled_count += count
    rec.avg_fill_price_cents = price
    rec.state = "filled" if rec.filled_count >= rec.request.count else "resting"
    if rec.state == "filled":
        async with _resting_lock:
            _resting.pop(coid, None)
    await _record(rec.request, paper=False, state="filled", price=price,
                  extra={"exchange_order_id": rec.exchange_order_id, "fill": fill})
    await _emit({"event": "filled", "order": rec.to_dict()})
    # LIVE-trade alert (Elite-tier per spec). Paper fills never get
    # here — they short-circuit in submit_order().
    try:
        from ..notifications import alerts as _alerts
        _alerts.live_trade(
            market_ticker=rec.request.market_ticker,
            side=rec.request.side,
            action=rec.request.action,
            contracts=int(rec.request.count),
            price_cents=price,
            order_id=rec.order_id,
            fill_id=str(fill.get("trade_id") or fill.get("fill_id") or ""),
            vertical=rec.request.vertical,
        )
    except Exception as e:  # pragma: no cover
        log.warning("live_trade alert dispatch failed: %s", e)


# --- helpers --------------------------------------------------------------

async def _record(
    req: OrderRequest,
    *,
    paper: bool,
    state: EventType,
    price: int,
    extra: dict[str, Any] | None = None,
) -> None:
    md = {
        "paper": paper,
        "client_order_id": req.client_order_id,
        "action": req.action,
        "type": req.type,
        "tif": req.time_in_force,
        "context": req.context,
        "trading_mode": "paper" if paper else "live",
    }
    if extra:
        md.update(extra)
    await _write_lifecycle(
        order_id=req.client_order_id,
        market_id=req.market_ticker,
        event_type=state,
        side=req.side,
        price_cents=int(price or 0),
        contracts=int(req.count),
        vertical=req.vertical,
        metadata=md,
    )


async def attach_ws_fill_handler() -> asyncio.Task:
    """Spawn a background task that drains the bus and routes fills into
    the lifecycle. Returns the task handle.
    """
    async def _runner() -> None:
        async for msg in bus.stream():
            if msg.get("type") == "kalshi_event":
                try:
                    await handle_ws_fill(msg)
                except Exception as e:  # pragma: no cover
                    log.warning("ws fill handler error: %s", e)
    return asyncio.create_task(_runner(), name="kalshi-fill-router")
