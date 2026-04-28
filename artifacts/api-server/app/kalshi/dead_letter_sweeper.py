"""Periodic sweeper that promotes long-failing orders into the
dead-letter queue.

A row in ``order_recovery_attempts`` is created the first time
``app.kalshi.lifecycle._attempt_submit_recovery`` runs against a
``client_order_id``.  After that, every recovery attempt updates the
row in place — so a coid that has been failing recovery for an hour
shows up as a single row with ``recovery_attempts >> 0`` and a
``first_failed_at`` from the original failure.

This sweeper hits the row when ``first_failed_at`` is older than the
configured threshold (default 600s) and the operator hasn't resolved
it yet. It marks the row as dead-lettered and fires an
``order_dead_lettered`` operator alert (deduped on the coid so a
sustained outage pages once, not once per sweep). The sweep itself
is idempotent — a dead-lettered row is filtered out of the scan and
the alert dedupe gate covers the unlikely case where the sweep races
with itself.

Skipped entirely in paper mode: paper submits never go through the
recovery path, so the queue is always empty and there's nothing to
do. Skipping avoids waking the DB on a system that doesn't trade
live.

Per task #710.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .. import db
from ..config import get_settings


log = logging.getLogger(__name__)


# --- env-tunable knobs ---------------------------------------------------

def _sweep_interval_secs() -> float:
    raw = os.environ.get("AXIOM_ORDER_DEAD_LETTER_SWEEP_SECS")
    try:
        v = float(raw) if raw is not None else 60.0
        return v if v > 0 else 60.0
    except (TypeError, ValueError):
        return 60.0


def _dead_letter_after_secs() -> float:
    raw = os.environ.get("AXIOM_ORDER_DEAD_LETTER_AFTER_SECS")
    try:
        v = float(raw) if raw is not None else 600.0
        return v if v > 0 else 600.0
    except (TypeError, ValueError):
        return 600.0


# --- counters (surfaced via ``state()`` for /api/health/snapshot) --------

_state: dict[str, int | str | float | None] = {
    "scans": 0,
    "marked": 0,
    "errors": 0,
    "last_scan_at": None,
    "last_marked_at": None,
    "last_error": None,
}


def state() -> dict[str, Any]:
    """JSON-safe copy of the sweeper counters."""
    return dict(_state)


def _reset_state() -> None:
    """Test helper — clears every counter."""
    _state.update(
        {
            "scans": 0,
            "marked": 0,
            "errors": 0,
            "last_scan_at": None,
            "last_marked_at": None,
            "last_error": None,
        }
    )


# --- sweeper loop --------------------------------------------------------

async def sweep_once() -> list[dict[str, Any]]:
    """Mark every row eligible for dead-lettering. Returns the rows that
    were just marked (so the caller can fire alerts).

    Idempotent and safe to run concurrently — the UPDATE filters on
    ``dead_lettered_at IS NULL`` so a second invocation sees zero rows
    to mark.
    """
    import time as _time
    _state["scans"] = int(_state["scans"]) + 1  # type: ignore[arg-type]
    _state["last_scan_at"] = _time.time()

    threshold_secs = _dead_letter_after_secs()
    try:
        rows = await db.fetch(
            """
            UPDATE order_recovery_attempts
               SET dead_lettered_at = now(),
                   updated_at       = now()
             WHERE dead_lettered_at IS NULL
               AND resolved_at      IS NULL
               AND first_failed_at  < now() - ($1::float || ' seconds')::interval
         RETURNING client_order_id, market_ticker, side, action, contracts,
                   limit_price_cents, vertical, recovery_attempts,
                   last_recovery_outcome, last_error,
                   first_failed_at, dead_lettered_at
            """,
            float(threshold_secs),
        )
    except Exception as e:  # noqa: BLE001 — counted, will retry next tick
        _state["errors"] = int(_state["errors"]) + 1  # type: ignore[arg-type]
        _state["last_error"] = f"{type(e).__name__}: {e}"[:300]
        log.warning("dead-letter sweep query failed: %s", e)
        return []

    marked = [dict(r) for r in (rows or [])]
    if marked:
        _state["marked"] = int(_state["marked"]) + len(marked)  # type: ignore[arg-type]
        _state["last_marked_at"] = _time.time()
        log.warning(
            "dead-letter sweep promoted %d order(s): %s",
            len(marked),
            ", ".join(str(r.get("client_order_id")) for r in marked),
        )
    return marked


async def _persist_health_alert(row: dict[str, Any]) -> None:
    """Persist a ``model_health_alerts`` row per dead-lettered coid so
    the operator console (which queries that table) surfaces the
    promotion alongside other model-health signals.

    Best-effort: any DB error is swallowed — the email path via
    :func:`_dispatch_alerts` is the source of truth for paging; this
    row is a redundant dashboard surface.
    """
    coid = str(row.get("client_order_id") or "")
    if not coid:
        return
    attempts = int(row.get("recovery_attempts") or 0)
    threshold_secs = _dead_letter_after_secs()
    ticker = str(row.get("market_ticker") or "")
    vertical = str(row.get("vertical") or "trading")
    last_err = str(row.get("last_error") or "")[:200]
    try:
        await db.execute(
            """
            INSERT INTO model_health_alerts
                (vertical, alert_type, severity, metric_name,
                 metric_value, threshold, message, acknowledged)
            VALUES ($1, 'order_dead_lettered', 'high', $2,
                    $3, $4, $5, false)
            ON CONFLICT (id) DO NOTHING
            """,
            vertical,
            coid,
            float(attempts),
            float(threshold_secs),
            (
                f"Order {coid} dead-lettered after {attempts} recovery "
                f"attempts (ticker={ticker}, last_error={last_err!r})"
            ),
        )
    except Exception as e:  # pragma: no cover — dashboard surface is best-effort
        log.warning(
            "order_dead_lettered health-alert insert failed for %s: %s",
            coid, e,
        )


async def _dispatch_alerts(rows: list[dict[str, Any]]) -> None:
    """Fire one ``order_dead_lettered`` alert per coid (deduped) AND
    persist a ``model_health_alerts`` dashboard row per coid."""
    if not rows:
        return
    from ..notifications import alerts as _alerts
    for r in rows:
        # Dashboard surface first — insert is independent of the email
        # dedupe gate so the row appears even if email is gated/disabled.
        await _persist_health_alert(r)
        try:
            _alerts.order_dead_lettered(
                client_order_id=str(r.get("client_order_id") or ""),
                market_ticker=str(r.get("market_ticker") or ""),
                side=str(r.get("side") or ""),
                action=str(r.get("action") or ""),
                contracts=int(r.get("contracts") or 0),
                limit_price_cents=(
                    int(r["limit_price_cents"])
                    if r.get("limit_price_cents") is not None
                    else None
                ),
                vertical=(
                    str(r["vertical"]) if r.get("vertical") is not None else None
                ),
                recovery_attempts=int(r.get("recovery_attempts") or 0),
                last_recovery_outcome=(
                    str(r["last_recovery_outcome"])
                    if r.get("last_recovery_outcome") is not None
                    else None
                ),
                last_error=(
                    str(r["last_error"]) if r.get("last_error") is not None else None
                ),
            )
        except Exception as e:  # pragma: no cover — alert must not break sweep
            log.warning(
                "order_dead_lettered alert dispatch failed for %s: %s",
                r.get("client_order_id"), e,
            )


_TASK: asyncio.Task | None = None
_STOP: asyncio.Event | None = None


async def _runner() -> None:
    """Internal loop body. Sleeps `sweep_interval` between sweeps and
    exits cleanly when ``_STOP`` is set."""
    global _STOP
    interval = _sweep_interval_secs()
    log.info(
        "dead-letter sweeper started (interval=%.1fs threshold=%.1fs)",
        interval, _dead_letter_after_secs(),
    )
    assert _STOP is not None
    while not _STOP.is_set():
        try:
            # Skip in paper mode — submit_failed never lands there.
            mode = (get_settings().trading_mode or "").lower()
            if mode == "paper":
                pass
            else:
                rows = await sweep_once()
                if rows:
                    await _dispatch_alerts(rows)
        except Exception as e:  # noqa: BLE001
            _state["errors"] = int(_state["errors"]) + 1  # type: ignore[arg-type]
            _state["last_error"] = f"{type(e).__name__}: {e}"[:300]
            log.warning("dead-letter sweep tick raised: %s", e)
        # Wait for either the next tick OR a stop signal — whichever
        # comes first. Avoids a `interval`-second shutdown lag.
        try:
            await asyncio.wait_for(_STOP.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("dead-letter sweeper stopped")


def start() -> asyncio.Task | None:
    """Start the sweeper task on the running event loop. Returns the
    task handle (or ``None`` if already running)."""
    global _TASK, _STOP
    if _TASK is not None and not _TASK.done():
        return _TASK
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("dead-letter sweeper start: no running loop")
        return None
    _STOP = asyncio.Event()
    _TASK = loop.create_task(_runner(), name="kalshi-dead-letter-sweeper")
    return _TASK


async def stop() -> None:
    """Signal the sweeper to exit and await its task. Safe to call
    when the sweeper isn't running."""
    global _TASK, _STOP
    if _STOP is not None:
        _STOP.set()
    if _TASK is not None:
        try:
            await asyncio.wait_for(_TASK, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _TASK.cancel()
        except Exception as e:  # pragma: no cover
            log.warning("dead-letter sweeper stop error: %s", e)
    _TASK = None
    _STOP = None
