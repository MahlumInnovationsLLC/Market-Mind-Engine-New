"""Polymarket order lifecycle + paper-mode interceptor.

Mirrors `app.kalshi.lifecycle` so the orchestrator can route between
exchanges through identical primitives. Paper orders are simulated
filled at the limit price (or 50¢ for market orders); live orders
delegate to `PolymarketClient.submit_order` which the caller is
responsible for signing.

The kill switch is the *shared* `app.exchange_kill_switch` module —
tripping it from either Kalshi or Polymarket halts both venues.
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
from ..exchange_kill_switch import _kill, kill_switch_state  # noqa: F401 — re-exported via __init__
from .bus import bus
from . import client as polymarket_client

log = logging.getLogger(__name__)


Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
EventType = Literal["submitted", "resting", "filled", "canceled", "rejected", "settled"]

EXCHANGE = "polymarket"


# --- order types ----------------------------------------------------------

@dataclass
class OrderRequest:
    market_id: str                # Polymarket conditionId
    token_id: str                 # ERC1155 outcome token id (YES or NO)
    side: Side                    # "yes" | "no" — for our internal book
    action: Action                # "buy" | "sell"
    count: int                    # contracts (a.k.a. shares)
    limit_price_cents: int | None = None    # 1..99; required for limit orders
    type: Literal["limit", "market"] = "limit"
    time_in_force: Literal["GTC", "IOC", "FOK"] = "GTC"
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    vertical: str = "sports"
    context: dict[str, Any] = field(default_factory=dict)

    def to_polymarket_payload(self) -> dict[str, Any]:
        """Return the *unsigned* skeleton of a Polymarket order body.

        L1 (EIP-712) signing is the caller's responsibility; this method
        only assembles the fields that don't require a wallet signature.
        """
        if self.type == "limit" and self.limit_price_cents is None:
            raise ValueError("limit_price_cents required for limit orders")
        price_decimal = (self.limit_price_cents or 50) / 100.0
        side_str = "BUY" if self.action == "buy" else "SELL"
        return {
            "tokenID": self.token_id,
            "price": price_decimal,
            "size": int(self.count),
            "side": side_str,
            "orderType": (
                "GTC" if self.time_in_force == "GTC"
                else "IOC" if self.time_in_force == "IOC"
                else "FOK"
            ),
            "clientOrderId": self.client_order_id,
        }


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
        return {
            "exchange": EXCHANGE,
            "order_id": self.order_id,
            "paper": self.paper,
            "state": self.state,
            "exchange_order_id": self.exchange_order_id,
            "filled_count": self.filled_count,
            "avg_fill_price_cents": self.avg_fill_price_cents,
            "created_at": self.created_at.isoformat(),
            "request": {**asdict(self.request)},
        }


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
        log.warning("polymarket order_lifecycle insert failed (%s/%s): %s", order_id, event_type, e)
        return None


async def _emit(event: dict[str, Any]) -> None:
    await bus.publish({"type": "order_lifecycle", "exchange": EXCHANGE, **event})


# --- main entry ----------------------------------------------------------

class OrderRejected(RuntimeError):
    pass


async def submit_order(req: OrderRequest, *, force_paper: bool | None = None) -> OrderRecord:
    """Submit an order through the Polymarket lifecycle.

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
    if not req.token_id:
        await _record(req, paper=paper, state="rejected", price=req.limit_price_cents or 0,
                      extra={"reason": "missing_token_id"})
        raise OrderRejected("token_id is required")

    record = OrderRecord(
        order_id=req.client_order_id,
        request=req,
        paper=paper,
        state="submitted",
    )

    await _record(req, paper=paper, state="submitted", price=req.limit_price_cents or 0)
    await _emit({"event": "submitted", "order": record.to_dict()})

    if paper:
        # Short-circuit before the wire. Treat the order as immediately
        # filled at the limit price (or 50¢ for market orders) so
        # downstream P&L / settlement logic has something to chew on.
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
        async with polymarket_client.PolymarketClient(settings=s) as c:
            ack = await c.submit_order(req.to_polymarket_payload())
    except Exception as e:
        record.state = "rejected"
        await _record(req, paper=False, state="rejected", price=req.limit_price_cents or 0,
                      extra={"reason": "exchange_reject", "error": str(e)[:500]})
        await _emit({"event": "rejected", "order": record.to_dict(), "error": str(e)[:500]})
        raise OrderRejected(f"Polymarket rejected order: {e}") from e

    order_obj = (ack or {}).get("order") or ack or {}
    record.exchange_order_id = (
        order_obj.get("orderID") or order_obj.get("order_id") or order_obj.get("id")
    )
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
            async with polymarket_client.PolymarketClient(settings=s) as c:
                await c.cancel_order(rec.exchange_order_id or client_order_id)
        except Exception as e:
            log.warning("polymarket cancel failed for %s: %s", client_order_id, e)
            raise OrderRejected(f"cancel failed: {e}") from e
        rec.state = "canceled"

    async with _resting_lock:
        _resting.pop(client_order_id, None)

    await _record(rec.request, paper=rec.paper, state="canceled",
                  price=rec.request.limit_price_cents or 0,
                  extra={"exchange_order_id": rec.exchange_order_id})
    await _emit({"event": "canceled", "order": rec.to_dict()})
    return rec.to_dict()


# --- helpers --------------------------------------------------------------

async def _record(
    req: OrderRequest,
    *,
    paper: bool,
    state: EventType,
    price: int,
    extra: dict[str, Any] | None = None,
) -> None:
    md: dict[str, Any] = {
        "exchange": EXCHANGE,
        "paper": paper,
        "client_order_id": req.client_order_id,
        "token_id": req.token_id,
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
        market_id=req.market_id,
        event_type=state,
        side=req.side,
        price_cents=int(price or 0),
        contracts=int(req.count),
        vertical=req.vertical,
        metadata=md,
    )
