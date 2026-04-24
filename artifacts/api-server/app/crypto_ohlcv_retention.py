"""Periodic GC for the `crypto_ohlcv` bar table.

The crypto OHLCV poller (``app.feeds.crypto``) is append-with-upsert
and never deletes anything.  At 1-minute granularity for two symbols
that's roughly a million rows a year — fine in the short term, but
without a TTL the table grows forever.

This module mirrors the start/stop/state shape used by
``app.order_fills_retention`` so it slots into the existing lifespan
manager in ``app.main``.  It runs a single asyncio loop that, on each
tick, deletes bars older than a per-granularity retention window.

Defaults (operator-overridable via env):
  * 1m   — 180 days   (``AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_1M``)
  * 5m   — 365 days   (``AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_5M``)
  * 15m  — 730 days   (``AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_15M``)
  * 1h   — 1825 days  (``AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_1H``)

Sweep cadence defaults to 6 hours
(``AXIOM_CRYPTO_OHLCV_CLEANUP_INTERVAL_SECONDS``); the whole loop can
be turned off with ``AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED=0``.

Per task #175.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import db


log = logging.getLogger("axiom.crypto_ohlcv_retention")


# Defaults are deliberately generous: coarser bars are cheap to keep
# around and the predictor benefits from a long lookback for backtests.
DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "1m": 180,
    "5m": 365,
    "15m": 730,
    "1h": 1825,
}

DEFAULT_INTERVAL_SECONDS = 6 * 3600  # 6h sweep — TTLs are measured in days

_ENV_KEY_BY_GRANULARITY: dict[str, str] = {
    g: f"AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_{g.upper()}"
    for g in DEFAULT_RETENTION_DAYS
}


def _retention_days(granularity: str) -> int:
    default = DEFAULT_RETENTION_DAYS[granularity]
    raw = os.environ.get(_ENV_KEY_BY_GRANULARITY[granularity])
    try:
        v = int(float(raw)) if raw is not None else default
    except (TypeError, ValueError):
        v = default
    # Refuse 0 / negative so a typo can't immediately wipe the table.
    return max(1, v)


def retention_windows() -> dict[str, int]:
    """Public view of the resolved per-granularity windows (in days)."""
    return {g: _retention_days(g) for g in DEFAULT_RETENTION_DAYS}


def _interval_seconds() -> float:
    raw = os.environ.get("AXIOM_CRYPTO_OHLCV_CLEANUP_INTERVAL_SECONDS")
    try:
        v = float(raw) if raw is not None else float(DEFAULT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        v = float(DEFAULT_INTERVAL_SECONDS)
    # 5-minute floor protects against a typo hammering the DB.
    return max(300.0, v)


_task: asyncio.Task[Any] | None = None
_stop = asyncio.Event()
_started_at: float | None = None
_last_tick_at: float | None = None
_last_error: str | None = None
_last_deleted: int = 0
_total_deleted: int = 0
_last_deleted_by_granularity: dict[str, int] = {}


def _enabled_env() -> bool:
    """Whether the loop is allowed to run per ``AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED``.

    Mirrors the check ``start()`` performs so the operator dashboard can
    distinguish "disabled by config" from "intended to run but crashed".
    """
    return (os.environ.get("AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED", "1") or "").strip() != "0"


def state() -> dict[str, Any]:
    return {
        "running": _task is not None and not _task.done(),
        "enabled": _enabled_env(),
        "started_at": _started_at,
        "last_tick_at": _last_tick_at,
        "last_error": _last_error,
        "last_deleted": _last_deleted,
        "total_deleted": _total_deleted,
        "last_deleted_by_granularity": dict(_last_deleted_by_granularity),
        "retention_days": retention_windows(),
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
    """Sweep every granularity once. Returns total rows deleted."""
    global _last_tick_at, _last_error, _last_deleted, _total_deleted
    global _last_deleted_by_granularity

    per_gran: dict[str, int] = {}
    total = 0
    last_err: str | None = None
    for granularity in DEFAULT_RETENTION_DAYS:
        days = _retention_days(granularity)
        try:
            # Cast days through a parameter so the value never has to
            # be interpolated into the SQL string itself.
            status = await db.execute(
                "DELETE FROM crypto_ohlcv "
                "WHERE granularity = $1 "
                "AND ts < now() - ($2::int * INTERVAL '1 day')",
                granularity, days,
            )
            deleted = _parse_delete_status(status)
            per_gran[granularity] = deleted
            total += deleted
            if deleted:
                log.info(
                    "crypto_ohlcv retention: deleted %s row(s) at %s older than %s day(s)",
                    deleted, granularity, days,
                )
            else:
                log.debug(
                    "crypto_ohlcv retention: nothing to delete at %s (window=%sd)",
                    granularity, days,
                )
        except Exception as e:  # noqa: BLE001 — counted, retried next tick
            last_err = f"{type(e).__name__}: {e}"
            log.warning(
                "crypto_ohlcv retention sweep failed at %s: %s",
                granularity, last_err,
            )
            per_gran[granularity] = 0

    _last_deleted_by_granularity = per_gran
    _last_deleted = total
    _total_deleted += total
    _last_error = last_err
    _last_tick_at = time.time()
    return total


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
    if (os.environ.get("AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED", "1") or "").strip() == "0":
        log.info("crypto_ohlcv retention disabled by env")
        return
    # Re-create the Event in case start() is called from a fresh event
    # loop (test reuse pattern), otherwise the Event remains bound to
    # the previous loop and `wait_for` raises.
    _stop = asyncio.Event()
    _started_at = time.time()
    _task = asyncio.create_task(_loop(), name="crypto-ohlcv-retention")
    log.info(
        "crypto_ohlcv retention started (windows=%s, interval=%ss)",
        retention_windows(), _interval_seconds(),
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
    global _last_deleted, _total_deleted, _last_deleted_by_granularity
    _task = None
    _started_at = None
    _last_tick_at = None
    _last_error = None
    _last_deleted = 0
    _total_deleted = 0
    _last_deleted_by_granularity = {}
