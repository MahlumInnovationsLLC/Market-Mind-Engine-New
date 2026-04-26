"""Periodic GC for the `crypto_backtest_runs` trend table.

Every successful run of ``/api/admin/crypto/backtest`` (and every tick
of ``app.crypto.backtest_scheduler``) appends a row to
``crypto_backtest_runs`` (added by migration ``0021_crypto_backtest_runs``).
Without a TTL the table grows unboundedly — the dashboard's trend
chart only needs the most recent ~50 rows per ``(symbol, timeframe)``
pair, so anything older than that is dead weight.

This module mirrors the start/stop/state shape used by
``app.crypto_ohlcv_retention`` so it slots into the existing lifespan
manager in ``app.main``. The trim policy is "keep the last N rows OR
last M days per pair, whichever is larger" — i.e. a row is only
deleted when it falls outside *both* the row-count window *and* the
age window. That keeps the chart's most-recent slice intact while
guaranteeing some history for cold pairs that don't run often enough
to fill N rows.

Defaults (operator-overridable via env):
  * ``AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS``         50
  * ``AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS``             90
  * ``AXIOM_CRYPTO_BACKTEST_CLEANUP_INTERVAL_SECONDS``   21600 (6h)
  * ``AXIOM_CRYPTO_BACKTEST_RETENTION_ENABLED``          1

Per task #582.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import db


log = logging.getLogger("axiom.crypto_backtest_retention")


# Defaults: 50 rows matches `list_history`'s default `limit=50`, and
# 90d gives ~3 months of trend coverage for low-cadence pairs.
DEFAULT_MIN_ROWS = 50
DEFAULT_RETENTION_DAYS = 90
DEFAULT_INTERVAL_SECONDS = 6 * 3600  # 6h sweep — TTLs are days-scale


def _min_rows() -> int:
    raw = os.environ.get("AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS")
    try:
        v = int(float(raw)) if raw is not None else DEFAULT_MIN_ROWS
    except (TypeError, ValueError):
        v = DEFAULT_MIN_ROWS
    # Refuse 0 / negative so a typo can't immediately wipe the table.
    return max(1, v)


def _retention_days() -> int:
    raw = os.environ.get("AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS")
    try:
        v = int(float(raw)) if raw is not None else DEFAULT_RETENTION_DAYS
    except (TypeError, ValueError):
        v = DEFAULT_RETENTION_DAYS
    return max(1, v)


def _interval_seconds() -> float:
    raw = os.environ.get("AXIOM_CRYPTO_BACKTEST_CLEANUP_INTERVAL_SECONDS")
    try:
        v = float(raw) if raw is not None else float(DEFAULT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        v = float(DEFAULT_INTERVAL_SECONDS)
    # 5-minute floor protects against a typo hammering the DB.
    return max(300.0, v)


def _enabled_env() -> bool:
    """Whether the loop is allowed to run per ``AXIOM_CRYPTO_BACKTEST_RETENTION_ENABLED``.

    Mirrors the check ``start()`` performs so the operator dashboard can
    distinguish "disabled by config" from "intended to run but crashed".
    """
    return (
        os.environ.get("AXIOM_CRYPTO_BACKTEST_RETENTION_ENABLED", "1") or ""
    ).strip() != "0"


def retention_policy() -> dict[str, int]:
    """Public view of the resolved retention horizons."""
    return {
        "min_rows": _min_rows(),
        "retention_days": _retention_days(),
    }


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
        "enabled": _enabled_env(),
        "started_at": _started_at,
        "last_tick_at": _last_tick_at,
        "last_error": _last_error,
        "last_deleted": _last_deleted,
        "total_deleted": _total_deleted,
        "policy": retention_policy(),
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


# Keep the most-recent ``$1`` rows per (symbol, timeframe) AND any row
# newer than ``$2`` days, deleting only rows that fall outside *both*
# bands.  Using a CTE keeps the per-pair row_number() window compact
# enough that the supporting (symbol, timeframe, completed_at DESC)
# index introduced by migration 0021 covers the whole scan.
_TRIM_SQL = """
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY symbol, timeframe
               ORDER BY completed_at DESC, id DESC
           ) AS rn
      FROM crypto_backtest_runs
)
DELETE FROM crypto_backtest_runs
 WHERE id IN (
       SELECT id FROM ranked WHERE rn > $1
 )
   AND completed_at < now() - ($2::int * INTERVAL '1 day')
"""


async def cleanup_once() -> int:
    """Trim the trend table once. Returns total rows deleted."""
    global _last_tick_at, _last_error, _last_deleted, _total_deleted

    min_rows = _min_rows()
    retention_days = _retention_days()
    last_err: str | None = None
    deleted = 0
    try:
        status = await db.execute(_TRIM_SQL, min_rows, retention_days)
        deleted = _parse_delete_status(status)
        if deleted:
            log.info(
                "crypto_backtest_runs retention: deleted %s row(s) "
                "older than %sd beyond top %s per pair",
                deleted, retention_days, min_rows,
            )
        else:
            log.debug(
                "crypto_backtest_runs retention: nothing to delete "
                "(min_rows=%s, days=%s)",
                min_rows, retention_days,
            )
    except Exception as e:  # noqa: BLE001 — counted, retried next tick
        last_err = f"{type(e).__name__}: {e}"
        log.warning("crypto_backtest_runs retention sweep failed: %s", last_err)

    _last_deleted = deleted
    _total_deleted += deleted
    _last_error = last_err
    _last_tick_at = time.time()
    return deleted


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
    if not _enabled_env():
        log.info("crypto_backtest_runs retention disabled by env")
        return
    # Re-create the Event in case start() is called from a fresh event
    # loop (test reuse pattern), otherwise the Event remains bound to
    # the previous loop and `wait_for` raises.
    _stop = asyncio.Event()
    _started_at = time.time()
    _task = asyncio.create_task(_loop(), name="crypto-backtest-retention")
    log.info(
        "crypto_backtest_runs retention started (policy=%s, interval=%ss)",
        retention_policy(), _interval_seconds(),
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
