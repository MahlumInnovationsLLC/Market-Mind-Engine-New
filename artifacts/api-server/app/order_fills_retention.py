"""Periodic GC for the `order_fills` WS-replay dedupe table.

The `order_fills` table (created in migration
``0002_order_fills_position_rollup``) stores one row per partial fill
purely so that a Kalshi (or Polymarket) WebSocket reconnect can't
double-apply a fill we've already accepted. Once a fill is older than
the WS reconnect window — a few hours at most — the dedupe row no
longer serves any purpose, but nothing was deleting it, so the table
grew unboundedly.

This module runs a single asyncio background task that periodically
deletes `order_fills` rows older than `AXIOM_ORDER_FILLS_RETENTION_DAYS`
(default 7). Mirrors the start/stop/state shape used by
``app.bankroll_sync`` so it slots cleanly into the existing lifespan
manager in ``app.main``.

Per task #154.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import db


log = logging.getLogger("axiom.order_fills_retention")


DEFAULT_RETENTION_DAYS = 7
DEFAULT_INTERVAL_SECONDS = 3600  # hourly sweep is plenty for a few-hour TTL


def _retention_days() -> int:
    raw = os.environ.get("AXIOM_ORDER_FILLS_RETENTION_DAYS")
    try:
        v = int(float(raw)) if raw is not None else DEFAULT_RETENTION_DAYS
    except (TypeError, ValueError):
        v = DEFAULT_RETENTION_DAYS
    # Refuse 0 / negative so a typo can't immediately wipe every dedupe row
    # (and break replay protection for in-flight fills).
    return max(1, v)


def _interval_seconds() -> float:
    raw = os.environ.get("AXIOM_ORDER_FILLS_CLEANUP_INTERVAL_SECONDS")
    try:
        v = float(raw) if raw is not None else float(DEFAULT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        v = float(DEFAULT_INTERVAL_SECONDS)
    return max(60.0, v)


_task: asyncio.Task[Any] | None = None
_stop = asyncio.Event()
_started_at: float | None = None
_last_tick_at: float | None = None
_last_error: str | None = None
_last_deleted: int = 0
_total_deleted: int = 0


def state() -> dict[str, Any]:
    return {
        "running": _task is not None and not _task.done(),
        "started_at": _started_at,
        "last_tick_at": _last_tick_at,
        "last_error": _last_error,
        "last_deleted": _last_deleted,
        "total_deleted": _total_deleted,
        "retention_days": _retention_days(),
        "interval_seconds": _interval_seconds(),
    }


def _parse_delete_status(status: str) -> int:
    """asyncpg returns ``"DELETE <n>"``; pull <n> back out, default 0."""
    try:
        parts = (status or "").split()
        if len(parts) >= 2 and parts[0].upper() == "DELETE":
            return int(parts[1])
    except (ValueError, AttributeError):
        pass
    return 0


async def cleanup_once() -> int:
    """Delete `order_fills` rows older than the retention window.

    Returns the number of rows deleted (0 on DB error so a flaky
    connection can't make the loop look like it's silently winning).
    """
    global _last_tick_at, _last_error, _last_deleted, _total_deleted
    days = _retention_days()
    try:
        # Cast to interval arithmetic on the SQL side so we never have
        # to build the days value into the query string itself.
        status = await db.execute(
            "DELETE FROM order_fills "
            "WHERE received_at < now() - ($1::int * INTERVAL '1 day')",
            days,
        )
        deleted = _parse_delete_status(status)
        _last_deleted = deleted
        _total_deleted += deleted
        _last_error = None
        if deleted:
            log.info(
                "order_fills retention: deleted %s row(s) older than %s day(s)",
                deleted, days,
            )
        else:
            log.debug(
                "order_fills retention: nothing to delete (window=%sd)", days,
            )
        return deleted
    except Exception as e:  # noqa: BLE001 — counted, retried next tick
        _last_error = f"{type(e).__name__}: {e}"
        log.warning("order_fills retention sweep failed: %s", _last_error)
        return 0
    finally:
        _last_tick_at = time.time()


async def _loop() -> None:
    while not _stop.is_set():
        await cleanup_once()
        try:
            await asyncio.wait_for(_stop.wait(), timeout=_interval_seconds())
        except asyncio.TimeoutError:
            continue
        else:
            return


async def start() -> None:
    global _task, _started_at, _stop
    if _task and not _task.done():
        return
    if (os.environ.get("AXIOM_ORDER_FILLS_RETENTION_ENABLED", "1") or "").strip() == "0":
        log.info("order_fills retention disabled by env")
        return
    # Re-create the Event in case start() is called from a fresh event
    # loop (test reuse pattern), otherwise the Event remains bound to
    # the previous loop and `wait_for` raises.
    _stop = asyncio.Event()
    _started_at = time.time()
    _task = asyncio.create_task(_loop(), name="order-fills-retention")
    log.info(
        "order_fills retention started (retention=%sd, interval=%ss)",
        _retention_days(), _interval_seconds(),
    )


async def stop() -> None:
    global _task
    _stop.set()
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None


def _reset_state_for_tests() -> None:
    """Test helper — clears every counter and the task handle."""
    global _task, _started_at, _last_tick_at, _last_error
    global _last_deleted, _total_deleted
    _task = None
    _started_at = None
    _last_tick_at = None
    _last_error = None
    _last_deleted = 0
    _total_deleted = 0
