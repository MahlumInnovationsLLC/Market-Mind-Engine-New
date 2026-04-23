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
from ..exchange_kill_switch import (
    _kill,
    halt,
    is_halted,
    kill_switch_state,
    resume,
)
from .bus import bus
from . import client as kalshi_client
from . import orderbook as kalshi_orderbook

log = logging.getLogger(__name__)


Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
EventType = Literal[
    "submitted", "resting", "filled", "canceled", "rejected", "settled",
    "submit_failed",
]


# --- submit-recovery counters (surfaced on /api/health/snapshot) ---------
#
# A live ``POST /portfolio/orders`` is intentionally single-shot so we never
# risk a double-submit. But a network timeout / 5xx leaves the order in an
# unknown state. The recovery path (see ``submit_order``) asks Kalshi for
# the order by ``client_order_id`` and applies venue truth locally. These
# counters give operators a read on how often that path fires and how
# often it actually recovers a real order.
_submit_recovery: dict[str, int | str | float | None] = {
    "attempts": 0,              # recovery path entered
    "recovered_total": 0,       # venue had the order (resting / filled / canceled)
    "recovered_resting": 0,
    "recovered_filled": 0,
    "recovered_canceled": 0,
    "not_found": 0,             # venue did not accept the order
    "lookup_failed": 0,         # lookup itself raised — safe retry next cycle
    "last_attempt_at": None,
    "last_outcome": None,       # str
    "last_error": None,         # str
}


def submit_recovery_state() -> dict[str, Any]:
    """JSON-safe copy of the submit-recovery counters for health snapshots."""
    return dict(_submit_recovery)


def _reset_submit_recovery_state() -> None:
    """Test helper — clears every counter."""
    _submit_recovery.update(
        {
            "attempts": 0,
            "recovered_total": 0,
            "recovered_resting": 0,
            "recovered_filled": 0,
            "recovered_canceled": 0,
            "not_found": 0,
            "lookup_failed": 0,
            "last_attempt_at": None,
            "last_outcome": None,
            "last_error": None,
        }
    )


# HTTP statuses that make the submit POST outcome ambiguous (venue may
# have accepted the order or may not have). 408 = client timeout, 429 =
# rate limit (transport-level retry is safe because the first request
# may never have reached the matching engine), 5xx = server trouble.
# status == 0 is our synthetic "network error" mapping from
# KalshiAPIError when httpx raises before a response arrives.
_RECOVERABLE_SUBMIT_STATUSES = frozenset({0, 408, 429, 500, 502, 503, 504})


__all__ = [
    "OrderRequest",
    "OrderRecord",
    "OrderRejected",
    "submit_order",
    "cancel_order",
    "cancel_all_resting",
    "halt",
    "resume",
    "is_halted",
    "kill_switch_state",
    "list_resting_orders",
    "handle_ws_fill",
    "attach_ws_fill_handler",
]


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
    over_filled: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = {
            "order_id": self.order_id,
            "paper": self.paper,
            "state": self.state,
            "exchange_order_id": self.exchange_order_id,
            "filled_count": self.filled_count,
            "avg_fill_price_cents": self.avg_fill_price_cents,
            "over_filled": self.over_filled,
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


async def _record_fill_idempotent(
    *, order_id: str, fill_id: str, count: int, price_cents: int
) -> bool:
    """Insert one row into `order_fills`. Returns True iff the row was newly
    inserted (i.e. this fill event is NOT a WebSocket replay).

    Acts as the dedupe gate for `handle_ws_fill`: when Kalshi re-delivers
    the same partial-fill on reconnect, the unique (order_id, fill_id)
    constraint trips and we report False so the caller skips state
    mutation. Returns True on DB error too — failing-open is the safer
    default than silently dropping a real fill.
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
        log.warning("order_fills insert failed (%s/%s): %s", order_id, fill_id, e)
        return True


async def _upsert_position_rollup(
    *,
    order_id: str,
    market_id: str,
    side: Side,
    vertical: str,
    quantity: int,
    avg_price_cents: int,
    over_filled: bool,
    closed: bool,
) -> None:
    """UPSERT the rolled-up position row keyed by `order_id`.

    `quantity` is the cumulative filled count CAPPED at the order's
    requested size (over-fill never inflates exposure here — see the
    `over_filled` flag for the audit trail). `avg_price_cents` is the
    weighted average across all accepted partial fills.

    The PnL pipeline reads from `positions` and now sees one row per
    completed/partially-filled order rather than one row per raw fill
    event from `order_lifecycle`.
    """
    try:
        # Store avg_price as a unit price (0..1) to match the convention used
        # by orchestrator/paper.py (`price = ctx.current_price`) so the
        # settlement pipeline's `_trade_pnl` math is consistent.
        avg_unit = float(avg_price_cents) / 100.0
        status = "closed" if closed and quantity > 0 else "open"
        await db.execute(
            """
            INSERT INTO positions (
                order_id, market_id, side, vertical, exchange,
                quantity, avg_price, status, filled_count, over_filled
            ) VALUES (
                $1, $2, $3, $4, 'kalshi',
                $5, $6, $7, $5, $8
            )
            ON CONFLICT (order_id) DO UPDATE
               SET quantity     = EXCLUDED.quantity,
                   avg_price    = EXCLUDED.avg_price,
                   filled_count = EXCLUDED.filled_count,
                   over_filled  = positions.over_filled OR EXCLUDED.over_filled,
                   status       = EXCLUDED.status
            """,
            order_id, market_id, side, vertical,
            int(quantity), avg_unit, status, bool(over_filled),
        )
    except Exception as e:
        log.warning("position rollup upsert failed (%s): %s", order_id, e)


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

    # ---- pre-submit orderbook liquidity / slippage guard -----------------
    # When the request carries `model_edge_cents` in its context, project the
    # worst-case fill against the cached WS orderbook. If projected slippage
    # would erase more than `slippage_guard_max_edge_pct` of the model edge,
    # reject before submission with reason=`slippage_guard`.
    guard = await _check_slippage_guard(req, settings=s)
    if guard is not None:
        await _record(req, paper=paper, state="rejected",
                      price=req.limit_price_cents or 0,
                      extra={"reason": "slippage_guard", **guard})
        raise OrderRejected(
            f"slippage guard: {guard.get('detail') or 'rejected'}"
        )

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
        # Distinguish "venue definitely rejected us" (terminal 4xx) from
        # "we don't know whether the venue accepted the order"
        # (timeout / 5xx / transport error). The latter is recoverable
        # via ``client_order_id`` lookup; the former is terminal.
        if _is_recoverable_submit_error(e):
            rec_state = await _attempt_submit_recovery(req, record, exc=e, settings=s)
            if rec_state is not None:
                return rec_state
            # Fell through: venue does not have the order, OR the
            # lookup itself failed. The `submit_failed` row has already
            # been recorded and emitted by the helper; raise so the
            # caller knows to treat this as a retry-next-cycle outcome
            # rather than a permanent rejection.
            raise OrderRejected(
                f"Kalshi submit unresolved ({type(e).__name__}: {e}) — "
                "venue has no record of this client_order_id"
            ) from e
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


def _is_recoverable_submit_error(exc: BaseException) -> bool:
    """True when a submit-POST failure leaves the venue state ambiguous.

    KalshiAPIError with a status in ``_RECOVERABLE_SUBMIT_STATUSES`` is
    the main case. Anything else is treated as terminal (4xx, auth
    errors, programming errors) — those definitely did not result in an
    accepted order at the venue.
    """
    from . import client as _kc  # local import to avoid cycle at module load
    if isinstance(exc, _kc.KalshiAPIError):
        return int(getattr(exc, "status", -1) or 0) in _RECOVERABLE_SUBMIT_STATUSES
    return False


async def _attempt_submit_recovery(
    req: OrderRequest,
    record: OrderRecord,
    *,
    exc: BaseException,
    settings,
) -> OrderRecord | None:
    """Resolve an ambiguous submit POST via ``client_order_id`` lookup.

    Returns the updated :class:`OrderRecord` when venue truth could be
    applied locally (order is resting / filled / canceled). Returns
    ``None`` when the order is not at the venue OR when the lookup
    itself failed — the caller should raise ``OrderRejected`` so the
    next cycle tries afresh. In either terminal case a
    ``submit_failed`` lifecycle row has been recorded and emitted
    before we return.
    """
    import time as _time
    _submit_recovery["attempts"] = int(_submit_recovery["attempts"]) + 1  # type: ignore[arg-type]
    _submit_recovery["last_attempt_at"] = _time.time()
    err_str = f"{type(exc).__name__}: {exc}"[:300]

    # Record the ambiguous POST outcome up front — regardless of what
    # the lookup returns, operators should see the timeout / 5xx in the
    # lifecycle audit trail.
    await _record(
        req, paper=False, state="submit_failed",
        price=req.limit_price_cents or 0,
        extra={"reason": "submit_ambiguous", "error": err_str},
    )
    await _emit({
        "event": "submit_failed",
        "order": record.to_dict(),
        "error": err_str,
    })

    try:
        async with kalshi_client.KalshiClient(settings=settings) as c:
            result = await c.get_order_by_client_id(req.client_order_id)
    except Exception as lookup_exc:  # noqa: BLE001 — counted + retried next cycle
        _submit_recovery["lookup_failed"] = int(_submit_recovery["lookup_failed"]) + 1  # type: ignore[arg-type]
        _submit_recovery["last_outcome"] = "lookup_failed"
        _submit_recovery["last_error"] = f"{type(lookup_exc).__name__}: {lookup_exc}"[:300]
        log.warning(
            "submit recovery lookup failed for %s: %s",
            req.client_order_id, lookup_exc,
        )
        return None

    if not result.get("found"):
        _submit_recovery["not_found"] = int(_submit_recovery["not_found"]) + 1  # type: ignore[arg-type]
        _submit_recovery["last_outcome"] = "not_found"
        _submit_recovery["last_error"] = err_str
        log.warning(
            "submit recovery: venue has no order for client_order_id=%s "
            "(original error: %s)",
            req.client_order_id, err_str,
        )
        return None

    order = result.get("order") or {}
    exchange_order_id = order.get("order_id") or order.get("id")
    # Kalshi order statuses: resting, executed, canceled. Some responses
    # nest under `order.status` as a string; others flatten. Be liberal.
    venue_status = str(
        order.get("status") or order.get("order_status") or ""
    ).lower()

    # Defensive: a "found" result without either an exchange id or a
    # known status is not something we can safely promote. Treat the
    # shape as malformed and fall back to not_found semantics so the
    # caller retries on the next cycle instead of inventing local state.
    _known_statuses = {
        "resting", "open", "new",
        "executed", "filled", "trade_done",
        "canceled", "cancelled",
    }
    if not exchange_order_id and venue_status not in _known_statuses:
        _submit_recovery["not_found"] = int(_submit_recovery["not_found"]) + 1  # type: ignore[arg-type]
        _submit_recovery["last_outcome"] = "not_found_malformed"
        _submit_recovery["last_error"] = (
            f"malformed venue order: missing exchange id and unknown status "
            f"{venue_status!r}"
        )
        log.warning(
            "submit recovery: malformed venue order for %s: %r",
            req.client_order_id, order,
        )
        return None
    record.exchange_order_id = exchange_order_id

    if venue_status in ("canceled", "cancelled"):
        record.state = "canceled"
        _submit_recovery["recovered_canceled"] = int(_submit_recovery["recovered_canceled"]) + 1  # type: ignore[arg-type]
        _submit_recovery["recovered_total"] = int(_submit_recovery["recovered_total"]) + 1  # type: ignore[arg-type]
        _submit_recovery["last_outcome"] = "recovered_canceled"
        await _record(
            req, paper=False, state="canceled",
            price=req.limit_price_cents or 0,
            extra={
                "reason": "submit_recovered",
                "exchange_order_id": exchange_order_id,
                "venue_status": venue_status,
                "original_error": err_str,
            },
        )
        await _emit({"event": "canceled", "order": record.to_dict()})
        log.info(
            "submit recovery: client_order_id=%s -> canceled at venue",
            req.client_order_id,
        )
        return record

    if venue_status in ("executed", "filled", "trade_done"):
        # Venue says the order is already done. We don't have individual
        # fill detail here — the reconciler / WS replay will back-fill
        # price and count via the idempotent order_fills table. Record a
        # filled lifecycle row as the marker.
        record.state = "filled"
        # Prefer any price/count the venue volunteered (Kalshi sometimes
        # includes `filled_count` and a weighted avg); otherwise fall
        # back to the order's original limit so downstream observability
        # has a non-zero number until the reconciler catches up.
        filled_count = int(
            order.get("filled_count")
            or order.get("executed_count")
            or req.count
        )
        avg_price = (
            order.get("avg_fill_price")
            or order.get("average_price")
            or req.limit_price_cents
            or 0
        )
        record.filled_count = filled_count
        record.avg_fill_price_cents = int(avg_price or 0)
        _submit_recovery["recovered_filled"] = int(_submit_recovery["recovered_filled"]) + 1  # type: ignore[arg-type]
        _submit_recovery["recovered_total"] = int(_submit_recovery["recovered_total"]) + 1  # type: ignore[arg-type]
        _submit_recovery["last_outcome"] = "recovered_filled"
        await _record(
            req, paper=False, state="filled",
            price=int(avg_price or 0),
            extra={
                "reason": "submit_recovered",
                "exchange_order_id": exchange_order_id,
                "venue_status": venue_status,
                "filled_count": filled_count,
                "original_error": err_str,
                "note": "fill detail will be back-filled by reconciler",
            },
        )
        await _emit({"event": "filled", "order": record.to_dict()})
        log.info(
            "submit recovery: client_order_id=%s -> filled at venue (count=%d)",
            req.client_order_id, filled_count,
        )
        return record

    # Default: treat as resting (includes "resting", "open", "new", or
    # any unrecognised-but-non-terminal state). Promote to the local
    # resting book so cancel / fill routing works normally.
    record.state = "resting"
    async with _resting_lock:
        _resting[record.order_id] = record
    _submit_recovery["recovered_resting"] = int(_submit_recovery["recovered_resting"]) + 1  # type: ignore[arg-type]
    _submit_recovery["recovered_total"] = int(_submit_recovery["recovered_total"]) + 1  # type: ignore[arg-type]
    _submit_recovery["last_outcome"] = "recovered_resting"
    await _record(
        req, paper=False, state="resting",
        price=req.limit_price_cents or 0,
        extra={
            "reason": "submit_recovered",
            "exchange_order_id": exchange_order_id,
            "venue_status": venue_status or "resting",
            "original_error": err_str,
        },
    )
    await _emit({"event": "resting", "order": record.to_dict()})
    log.info(
        "submit recovery: client_order_id=%s -> resting at venue (exo=%s)",
        req.client_order_id, exchange_order_id,
    )
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

    Kalshi fill messages do not include `client_order_id` — only the exchange
    `order_id` (which we recorded as `exchange_order_id` on the resting record).
    Match on that primarily; fall back to `client_order_id` for completeness.
    """
    payload = (message or {}).get("msg", {})
    if not isinstance(payload, dict):
        return
    if (payload.get("type") or payload.get("channel")) != "fill":
        return
    fill = payload.get("msg") or payload.get("data") or {}
    if not isinstance(fill, dict):
        return
    exch_id = fill.get("order_id") or fill.get("orderId")
    coid = fill.get("client_order_id") or fill.get("clientOrderId")
    rec: OrderRecord | None = None
    async with _resting_lock:
        if coid and coid in _resting:
            rec = _resting.get(coid)
        elif exch_id:
            for r in _resting.values():
                if r.exchange_order_id == exch_id:
                    rec = r
                    break
    if rec is None:
        return
    count = int(fill.get("count") or 0)
    side_price = fill.get("yes_price") if rec.request.side == "yes" else fill.get("no_price")
    price = int(side_price or 0)
    if count <= 0:
        return

    # ---- fill-event idempotency (resolve fill_id BEFORE state mutation) ----
    # Kalshi's WS can re-deliver the same partial fill on reconnect; without
    # a persistent dedupe gate `rec.filled_count` and the rolled-up position
    # would double-count. Use the same fill_id derivation as the alert path
    # (trade_id/fill_id when present, else a hash of the immutable payload).
    from ..notifications import alerts as _alerts
    fill_id = str(fill.get("trade_id") or fill.get("fill_id") or "")
    if not fill_id:
        raw_count = fill.get("count") or 0
        raw_yes = fill.get("yes_price") or ""
        raw_no = fill.get("no_price") or ""
        raw_ts = (
            fill.get("created_time")
            or fill.get("ts")
            or fill.get("timestamp")
            or ""
        )
        fallback = f"{raw_count}|{raw_yes}|{raw_no}|{raw_ts}"
        fill_id = f"raw:{_alerts._hash_reason(fallback)}"

    fresh = await _record_fill_idempotent(
        order_id=rec.order_id, fill_id=fill_id,
        count=count, price_cents=price,
    )
    if not fresh:
        log.info(
            "ws fill replay suppressed: order_id=%s fill_id=%s",
            rec.order_id, fill_id,
        )
        return

    # ---- weighted-average rollup, with overfill cap+warn -------------------
    requested = int(rec.request.count)
    prior_count = int(rec.filled_count)
    prior_avg = int(rec.avg_fill_price_cents or 0)
    remaining = max(0, requested - prior_count)
    accepted = min(count, remaining)
    if count > remaining:
        rec.over_filled = True
        log.warning(
            "GTC over-fill detected: order_id=%s exchange_order_id=%s "
            "requested=%d prior_filled=%d this_fill=%d (accepting=%d, "
            "discarding=%d to avoid double-count)",
            rec.order_id, rec.exchange_order_id,
            requested, prior_count, count, accepted, count - accepted,
        )
    new_count = prior_count + accepted
    if new_count > 0 and accepted > 0:
        # Weighted average across all ACCEPTED partial fills.
        rec.avg_fill_price_cents = int(round(
            (prior_avg * prior_count + price * accepted) / new_count
        ))
    rec.filled_count = new_count
    rec.state = "filled" if rec.filled_count >= requested else "resting"
    if rec.state == "filled":
        async with _resting_lock:
            _resting.pop(rec.order_id, None)

    # ---- single rolled-up position row -------------------------------------
    await _upsert_position_rollup(
        order_id=rec.order_id,
        market_id=rec.request.market_ticker,
        side=rec.request.side,
        vertical=rec.request.vertical,
        quantity=rec.filled_count,
        avg_price_cents=int(rec.avg_fill_price_cents or 0),
        over_filled=rec.over_filled,
        closed=False,
    )

    await _record(rec.request, paper=False, state="filled", price=price,
                  extra={"exchange_order_id": rec.exchange_order_id, "fill": fill,
                         "accepted_count": accepted, "over_filled": rec.over_filled,
                         "rolled_up_avg_price_cents": rec.avg_fill_price_cents,
                         "rolled_up_filled_count": rec.filled_count})
    await _emit({"event": "filled", "order": rec.to_dict()})
    # LIVE-trade alert (Elite-tier per spec). Paper fills never get
    # here — they short-circuit in submit_order().
    try:
        # `fill_id` was already resolved above (with the same raw-field
        # fallback) so the rolled-up dedupe and the alert dedupe share
        # an idempotency key. Replays would have returned early above
        # before reaching here, so this is purely a belt-and-braces
        # guard against the alerts pipeline mis-routing.
        dk = f"{rec.order_id or coid}:{fill_id}"
        if await _alerts.dedupe_or_skip(
            "live_trade", dk, ttl_seconds=7 * 24 * 3600
        ):
            _alerts.live_trade(
                market_ticker=rec.request.market_ticker,
                side=rec.request.side,
                action=rec.request.action,
                contracts=int(rec.request.count),
                price_cents=price,
                order_id=rec.order_id,
                fill_id=fill_id,
                vertical=rec.request.vertical,
            )
    except Exception as e:  # pragma: no cover
        log.warning("live_trade alert dispatch failed: %s", e)


# --- helpers --------------------------------------------------------------

async def _check_slippage_guard(req: OrderRequest, *, settings) -> dict[str, Any] | None:
    """Project worst-case fill against the cached orderbook.

    Returns a metadata dict to attach to the rejection (and log on the
    cycle) when the guard fires; returns None when the order is allowed
    to proceed.

    The guard is a no-op when:
      * the order isn't a limit order (we don't quote a reference price);
      * the request context doesn't include `model_edge_cents`;
      * the cache has no book for the ticker (degraded mode — we don't
        block trading just because WS hasn't delivered a snapshot yet);
      * the configured threshold is 0 (operator-disabled).
    """
    threshold_pct = float(getattr(settings, "slippage_guard_max_edge_pct", 0.0) or 0.0)
    if threshold_pct <= 0.0:
        return None
    if req.type != "limit" or req.limit_price_cents is None:
        return None
    ctx = req.context if isinstance(req.context, dict) else {}
    edge_raw = ctx.get("model_edge_cents")
    if edge_raw is None:
        # Fall back to fractional `model_edge` (probability units, 0..1).
        # 0.04 = 4¢ on a 1¢-100¢ book.
        prob_edge = ctx.get("model_edge")
        if prob_edge is None:
            return None
        try:
            edge_cents = abs(float(prob_edge)) * 100.0
        except (TypeError, ValueError):
            return None
    else:
        try:
            edge_cents = abs(float(edge_raw))
        except (TypeError, ValueError):
            return None
    if edge_cents <= 0:
        return None

    worst, filled = await kalshi_orderbook.orderbook_cache.worst_fill_price(
        req.market_ticker,
        action=req.action,
        side=req.side,
        count=int(req.count),
    )
    # Cache miss → degraded; allow.
    if worst is None and filled == 0:
        snap = await kalshi_orderbook.orderbook_cache.snapshot(req.market_ticker)
        if snap is None:
            return None
        # Book exists but is empty on this side: zero liquidity.
        return {
            "detail": "no liquidity on requested side",
            "market_ticker": req.market_ticker,
            "side": req.side,
            "requested_count": int(req.count),
            "filled_available": 0,
            "model_edge_cents": edge_cents,
            "threshold_pct": threshold_pct,
        }
    if filled < int(req.count):
        # Partial book depth — treat as a liquidity reject too.
        return {
            "detail": (
                f"insufficient depth: book covers {filled} of {req.count}"
            ),
            "market_ticker": req.market_ticker,
            "side": req.side,
            "requested_count": int(req.count),
            "filled_available": int(filled),
            "worst_fill_price_cents": int(worst) if worst is not None else None,
            "model_edge_cents": edge_cents,
            "threshold_pct": threshold_pct,
        }

    limit = int(req.limit_price_cents)
    # Slippage in cents per contract. For a BUY we paid more than we
    # wanted (worst > limit); for a SELL we received less (worst < limit).
    # In both cases the magnitude of "worse-than-limit" is what erodes
    # the model edge. Negative => the book is *better* than our limit
    # (zero slippage).
    if (req.action or "").lower() == "sell":
        slip_cents = max(0, limit - int(worst))
    else:
        slip_cents = max(0, int(worst) - limit)
    erased_pct = (slip_cents / edge_cents) * 100.0 if edge_cents > 0 else 0.0
    if erased_pct > threshold_pct:
        return {
            "detail": (
                f"projected slippage {slip_cents}¢ erases "
                f"{erased_pct:.1f}% of {edge_cents:.2f}¢ edge "
                f"(threshold {threshold_pct:.1f}%)"
            ),
            "market_ticker": req.market_ticker,
            "side": req.side,
            "requested_count": int(req.count),
            "filled_available": int(filled),
            "worst_fill_price_cents": int(worst),
            "limit_price_cents": limit,
            "slippage_cents": slip_cents,
            "model_edge_cents": edge_cents,
            "edge_erased_pct": round(erased_pct, 4),
            "threshold_pct": threshold_pct,
        }
    return None


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
