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
from . import signer as polymarket_signer

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

    def to_polymarket_payload(self, *, settings: Any = None) -> dict[str, Any]:
        """Return the Polymarket order body for ``POST /order``.

        When wallet credentials (``POLYMARKET_PRIVATE_KEY``) are
        configured, this returns the fully L1-signed envelope shaped
        ``{"order": {...}, "owner": <api_key>, "orderType": ...}``
        ready for submission. Otherwise it returns an unsigned
        skeleton — useful for paper-mode introspection and tests.
        """
        if self.type == "limit" and self.limit_price_cents is None:
            raise ValueError("limit_price_cents required for limit orders")
        price_decimal = (self.limit_price_cents or 50) / 100.0

        s = settings or get_settings()
        if polymarket_signer.have_signing_creds():
            return polymarket_signer.build_signed_order(
                action=self.action,
                token_id=self.token_id,
                size_shares=int(self.count),
                price_decimal=price_decimal,
                time_in_force=self.time_in_force,
                api_key=s.polymarket_api_key or "",
                settings=s,
            )

        # Unsigned skeleton (paper / preview only — the CLOB will reject
        # this if it ever reaches the wire).
        side_str = "BUY" if self.action == "buy" else "SELL"
        return {
            "tokenID": self.token_id,
            "price": price_decimal,
            "size": int(self.count),
            "side": side_str,
            "orderType": (
                "GTC" if self.time_in_force == "GTC"
                else "FAK" if self.time_in_force == "IOC"
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


async def _record_fill_idempotent(
    *, order_id: str, fill_id: str, count: int, price_cents: int
) -> bool:
    """Insert one row into ``order_fills``. Returns True iff the row was
    newly inserted (i.e. this fill event is not a WS replay).

    Returns True on DB error too — failing-open is the safer default
    than silently dropping a real fill.
    """
    try:
        rid = await db.fetchval(
            """
            INSERT INTO order_fills (order_id, fill_id, count, price_cents)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (order_id, fill_id) DO NOTHING
            RETURNING order_id
            """,
            order_id, fill_id, int(count), int(price_cents),
        )
        return rid is not None
    except Exception as e:
        log.warning("polymarket order_fills insert failed (%s/%s): %s", order_id, fill_id, e)
        return True


# --- main entry ----------------------------------------------------------

class OrderRejected(RuntimeError):
    pass


async def submit_order(req: OrderRequest, *, force_paper: bool | None = None) -> OrderRecord:
    """Submit an order through the Polymarket lifecycle.

    `force_paper=True` always paper-trades. `force_paper=None` defers to
    the operator override in ``system_settings.risk.trading_mode`` (the
    Elite live-execute toggle), falling back to ``AXIOM_TRADING_MODE``.
    """
    s = get_settings()
    if force_paper is True:
        paper = True
    elif force_paper is False:
        paper = False
    else:
        from ..risk.trading_mode import resolve_trading_mode
        mode = await resolve_trading_mode(env_default=s.trading_mode)
        paper = mode != "live"

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
    if not polymarket_signer.have_signing_creds():
        await _record(req, paper=False, state="rejected", price=req.limit_price_cents or 0,
                      extra={"reason": "missing_l1_signer",
                             "hint": "set POLYMARKET_PRIVATE_KEY"})
        await _emit({"event": "rejected", "order": record.to_dict(),
                     "error": "POLYMARKET_PRIVATE_KEY not configured"})
        raise OrderRejected(
            "live Polymarket order requires POLYMARKET_PRIVATE_KEY for L1 signing"
        )
    try:
        signed_payload = req.to_polymarket_payload(settings=s)
        async with polymarket_client.PolymarketClient(settings=s) as c:
            ack = await c.submit_order(signed_payload)
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


# --- WS fill router -------------------------------------------------------

def _decimal_to_cents(value: Any) -> int:
    """Convert a Polymarket decimal price (e.g. 0.45 or "0.45") to cents (1..99).

    Returns 0 when the input is unparsable. Polymarket prices are unit
    prices in [0, 1]; we round to the nearest cent and clamp to a sane
    range so a malformed frame can't overflow downstream cents math.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    cents = int(round(f * 100))
    if cents < 0:
        return 0
    if cents > 100:
        return 100
    return cents


def _coerce_int(value: Any) -> int:
    """Polymarket sends sizes as strings (e.g. "10"). Coerce to int safely."""
    try:
        # Pass through floats too (e.g. partial fills like "5.0")
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _find_resting_for(
    *,
    exchange_order_ids: list[str],
    client_order_id: str | None,
) -> OrderRecord | None:
    """Locate a resting record matching either the venue order id(s) or
    the client_order_id. Caller already holds the resting lock."""
    if client_order_id and client_order_id in _resting:
        return _resting.get(client_order_id)
    if exchange_order_ids:
        wanted = {oid for oid in exchange_order_ids if oid}
        if wanted:
            for r in _resting.values():
                if r.exchange_order_id and r.exchange_order_id in wanted:
                    return r
    return None


def _iter_user_events(payload: Any) -> list[dict[str, Any]]:
    """Polymarket frames may arrive as a single event dict or as a list
    of event dicts. Normalize to a list of dicts so callers can iterate."""
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


async def handle_ws_fill(message: dict[str, Any]) -> None:
    """Best-effort: when the Polymarket WS bus emits a user-channel
    ``trade`` (or ``order``-with-match) event, mark the matching resting
    order as filled and write the ``filled`` row.

    Polymarket sends two relevant user-channel event shapes:

    * ``event_type: "trade"`` — a match between a taker and one or more
      makers. Carries ``taker_order_id``, ``maker_orders[].order_id``,
      ``size``, and ``price`` (decimal). **Trades are the single source
      of truth for executed quantity.**
    * ``event_type: "order"`` — status updates (PLACEMENT / UPDATE /
      CANCELLATION). We deliberately do NOT credit fills from
      ``size_matched`` here; doing so would double-count whenever the
      matching ``trade`` event also arrives (which it always does). The
      only state change applied on an order event is CANCELLATION,
      which removes the resting record.

    The trade-fill match is on ``exchange_order_id`` (recorded when the
    submit ack came back) with a fallback to ``client_order_id``.
    Idempotency is gated on (order_id, trade_id) via ``order_fills`` so
    a WS reconnect re-delivery doesn't double-count.
    """
    if (message or {}).get("type") != "polymarket_event":
        return
    for event in _iter_user_events(message.get("msg")):
        try:
            await _handle_one_event(event)
        except Exception as e:  # pragma: no cover — defensive
            log.warning("polymarket fill handler error: %s", e)


async def _handle_one_event(event: dict[str, Any]) -> None:
    et = str(event.get("event_type") or event.get("type") or "").lower()

    if et == "trade":
        taker_id = event.get("taker_order_id") or event.get("takerOrderId")
        coid = event.get("client_order_id") or event.get("clientOrderId")
        maker_ids = [
            (m or {}).get("order_id") or (m or {}).get("orderID")
            for m in (event.get("maker_orders") or event.get("makerOrders") or [])
            if isinstance(m, dict)
        ]
        candidate_ids = [taker_id, *maker_ids]
        async with _resting_lock:
            rec = _find_resting_for(
                exchange_order_ids=[oid for oid in candidate_ids if oid],
                client_order_id=str(coid) if coid else None,
            )
        if rec is None:
            return
        size = _coerce_int(event.get("size"))
        if size <= 0:
            return
        price_cents = _decimal_to_cents(event.get("price"))
        fill_id = str(event.get("id") or event.get("trade_id") or "")
        if not fill_id:
            # Fall back to a deterministic hash of the immutable fields so
            # replays of the same frame still dedupe.
            from ..notifications import alerts as _alerts
            raw = (
                f"{taker_id}|{event.get('size')}|{event.get('price')}|"
                f"{event.get('timestamp') or event.get('match_time') or ''}"
            )
            fill_id = f"raw:{_alerts._hash_reason(raw)}"
        await _apply_fill(
            rec=rec,
            fill_id=fill_id,
            count=size,
            price_cents=price_cents,
            raw=event,
        )
        return

    if et in ("order", "user_order"):
        # `order` events are status-only: PLACEMENT, UPDATE, or
        # CANCELLATION. Polymarket emits a separate `trade` event for
        # every actual match, so we deliberately do NOT count fills from
        # the `size_matched` field here — doing so would double-count
        # whenever an `order UPDATE` and the matching `trade` both
        # arrive (which is the typical sequence). Trades are the single
        # source of truth for executed quantity. The only state change
        # we apply on an order event is CANCELLATION, which removes the
        # resting record so a stale order doesn't strand in memory.
        update_type = str(event.get("type") or "").upper()
        if update_type != "CANCELLATION":
            return
        order_id = event.get("id") or event.get("order_id") or event.get("orderID")
        coid = event.get("client_order_id") or event.get("clientOrderId")
        async with _resting_lock:
            rec = _find_resting_for(
                exchange_order_ids=[order_id] if order_id else [],
                client_order_id=str(coid) if coid else None,
            )
            if rec is None:
                return
            _resting.pop(rec.order_id, None)
        rec.state = "canceled"
        await _record(
            rec.request, paper=False, state="canceled",
            price=rec.request.limit_price_cents or 0,
            extra={"exchange_order_id": rec.exchange_order_id, "ws_cancel": event},
        )
        await _emit({"event": "canceled", "order": rec.to_dict()})


async def _apply_fill(
    *,
    rec: OrderRecord,
    fill_id: str,
    count: int,
    price_cents: int,
    raw: dict[str, Any],
) -> None:
    """Apply one already-deduped-by-id fill to a resting record.

    Writes the ``order_fills`` idempotency row first; if the row was
    already present we drop the event silently. Otherwise update the
    in-memory record (weighted-average price, capped count), persist a
    ``filled`` lifecycle row, emit the bus event, and on the FIRST fill
    of a live order fire the operator ``live_trade`` alert (matching the
    Kalshi behavior).
    """
    fresh = await _record_fill_idempotent(
        order_id=rec.order_id,
        fill_id=fill_id,
        count=count,
        price_cents=price_cents,
    )
    if not fresh:
        log.info(
            "polymarket ws fill replay suppressed: order_id=%s fill_id=%s",
            rec.order_id, fill_id,
        )
        return

    requested = int(rec.request.count)
    prior_count = int(rec.filled_count)
    prior_avg = int(rec.avg_fill_price_cents or 0)
    remaining = max(0, requested - prior_count)
    accepted = min(count, remaining)
    is_first_fill = prior_count == 0 and accepted > 0

    new_count = prior_count + accepted
    if new_count > 0 and accepted > 0:
        rec.avg_fill_price_cents = int(round(
            (prior_avg * prior_count + price_cents * accepted) / new_count
        ))
    rec.filled_count = new_count
    rec.state = "filled" if rec.filled_count >= requested else "resting"
    if rec.state == "filled":
        async with _resting_lock:
            _resting.pop(rec.order_id, None)

    await _record(
        rec.request, paper=False, state="filled", price=price_cents,
        extra={
            "exchange_order_id": rec.exchange_order_id,
            "fill": raw,
            "fill_id": fill_id,
            "accepted_count": accepted,
            "rolled_up_avg_price_cents": rec.avg_fill_price_cents,
            "rolled_up_filled_count": rec.filled_count,
        },
    )
    await _emit({"event": "filled", "order": rec.to_dict()})

    if not is_first_fill:
        return

    # First fill of a live order — fire the operator broadcast that
    # mirrors the Kalshi live_trade alert. Dedupe so a re-fired bus
    # event from a downstream replay can't double-alert.
    try:
        from ..notifications import alerts as _alerts
        dk = f"polymarket:{rec.order_id}:{fill_id}"
        if await _alerts.dedupe_or_skip(
            "live_trade", dk, ttl_seconds=7 * 24 * 3600
        ):
            _alerts.live_trade(
                market_ticker=rec.request.market_id,
                side=rec.request.side,
                action=rec.request.action,
                contracts=int(rec.request.count),
                price_cents=price_cents,
                order_id=rec.order_id,
                fill_id=fill_id,
                vertical=rec.request.vertical,
                exchange="polymarket",
            )
    except Exception as e:  # pragma: no cover — alerting must not break fills
        log.warning("polymarket live_trade alert dispatch failed: %s", e)


async def attach_ws_fill_handler() -> asyncio.Task:
    """Spawn a background task that drains the Polymarket bus and routes
    user-channel fills into the lifecycle. Returns the task handle.
    """
    async def _runner() -> None:
        async for msg in bus.stream():
            if msg.get("type") != "polymarket_event":
                continue
            try:
                await handle_ws_fill(msg)
            except Exception as e:  # pragma: no cover
                log.warning("polymarket ws fill handler error: %s", e)
    return asyncio.create_task(_runner(), name="polymarket-fill-router")
