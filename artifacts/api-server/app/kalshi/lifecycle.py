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
import os
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


# --- stale-resting cancel counters (surfaced on /api/health/snapshot) ----
#
# Orchestrator cycles start with an age-based sweep over resting orders
# (see ``app.orchestrator.cycle._cancel_stale_resting_orders``). These
# counters tell operators how often that sweep fires, how many orders
# it successfully cancels, and how often the venue refuses the cancel
# (which we swallow so the cycle proceeds).
_stale_resting: dict[str, int | str | float | None] = {
    "cancelled": 0,        # total resting orders successfully cancelled
    "cancel_failed": 0,    # cancel call raised — swallowed, cycle continues
    "scans": 0,            # number of cycle-start sweeps invoked
    "last_scan_at": None,
    "last_cancelled_at": None,
    "last_error": None,    # str — last cancel_failed error message
}


def stale_resting_cancelled_state() -> dict[str, Any]:
    """JSON-safe copy of the stale-resting counters for health snapshots."""
    return dict(_stale_resting)


# --- duplicate client_order_id rejection counters (surfaced on /api/health) -
#
# ``submit_order`` short-circuits a second submission that re-uses a
# ``client_order_id`` already resting (or in-flight on another task) — the
# call never reaches the wire and a ``rejected`` lifecycle row is written
# with reason ``duplicate_client_order_id``. These counters give operators
# a read on how often the dedupe gate fires; a sustained non-zero rate is
# an early signal of a buggy upstream caller re-using ids.
_submit_dedupe: dict[str, int | str | float | None] = {
    "rejected": 0,            # total dedupe rejections
    "last_rejected_at": None, # unix timestamp of the most recent rejection
    "last_client_order_id": None,
}


def submit_dedupe_state() -> dict[str, Any]:
    """JSON-safe copy of the dedupe-rejection counters for health snapshots."""
    return dict(_submit_dedupe)


def _reset_submit_dedupe_state() -> None:
    """Test helper — clears every counter."""
    _submit_dedupe.update(
        {
            "rejected": 0,
            "last_rejected_at": None,
            "last_client_order_id": None,
        }
    )


def _record_submit_dedupe_rejection(client_order_id: str) -> None:
    """Called by ``submit_order`` when the in-process dedupe gate fires."""
    import time as _time
    _submit_dedupe["rejected"] = int(_submit_dedupe["rejected"]) + 1  # type: ignore[arg-type]
    _submit_dedupe["last_rejected_at"] = _time.time()
    _submit_dedupe["last_client_order_id"] = str(client_order_id)[:120]


# --- orderbook freshness / fallback counters (surfaced on /api/health) ---
#
# The slippage guard reads the in-process orderbook cache that the WS
# subscriber feeds. Markets the WS hasn't covered yet — or where the
# connection has been silent for a while — leave the cache stale. The
# guard now refuses to silently wave those trades through: it tries a
# one-shot REST orderbook fetch as a fallback, and only falls back to
# degraded-allow when both the cache *and* REST are unusable. These
# counters give operators visibility into how often each path fires so
# WS coverage gaps surface as a metric instead of as silently-skipped
# guard checks.
_orderbook_guard: dict[str, int | str | float | None] = {
    "checks": 0,           # total submissions where the guard ran past early-exits
    "cache_fresh": 0,      # cache had a snapshot newer than max_age
    "rest_fallback": 0,    # cache stale/missing, REST repopulated it
    "rest_failed": 0,      # cache stale/missing, REST also errored
    "degraded_allow": 0,   # neither cache nor REST → allowed without check
    "last_source": None,   # "cache" | "fallback" | "degraded"
    "last_age_seconds": None,
    "last_ticker": None,
    "last_at": None,
    "last_error": None,    # str — last REST fallback error
}


def orderbook_guard_state() -> dict[str, Any]:
    """JSON-safe copy of the orderbook freshness counters for health snapshots."""
    return dict(_orderbook_guard)


def _reset_orderbook_guard_state() -> None:
    """Test helper — clears every counter."""
    _orderbook_guard.update(
        {
            "checks": 0,
            "cache_fresh": 0,
            "rest_fallback": 0,
            "rest_failed": 0,
            "degraded_allow": 0,
            "last_source": None,
            "last_age_seconds": None,
            "last_ticker": None,
            "last_at": None,
            "last_error": None,
        }
    )


def record_stale_resting_scan() -> None:
    """Called by the orchestrator at the start of each sweep."""
    import time as _time
    _stale_resting["scans"] = int(_stale_resting["scans"]) + 1  # type: ignore[arg-type]
    _stale_resting["last_scan_at"] = _time.time()


def record_stale_resting_cancelled(order_id: str) -> None:
    """Called after each successful cancel in the cycle-start sweep."""
    import time as _time
    _stale_resting["cancelled"] = int(_stale_resting["cancelled"]) + 1  # type: ignore[arg-type]
    _stale_resting["last_cancelled_at"] = _time.time()
    log.info("stale resting order cancelled: %s", order_id)


def record_stale_resting_cancel_failed(order_id: str, err: str) -> None:
    """Called when cancel_order raises during the cycle-start sweep."""
    _stale_resting["cancel_failed"] = int(_stale_resting["cancel_failed"]) + 1  # type: ignore[arg-type]
    _stale_resting["last_error"] = f"{order_id}: {err}"[:300]


def _reset_stale_resting_state() -> None:
    """Test helper — clears every counter."""
    _stale_resting.update({
        "cancelled": 0,
        "cancel_failed": 0,
        "scans": 0,
        "last_scan_at": None,
        "last_cancelled_at": None,
        "last_error": None,
    })


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

# client_order_ids that have passed the dedupe gate but have not yet
# settled into `_resting` (or completed for paper). Guarded by
# `_resting_lock` to keep the dedupe check + insert atomic.
_in_flight: set[str] = set()


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
    *, order_id: str, fill_id: str, count: int, price_cents: int,
    source: str = "ws",
    strict: bool = False,
) -> bool:
    """Insert one row into `order_fills`. Returns True iff the row was newly
    inserted (i.e. this fill event is NOT a WebSocket replay).

    Acts as the dedupe gate for `handle_ws_fill`: when Kalshi re-delivers
    the same partial-fill on reconnect, the unique (order_id, fill_id)
    constraint trips and we report False so the caller skips state
    mutation. Returns True on DB error too — failing-open is the safer
    default for the WS path than silently dropping a real fill.

    Pass ``strict=True`` from the operator-driven dead-letter resolve
    path: callers there NEED to know if the write actually persisted
    (otherwise the resolution UPDATE would mark the row resolved with
    no fill recorded). In strict mode the underlying DB exception
    propagates instead of being swallowed.

    ``source`` distinguishes WS-derived fills (``'ws'``, default) from
    operator-claimed manual fills written by the dead-letter resolve
    path (``'operator_manual'``). Uses positional INSERT against the
    column list so this stays in sync regardless of any future column
    additions.
    """
    try:
        rid = await db.fetchval(
            """
            INSERT INTO order_fills (order_id, fill_id, count, price_cents, source)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (order_id, fill_id) DO NOTHING
            RETURNING order_id
            """,
            order_id, fill_id, int(count), int(price_cents), str(source or "ws"),
        )
        return rid is not None
    except Exception as e:
        log.warning("order_fills insert failed (%s/%s): %s", order_id, fill_id, e)
        if strict:
            raise
        return True


def _recovery_attempt_threshold() -> int:
    """Per-order recovery-attempt count that triggers
    ``order_submit_stuck``. Default 5; tunable via
    ``AXIOM_ORDER_RECOVERY_ATTEMPT_THRESHOLD``.
    """
    raw = os.environ.get("AXIOM_ORDER_RECOVERY_ATTEMPT_THRESHOLD")
    try:
        v = int(raw) if raw is not None else 5
        return max(1, v)
    except (TypeError, ValueError):
        return 5


async def _record_recovery_attempt(
    req: "OrderRequest",
    *,
    outcome: str,
    error: str | None,
) -> None:
    """Sidecar row tracking submit-recovery attempts for one
    ``client_order_id``. Called by ``_attempt_submit_recovery`` after
    every branch.

    On a failure outcome (``lookup_failed`` / ``not_found`` /
    ``not_found_malformed``) increments ``recovery_attempts`` and
    fires ``order_submit_stuck`` once when the count first crosses
    :func:`_recovery_attempt_threshold`. Subsequent attempts re-cross
    the threshold but are absorbed by the dedupe gate keyed on the
    ``client_order_id``.

    On a success outcome (``recovered_*``) marks any open row
    ``resolved_at = now()`` so a coid that flapped between failure
    and recovery never lands in the dead-letter queue.

    Best-effort: fails open on every DB error so the recovery path
    keeps running even if the sidecar table is unavailable.
    """
    coid = req.client_order_id
    failure_outcomes = {"lookup_failed", "not_found", "not_found_malformed"}
    success_outcomes = {
        "recovered_canceled", "recovered_filled", "recovered_resting",
    }
    try:
        if outcome in success_outcomes:
            # Natural recovery on a non-dead-lettered row → resolve.
            # Natural recovery on a DEAD-LETTERED row → record the
            # outcome but DO NOT auto-resolve. Once the operator was
            # paged, only an explicit click closes the loop — otherwise
            # the row would silently disappear from the operator
            # console mid-investigation, leaving them confused about
            # whether the order was reconciled or whether a duplicate
            # manual fill is needed.
            await db.execute(
                """
                UPDATE order_recovery_attempts
                   SET resolved_at           = CASE
                                                  WHEN dead_lettered_at IS NULL
                                                  THEN now()
                                                  ELSE resolved_at
                                               END,
                       resolution_action     = CASE
                                                  WHEN dead_lettered_at IS NULL
                                                  THEN $2
                                                  ELSE resolution_action
                                               END,
                       last_recovery_at      = now(),
                       last_recovery_outcome = $2,
                       updated_at            = now()
                 WHERE client_order_id = $1
                   AND resolved_at     IS NULL
                """,
                coid, outcome,
            )
            return
        if outcome not in failure_outcomes:
            return
        row = await db.fetchrow(
            """
            INSERT INTO order_recovery_attempts (
                client_order_id, market_ticker, side, action,
                contracts, limit_price_cents, vertical,
                recovery_attempts, last_recovery_at, last_recovery_outcome,
                last_error
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, 1, now(), $8, $9
            )
            ON CONFLICT (client_order_id) DO UPDATE
               SET recovery_attempts     = CASE
                                              WHEN order_recovery_attempts.resolved_at IS NOT NULL
                                              THEN 1
                                              ELSE order_recovery_attempts.recovery_attempts + 1
                                           END,
                   first_failed_at       = CASE
                                              WHEN order_recovery_attempts.resolved_at IS NOT NULL
                                              THEN now()
                                              ELSE order_recovery_attempts.first_failed_at
                                           END,
                   resolved_at           = NULL,
                   resolution_action     = NULL,
                   resolver_email        = NULL,
                   resolution_reason     = NULL,
                   fill_price_cents      = NULL,
                   dead_lettered_at      = NULL,
                   last_recovery_at      = now(),
                   last_recovery_outcome = EXCLUDED.last_recovery_outcome,
                   last_error            = EXCLUDED.last_error,
                   updated_at            = now()
         RETURNING recovery_attempts, market_ticker, side, action,
                   contracts, limit_price_cents, vertical,
                   last_recovery_outcome, last_error
            """,
            coid, req.market_ticker, req.side, req.action, int(req.count),
            (
                int(req.limit_price_cents)
                if req.limit_price_cents is not None
                else None
            ),
            req.vertical, outcome, (error or "")[:500],
        )
    except Exception as e:  # noqa: BLE001 — fail open
        log.warning(
            "order_recovery_attempts upsert failed for %s: %s", coid, e,
        )
        return
    if not row:
        return

    threshold = _recovery_attempt_threshold()
    attempts = int(row["recovery_attempts"])
    if attempts < threshold:
        return
    snapshot = {
        "client_order_id": coid,
        "market_ticker": row["market_ticker"],
        "side": row["side"],
        "action": row["action"],
        "contracts": int(row["contracts"]),
        "limit_price_cents": (
            int(row["limit_price_cents"])
            if row["limit_price_cents"] is not None
            else None
        ),
        "vertical": row["vertical"],
        "recovery_attempts": attempts,
        "threshold": threshold,
        "last_recovery_outcome": row["last_recovery_outcome"],
        "last_error": row["last_error"],
    }
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_order_submit_stuck_alert(snapshot))
    except RuntimeError:  # pragma: no cover
        await _send_order_submit_stuck_alert(snapshot)


async def _persist_order_submit_stuck_health_alert(snap: dict[str, Any]) -> None:
    """Persist a row in ``model_health_alerts`` so the operator
    dashboard surfaces a stuck order alongside other model-health
    signals (Brier drift, position drift, settlement drift, etc.).

    Hourly dedupe on ``(alert_type, metric_name)`` so a sustained
    outage that re-crosses the threshold repeatedly doesn't flood
    the table.

    Best-effort: any DB error is swallowed — the threshold paging
    via :func:`_send_order_submit_stuck_alert` is the source of truth
    for operator notification; this row is a redundant dashboard
    surface.
    """
    coid = str(snap.get("client_order_id") or "")
    if not coid:
        return
    attempts = int(snap.get("recovery_attempts") or 0)
    threshold = int(snap.get("threshold") or 0)
    ticker = str(snap.get("market_ticker") or "")
    vertical = str(snap.get("vertical") or "trading")
    try:
        recent = await db.fetchrow(
            """
            SELECT 1 FROM model_health_alerts
             WHERE alert_type   = 'order_submit_stuck'
               AND metric_name  = $1
               AND created_at   > now() - interval '1 hour'
             LIMIT 1
            """,
            coid,
        )
        if recent:
            return
        await db.execute(
            """
            INSERT INTO model_health_alerts
                (vertical, alert_type, severity, metric_name,
                 metric_value, threshold, message, acknowledged)
            VALUES ($1, 'order_submit_stuck', 'warn', $2,
                    $3, $4, $5, false)
            ON CONFLICT (id) DO NOTHING
            """,
            vertical,
            coid,
            float(attempts),
            float(threshold),
            (
                f"Order {coid} stuck after {attempts} recovery "
                f"attempts (threshold={threshold}, ticker={ticker})"
            ),
        )
    except Exception as e:  # pragma: no cover — dashboard surface is best-effort
        log.warning(
            "order_submit_stuck health-alert insert failed for %s: %s",
            coid, e,
        )


async def _send_order_submit_stuck_alert(snap: dict[str, Any]) -> None:
    """Off-hot-path dispatcher for ``order_submit_stuck``. Deduped on
    ``client_order_id`` with a 7-day TTL so a sustained outage pages
    once per coid, not once per recovery tick.

    Also persists a ``model_health_alerts`` dashboard row (1-hour
    dedupe, best-effort) so the operator console surfaces the stuck
    order alongside other model-health signals.
    """
    from ..notifications import alerts as _alerts
    try:
        coid = str(snap.get("client_order_id") or "")
        if not coid:
            return
        ok = await _alerts.dedupe_or_skip(
            "order_submit_stuck", coid, ttl_seconds=7 * 24 * 3600,
        )
        if not ok:
            return
        # Dashboard surface — independent of the email dedupe gate so
        # the row appears even if the email path is gated/disabled.
        await _persist_order_submit_stuck_health_alert(snap)
        _alerts.order_submit_stuck(
            client_order_id=coid,
            market_ticker=str(snap.get("market_ticker") or ""),
            side=str(snap.get("side") or ""),
            action=str(snap.get("action") or ""),
            contracts=int(snap.get("contracts") or 0),
            threshold=int(snap.get("threshold") or 0),
            limit_price_cents=snap.get("limit_price_cents"),
            vertical=snap.get("vertical"),
            recovery_attempts=int(snap.get("recovery_attempts") or 0),
            last_recovery_outcome=snap.get("last_recovery_outcome"),
            last_error=snap.get("last_error"),
        )
    except Exception as e:  # pragma: no cover — alert must not break recovery
        log.warning("order_submit_stuck dispatch failed: %s", e)


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
    strict: bool = False,
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
        if strict:
            raise


# --- main entry ----------------------------------------------------------

class OrderRejected(RuntimeError):
    pass


async def submit_order(req: OrderRequest, *, force_paper: bool | None = None) -> OrderRecord:
    """Submit an order through the lifecycle.

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

    # ---- pre-submit orderbook liquidity / slippage guard -----------------
    # When the request carries `model_edge_cents` in its context, project the
    # worst-case fill against the cached WS orderbook. If projected slippage
    # would erase more than `slippage_guard_max_edge_pct` of the model edge,
    # reject before submission with reason=`slippage_guard`.
    guard = await _check_slippage_guard(req, settings=s)
    if guard is not None:
        slice_count = int(guard.get("slice_count") or 0)
        if slice_count > 0 and slice_count < int(req.count):
            # Don't reject — submit a smaller order that fits within the
            # configured edge-erosion budget. Stash slice provenance into
            # the request context so the existing `submitted` lifecycle
            # row records "filled X of Y" instead of a silent shrink.
            from dataclasses import replace as _dc_replace
            original_count = int(req.count)
            new_context = dict(req.context) if isinstance(req.context, dict) else {}
            new_context["sliced_by_slippage_guard"] = {
                "original_count": original_count,
                "sliced_count": slice_count,
                "reason": "slippage_guard_partial_depth",
                "detail": guard.get("detail"),
                "worst_acceptable_price_cents": guard.get("worst_acceptable_price_cents"),
                "model_edge_cents": guard.get("model_edge_cents"),
                "threshold_pct": guard.get("threshold_pct"),
                "limit_price_cents": guard.get("limit_price_cents"),
                "original_worst_fill_price_cents": guard.get("original_worst_fill_price_cents"),
                "sliced_worst_fill_price_cents": guard.get("sliced_worst_fill_price_cents"),
                "sliced_slippage_cents": guard.get("sliced_slippage_cents"),
                "edge_erased_pct": guard.get("edge_erased_pct"),
            }
            req = _dc_replace(req, count=slice_count, context=new_context)
            log.info(
                "slippage guard sliced order %s on %s: %d → %d (%s)",
                req.client_order_id, req.market_ticker,
                original_count, slice_count, guard.get("detail"),
            )
        else:
            await _record(req, paper=paper, state="rejected",
                          price=req.limit_price_cents or 0,
                          extra={"reason": "slippage_guard", **guard})
            raise OrderRejected(
                f"slippage guard: {guard.get('detail') or 'rejected'}"
            )

    # ---- in-process duplicate-client_order_id gate ----------------------
    # A second submission re-using a client_order_id that's already resting
    # (or in-flight on another task) would burn a signed REST call and get
    # bounced by Kalshi with a 4xx. Catch it here so the rejection is fast,
    # cheap, and shows up in the lifecycle audit trail before the wire.
    async with _resting_lock:
        if req.client_order_id in _resting or req.client_order_id in _in_flight:
            duplicate = True
        else:
            _in_flight.add(req.client_order_id)
            duplicate = False
    if duplicate:
        _record_submit_dedupe_rejection(req.client_order_id)
        await _record(
            req, paper=paper, state="rejected",
            price=req.limit_price_cents or 0,
            extra={"reason": "duplicate_client_order_id"},
        )
        await _emit({
            "event": "rejected",
            "order": {
                "order_id": req.client_order_id,
                "paper": paper,
                "state": "rejected",
                "request": {**asdict(req)},
            },
            "reason": "duplicate_client_order_id",
        })
        raise OrderRejected(
            f"duplicate client_order_id: {req.client_order_id}"
        )

    try:
        return await _submit_after_dedupe(req, paper=paper, settings=s)
    finally:
        async with _resting_lock:
            _in_flight.discard(req.client_order_id)


async def _submit_after_dedupe(
    req: OrderRequest, *, paper: bool, settings,
) -> OrderRecord:
    s = settings
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
        await _record_recovery_attempt(
            req, outcome="lookup_failed",
            error=f"{type(lookup_exc).__name__}: {lookup_exc}",
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
        await _record_recovery_attempt(
            req, outcome="not_found", error=err_str,
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
        await _record_recovery_attempt(
            req, outcome="not_found_malformed",
            error=(
                f"malformed venue order: missing exchange id and unknown "
                f"status {venue_status!r}"
            ),
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
        await _record_recovery_attempt(
            req, outcome="recovered_canceled", error=None,
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
        await _record_recovery_attempt(
            req, outcome="recovered_filled", error=None,
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
    await _record_recovery_attempt(
        req, outcome="recovered_resting", error=None,
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
    #
    # Fully off the hot path: the dedupe lookup is a DB round-trip
    # (`notification_dedupe` UPSERT) that can stall under DB pressure.
    # We capture an immutable snapshot and schedule a background task
    # so the WS fill handler can return promptly and process the next
    # fill without backlog. Mirrors the per-user trade-confirmation
    # treatment immediately below.
    try:
        live_trade_snapshot = {
            "dedupe_key": f"{rec.order_id or coid}:{fill_id}",
            "market_ticker": rec.request.market_ticker,
            "side": rec.request.side,
            "action": rec.request.action,
            "contracts": int(rec.request.count),
            "price_cents": int(price),
            "order_id": rec.order_id,
            "fill_id": fill_id,
            "vertical": rec.request.vertical,
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                _send_live_trade_alert_from_snapshot(live_trade_snapshot)
            )
        except RuntimeError:  # pragma: no cover
            # No running loop (unlikely from handle_ws_fill). Run
            # inline as a last resort.
            await _send_live_trade_alert_from_snapshot(live_trade_snapshot)
    except Exception as e:  # pragma: no cover
        log.warning("live_trade alert dispatch failed: %s", e)

    # Operator branded HTML companion (task #712). Independent failure
    # boundary from the plain-text firehose above — a render error in
    # the rich email must NEVER swallow the firehose alert, and vice
    # versa. The dedupe gate is per-recipient inside the helper, so a
    # partial-fill burst on the same fill_id collapses to one branded
    # email per operator.
    #
    # `contracts` is the actual ACCEPTED fill quantity for THIS WS event,
    # not the requested order size — partial fills on a 100-contract
    # order land in the operator's inbox showing the executed slice
    # (e.g. "BUY YES 25x") so on-call triage isn't misled by
    # `rec.request.count` which would be the unrelated total order.
    try:
        operator_alert_snapshot = {
            "market_ticker": rec.request.market_ticker,
            "side": rec.request.side,
            "action": rec.request.action,
            "contracts": int(accepted),
            "price_cents": int(price),
            "order_id": rec.order_id,
            "fill_id": fill_id,
            "vertical": rec.request.vertical,
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                _send_operator_trade_alert_from_snapshot(operator_alert_snapshot)
            )
        except RuntimeError:  # pragma: no cover
            await _send_operator_trade_alert_from_snapshot(operator_alert_snapshot)
    except Exception as e:  # pragma: no cover
        log.warning("operator_trade_alert dispatch failed: %s", e)

    # ---- per-user HTML trade-confirmation email (task #269) -----------
    # Distinct from the operator-broadcast `live_trade` alert above.
    # Fully off the hot path: we capture an immutable snapshot of the
    # fill and schedule a background task that does the DB recipient
    # lookup, optional digest aggregation, market-title resolution, and
    # dispatch. `handle_ws_fill` must return promptly so the WS router
    # can process the next fill without backlog.
    try:
        ctx = rec.request.context if isinstance(rec.request.context, dict) else {}
        _user_id = ctx.get("user_id") or ctx.get("clerk_user_id")
        if _user_id:
            snapshot = {
                "user_id": str(_user_id),
                "order_id": rec.order_id,
                "fill_id": fill_id,
                "price_cents": int(price),
                "accepted_count": int(accepted),
                "market_ticker": rec.request.market_ticker,
                "side": rec.request.side,
                "action": rec.request.action,
                "vertical": rec.request.vertical,
                "state": rec.state,
                "filled_count": int(rec.filled_count),
                "avg_fill_price_cents": int(rec.avg_fill_price_cents or 0),
            }
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_send_trade_confirmation_from_snapshot(snapshot))
            except RuntimeError:  # pragma: no cover
                # No running loop (unlikely from handle_ws_fill). Run
                # inline as a last resort — still bounded by timeouts.
                await _send_trade_confirmation_from_snapshot(snapshot)
    except Exception as e:  # pragma: no cover
        log.warning("trade_confirmation dispatch failed: %s", e)


async def _send_live_trade_alert_from_snapshot(snap: dict[str, Any]) -> None:
    """Background worker: claim the operator-broadcast `live_trade`
    dedupe key and dispatch the alert. Completely off the WS fill
    hot path so the dedupe DB round-trip cannot stall trade handling.

    Every failure is logged and swallowed — a failed alert must never
    surface upstream into the WS fill loop.
    """
    from ..notifications import alerts as _alerts
    try:
        if await _alerts.dedupe_or_skip(
            "live_trade",
            str(snap["dedupe_key"]),
            ttl_seconds=7 * 24 * 3600,
        ):
            _alerts.live_trade(
                market_ticker=snap["market_ticker"],
                side=snap["side"],
                action=snap["action"],
                contracts=int(snap["contracts"]),
                price_cents=snap["price_cents"],
                order_id=snap.get("order_id"),
                fill_id=snap.get("fill_id"),
                vertical=snap.get("vertical"),
            )
    except Exception as e:  # pragma: no cover
        log.warning("live_trade alert dispatch failed: %s", e)


async def _send_operator_trade_alert_from_snapshot(snap: dict[str, Any]) -> None:
    """Background worker: dispatch the branded HTML operator alert
    (task #712). Independent failure boundary from the plain-text
    firehose — a render error or DB hiccup here must NEVER surface
    upstream and must NEVER block the firehose from going out.

    All context fetches (market title, position-after-fill, vertical
    PnL, gate audit) happen inside ``operator_trade_alert`` and
    fail-soft to empty blocks.
    """
    from ..notifications import alerts as _alerts
    try:
        await _alerts.operator_trade_alert(
            market_ticker=snap["market_ticker"],
            side=snap["side"],
            action=snap["action"],
            contracts=int(snap["contracts"]),
            price_cents=int(snap["price_cents"]),
            order_id=str(snap.get("order_id") or ""),
            fill_id=str(snap.get("fill_id") or ""),
            vertical=snap.get("vertical"),
        )
    except Exception as e:  # pragma: no cover
        log.warning("operator_trade_alert worker failed: %s", e)


async def _resolve_trade_confirmation_recipient(
    user_id: str,
) -> tuple[str | None, bool, bool]:
    """Return ``(email, email_trade_confirmation, digest_partials)`` for
    a user.

    Missing prefs rows fall back to the PREF_COLUMNS defaults (email on,
    digest off). Missing/empty email yields ``(None, ...)`` so the caller
    can short-circuit.
    """
    from ..notifications import push as _push
    default_conf = "true" if _push.PREF_COLUMNS["email_trade_confirmation"] else "false"
    default_digest = "true" if _push.PREF_COLUMNS["digest_partials"] else "false"
    try:
        row = await db.fetchrow(
            f"""
            SELECT u.email,
                   COALESCE(p.email_trade_confirmation, {default_conf}) AS conf,
                   COALESCE(p.digest_partials,          {default_digest}) AS digest
              FROM app_users u
              LEFT JOIN user_notification_prefs p
                     ON p.clerk_user_id = u.clerk_user_id
             WHERE u.clerk_user_id = $1
            """,
            user_id,
        )
    except Exception as e:
        log.warning("trade_confirmation recipient lookup failed: %s", e)
        return (None, False, False)
    if not row:
        return (None, False, False)
    email = row["email"]
    conf = bool(row["conf"]) if row["conf"] is not None else bool(
        _push.PREF_COLUMNS["email_trade_confirmation"]
    )
    digest = bool(row["digest"]) if row["digest"] is not None else bool(
        _push.PREF_COLUMNS["digest_partials"]
    )
    return (
        (str(email).strip() or None) if email else None,
        conf,
        digest,
    )


async def _aggregate_order_fills(order_id: str) -> tuple[int, int, int]:
    """Sum all fills for ``order_id``. Returns
    ``(total_contracts, weighted_avg_price_cents, fill_count)``. When
    no rows exist (shouldn't happen post-insert) returns zeros.
    """
    try:
        rows = await db.fetch(
            "SELECT count, price_cents FROM order_fills "
            "WHERE order_id = $1",
            order_id,
        )
    except Exception as e:
        log.warning("order_fills aggregate failed for %s: %s", order_id, e)
        return (0, 0, 0)
    total_qty = 0
    total_cents = 0
    n = 0
    for r in rows or []:
        c = int(r["count"] or 0)
        p = int(r["price_cents"] or 0)
        total_qty += c
        total_cents += c * p
        n += 1
    avg = int(round(total_cents / total_qty)) if total_qty > 0 else 0
    return (total_qty, avg, n)


_TRADE_CONFIRMATION_LOOKUP_TIMEOUT_S = 3.0
_TRADE_CONFIRMATION_TITLE_TIMEOUT_S = 2.5


async def _send_trade_confirmation_from_snapshot(snap: dict[str, Any]) -> None:
    """Background worker: enrich the fill snapshot and dispatch the
    confirmation email. Completely off the WS fill hot path.

    All external-ish calls (DB lookups, Kalshi title API) are wrapped
    in short timeouts so notification enrichment cannot accumulate
    backlog under dependency slowness. Every failure is logged and
    swallowed — a failed email must never surface upstream.
    """
    from ..notifications import alerts as _alerts
    from . import market_titles as _titles

    user_id = str(snap["user_id"])
    order_id = str(snap["order_id"])
    fill_id = str(snap["fill_id"])

    try:
        email, wants_email, digest = await asyncio.wait_for(
            _resolve_trade_confirmation_recipient(user_id),
            timeout=_TRADE_CONFIRMATION_LOOKUP_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning(
            "trade_confirmation recipient lookup timed out user=%s", user_id,
        )
        return
    except Exception as e:  # pragma: no cover
        log.warning("trade_confirmation recipient lookup failed: %s", e)
        return

    if not wants_email:
        return
    if not email:
        log.warning(
            "trade_confirmation: user=%s has no email on file; skipping",
            user_id,
        )
        return

    partial = snap["state"] != "filled"
    if digest and partial:
        log.debug(
            "trade_confirmation deferred (digest_partials) user=%s order=%s",
            user_id, order_id,
        )
        return

    if digest:
        try:
            total_qty, avg_price, fills_summed = await asyncio.wait_for(
                _aggregate_order_fills(order_id),
                timeout=_TRADE_CONFIRMATION_LOOKUP_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning(
                "trade_confirmation digest aggregate timed out order=%s",
                order_id,
            )
            total_qty, avg_price, fills_summed = (0, 0, 0)
        if total_qty <= 0:
            total_qty = int(snap["filled_count"])
            avg_price = int(snap["avg_fill_price_cents"] or 0)
            fills_summed = 1
        confirm_fill_id = "digest"
        contracts_for_email = total_qty
        avg_for_email = avg_price
    else:
        fills_summed = None
        confirm_fill_id = fill_id
        contracts_for_email = int(snap["accepted_count"])
        avg_for_email = int(snap["price_cents"])

    total_cost = avg_for_email * contracts_for_email

    try:
        market_title = await asyncio.wait_for(
            _titles.get_title(snap["market_ticker"]),
            timeout=_TRADE_CONFIRMATION_TITLE_TIMEOUT_S,
        )
    except Exception:
        market_title = None

    _alerts.trade_confirmation(
        user_id=user_id,
        email=email,
        market_ticker=snap["market_ticker"],
        market_title=market_title or snap["market_ticker"],
        side=snap["side"],
        action=snap["action"],
        contracts=contracts_for_email,
        avg_price_cents=avg_for_email,
        total_cost_cents=total_cost,
        current_position=int(snap["filled_count"]),
        order_id=order_id,
        fill_id=confirm_fill_id,
        vertical=snap["vertical"],
        partial=partial,
        digest=digest,
        fills_summed=fills_summed,
    )


# --- helpers --------------------------------------------------------------

def _record_orderbook_guard_outcome(
    *,
    source: str,
    age: float | None,
    ticker: str,
    error: str | None = None,
) -> None:
    """Bump the per-source counters that /api/health surfaces.

    ``source`` is one of ``"cache"`` (WS view was fresh enough),
    ``"fallback"`` (WS view was stale/missing but the REST one-shot
    repopulated it), or ``"degraded"`` (neither path produced data —
    the trade was waved through without a guard check).
    """
    import time as _time
    _orderbook_guard["checks"] = int(_orderbook_guard["checks"]) + 1  # type: ignore[arg-type]
    if source == "cache":
        _orderbook_guard["cache_fresh"] = int(_orderbook_guard["cache_fresh"]) + 1  # type: ignore[arg-type]
    elif source == "fallback":
        _orderbook_guard["rest_fallback"] = int(_orderbook_guard["rest_fallback"]) + 1  # type: ignore[arg-type]
    elif source == "degraded":
        _orderbook_guard["degraded_allow"] = int(_orderbook_guard["degraded_allow"]) + 1  # type: ignore[arg-type]
    _orderbook_guard["last_source"] = source
    _orderbook_guard["last_age_seconds"] = float(age) if age is not None else None
    _orderbook_guard["last_ticker"] = ticker
    _orderbook_guard["last_at"] = _time.time()
    if error is not None:
        _orderbook_guard["last_error"] = str(error)[:300]


async def _ensure_fresh_orderbook(
    ticker: str, *, settings,
) -> tuple[str, float | None]:
    """Make sure the in-process cache has a recent snapshot for ``ticker``.

    Returns ``(source, age_seconds)`` where ``source`` is:

      * ``"cache"``    — cache already had data newer than
        ``kalshi_orderbook_max_age_secs``; nothing was fetched.
      * ``"fallback"`` — cache was stale or missing, so we issued a
        one-shot REST orderbook fetch and applied it as a snapshot.
      * ``"degraded"`` — cache was unusable AND the REST fallback was
        either disabled, missing credentials, or errored. The caller
        must skip the guard (degraded-allow) and the counter is
        bumped so operators can see the gap.

    This function records the per-source counters before returning, so
    callers can short-circuit on ``"degraded"`` without forgetting to
    update health metrics.
    """
    max_age = float(getattr(settings, "kalshi_orderbook_max_age_secs", 5.0) or 0.0)
    age = await kalshi_orderbook.orderbook_cache.age_seconds(ticker)
    if age is not None and (max_age <= 0 or age <= max_age):
        _record_orderbook_guard_outcome(source="cache", age=age, ticker=ticker)
        return "cache", age

    rest_enabled = bool(
        getattr(settings, "kalshi_orderbook_rest_fallback_enabled", True)
    )
    has_creds = bool(
        getattr(settings, "kalshi_api_key_id", None)
        and getattr(settings, "kalshi_private_key", None)
    )
    if not rest_enabled or not has_creds:
        _record_orderbook_guard_outcome(
            source="degraded",
            age=age,
            ticker=ticker,
            error=None if rest_enabled else "rest_fallback_disabled",
        )
        return "degraded", age

    try:
        async with kalshi_client.KalshiClient(settings=settings) as c:
            data = await c.get_orderbook(ticker)
    except Exception as e:  # noqa: BLE001 — network/auth errors degrade
        _orderbook_guard["rest_failed"] = int(_orderbook_guard["rest_failed"]) + 1  # type: ignore[arg-type]
        err = f"{type(e).__name__}: {e}"[:300]
        log.warning(
            "orderbook REST fallback failed for %s: %s — degraded-allow",
            ticker, err,
        )
        _record_orderbook_guard_outcome(
            source="degraded", age=age, ticker=ticker, error=err,
        )
        return "degraded", age

    body = (data or {}).get("orderbook") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        body = data if isinstance(data, dict) else {}
    yes_levels = body.get("yes") or body.get("yes_levels")
    no_levels = body.get("no") or body.get("no_levels")
    await kalshi_orderbook.orderbook_cache.apply_snapshot(
        ticker, yes=yes_levels, no=no_levels,
    )
    new_age = await kalshi_orderbook.orderbook_cache.age_seconds(ticker)
    _record_orderbook_guard_outcome(
        source="fallback", age=new_age, ticker=ticker,
    )
    return "fallback", new_age


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

    import math

    requested = int(req.count)
    action = (req.action or "").lower()
    limit = int(req.limit_price_cents)

    # Make sure the cache has fresh data before we trust it. If the WS
    # hasn't pushed a snapshot for this ticker, or the last update is
    # older than ``kalshi_orderbook_max_age_secs``, fall back to a
    # one-shot REST orderbook fetch. Only when *both* paths fail do we
    # degraded-allow — and we count it so operators see WS gaps.
    source, _age = await _ensure_fresh_orderbook(req.market_ticker, settings=settings)
    if source == "degraded":
        return None

    worst, filled = await kalshi_orderbook.orderbook_cache.worst_fill_price(
        req.market_ticker,
        action=action,
        side=req.side,
        count=requested,
    )

    # Cache miss vs empty-side book — only the former is degraded-allow.
    # Cache miss should be unreachable after a successful fresh-ensure
    # but defended for race-condition safety (cache could have been
    # cleared between ensure and read).
    book_known = True
    if worst is None and filled == 0:
        snap = await kalshi_orderbook.orderbook_cache.snapshot(req.market_ticker)
        if snap is None:
            _record_orderbook_guard_outcome(
                source="degraded", age=None, ticker=req.market_ticker,
            )
            return None  # degraded — let the order through
        book_known = False  # book exists but the requested side is empty

    # Full pass: book covers the whole order AND projected slippage stays
    # within the configured threshold. Short-circuit before doing slice math.
    if book_known and filled >= requested:
        if action == "sell":
            slip_cents = max(0, limit - int(worst))
        else:
            slip_cents = max(0, int(worst) - limit)
        erased_pct = (slip_cents / edge_cents) * 100.0 if edge_cents > 0 else 0.0
        if erased_pct <= threshold_pct:
            return None

    # Either the book is too thin OR the full-size walk pushes past the
    # edge-erosion threshold. Compute the largest slice that still
    # respects the budget by walking only price levels whose taker-units
    # cost stays within `worst_acceptable`.
    max_slip_cents = (threshold_pct / 100.0) * edge_cents
    if action == "sell":
        # SELL: receive >= floor. Use ceil so partial-cent budget can't push us
        # below the floor (e.g. limit=60, max_slip=2.5 → floor=58, not 57).
        worst_acceptable = int(math.ceil(limit - max_slip_cents))
    else:
        # BUY: pay <= ceiling. Use floor for the symmetric reason.
        worst_acceptable = int(math.floor(limit + max_slip_cents))

    slice_count = 0
    if book_known:
        slice_count = await kalshi_orderbook.orderbook_cache.fillable_within_price(
            req.market_ticker,
            action=action,
            side=req.side,
            count=requested,
            worst_acceptable_price_cents=worst_acceptable,
        )

    common = {
        "market_ticker": req.market_ticker,
        "side": req.side,
        "requested_count": requested,
        "filled_available": int(filled),
        "limit_price_cents": limit,
        "model_edge_cents": edge_cents,
        "threshold_pct": threshold_pct,
        "worst_acceptable_price_cents": worst_acceptable,
        "original_worst_fill_price_cents": int(worst) if worst is not None else None,
    }

    if slice_count <= 0:
        # Nothing fits inside the acceptable price band — full reject.
        if not book_known:
            common["detail"] = "no liquidity on requested side"
        elif filled <= 0:
            common["detail"] = "no liquidity on requested side at any price"
        else:
            common["detail"] = (
                f"book covers {filled} of {requested} but every level "
                f"violates worst-acceptable price {worst_acceptable}¢"
            )
        return common

    if slice_count >= requested:
        # Defensive — full coverage at acceptable prices means we should
        # not have reached this branch. Allow the order through.
        return None

    # Project the worst-case price for the slice so dashboards see the
    # actual fill envelope, not the full-size projection.
    sliced_worst, _sf = await kalshi_orderbook.orderbook_cache.worst_fill_price(
        req.market_ticker,
        action=action,
        side=req.side,
        count=int(slice_count),
    )
    if sliced_worst is None:
        # Should be unreachable given slice_count > 0, but stay safe.
        sliced_slip = 0
        sliced_erased = 0.0
    elif action == "sell":
        sliced_slip = max(0, limit - int(sliced_worst))
        sliced_erased = (sliced_slip / edge_cents) * 100.0 if edge_cents > 0 else 0.0
    else:
        sliced_slip = max(0, int(sliced_worst) - limit)
        sliced_erased = (sliced_slip / edge_cents) * 100.0 if edge_cents > 0 else 0.0

    common.update({
        "slice_count": int(slice_count),
        "sliced_worst_fill_price_cents": int(sliced_worst) if sliced_worst is not None else None,
        "sliced_slippage_cents": int(sliced_slip),
        "edge_erased_pct": round(sliced_erased, 4),
        "detail": (
            f"sliced {slice_count} of {requested} contracts to keep "
            f"slippage ≤ {threshold_pct:.1f}% of {edge_cents:.2f}¢ edge "
            f"(worst now {sliced_slip}¢ = {sliced_erased:.1f}%)"
        ),
    })
    return common


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
