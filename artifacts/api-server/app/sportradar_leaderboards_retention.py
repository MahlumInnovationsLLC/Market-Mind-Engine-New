"""Periodic GC for the `sportradar_tournament_leaderboards` history table.

Migration ``0025_sportradar_tournament_leaderboards`` (task #623) added
an append-only audit trail that captures one row per (event_id,
position, name) on every leaderboard poll.  With the default 120s
poll cadence and a typical PGA field of ~150 entrants, a single
4-day tournament writes ~430k rows, and the table grows without bound
across seasons.

This module mirrors the start/stop/state shape used by
``app.crypto_backtest_retention`` so it slots into the existing
lifespan manager in ``app.main``.  Per task #649.

The trim policy is "keep the last N snapshots per event_id OR any
snapshot newer than M days, whichever is larger" — i.e. a row is only
deleted when its snapshot falls outside *both* the snapshot-count
window *and* the age window.  That guarantees:

  * The latest snapshot per event_id is always preserved (so
    ``latest_tournament_leaderboard`` still works for any event we've
    ever polled).
  * Recent activity for every event stays available for replay.
  * Old, completed events eventually fall out of the table once both
    their snapshot count exceeds ``min_snapshots`` *and* every
    snapshot is older than the day window.

Defaults (operator-overridable via env):
  * ``AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS``     500
  * ``AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS``              90
  * ``AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS``    21600 (6h)
  * ``AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED``           1
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import db


log = logging.getLogger("axiom.sportradar_leaderboards_retention")


# Defaults: 500 snapshots covers ~17h of polling at the 120s default
# cadence (or ~10 days if the cadence is dialled down to 30 minutes),
# and 90 days of full audit trail comfortably spans a PGA season for
# any event that's been live recently.
DEFAULT_MIN_SNAPSHOTS = 500
DEFAULT_RETENTION_DAYS = 90
DEFAULT_INTERVAL_SECONDS = 6 * 3600  # 6h sweep — TTLs are days-scale


def _min_snapshots() -> int:
    raw = os.environ.get("AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS")
    try:
        v = int(float(raw)) if raw is not None else DEFAULT_MIN_SNAPSHOTS
    except (TypeError, ValueError):
        v = DEFAULT_MIN_SNAPSHOTS
    # Refuse 0 / negative so a typo can't immediately wipe every
    # event's most-recent snapshot.
    return max(1, v)


def _retention_days() -> int:
    raw = os.environ.get("AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS")
    try:
        v = int(float(raw)) if raw is not None else DEFAULT_RETENTION_DAYS
    except (TypeError, ValueError):
        v = DEFAULT_RETENTION_DAYS
    return max(1, v)


def _interval_seconds() -> float:
    raw = os.environ.get("AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS")
    try:
        v = float(raw) if raw is not None else float(DEFAULT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        v = float(DEFAULT_INTERVAL_SECONDS)
    # 5-minute floor protects against a typo hammering the DB.
    return max(300.0, v)


def _enabled_env() -> bool:
    """Whether the loop is allowed to run per
    ``AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED``.

    Mirrors the check ``start()`` performs so the operator dashboard
    can distinguish "disabled by config" from "intended to run but
    crashed".
    """
    return (
        os.environ.get("AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED", "1") or ""
    ).strip() != "0"


def retention_policy() -> dict[str, int]:
    """Public view of the resolved retention horizons."""
    return {
        "min_snapshots": _min_snapshots(),
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


# Keep the most-recent ``$1`` snapshots per event_id AND any snapshot
# newer than ``$2`` days, deleting only rows whose snapshot falls
# outside *both* bands.  A "snapshot" is the set of rows sharing a
# (event_id, fetched_at), so we rank distinct timestamps per event
# rather than ranking individual rows (which would over-delete the
# tail of any snapshot whose entrant count is below ``min_snapshots``).
#
# The supporting (event_id, fetched_at DESC) index introduced by
# migration 0025 covers the DISTINCT scan as well as the row-level
# delete predicate.
_TRIM_SQL = """
WITH ranked AS (
    SELECT event_id,
           fetched_at,
           row_number() OVER (
               PARTITION BY event_id
               ORDER BY fetched_at DESC
           ) AS rn
      FROM (
          SELECT DISTINCT event_id, fetched_at
            FROM sportradar_tournament_leaderboards
      ) AS snapshots
)
DELETE FROM sportradar_tournament_leaderboards t
 WHERE (t.event_id, t.fetched_at) IN (
       SELECT event_id, fetched_at FROM ranked WHERE rn > $1
 )
   AND t.fetched_at < now() - ($2::int * INTERVAL '1 day')
"""


async def cleanup_once() -> int:
    """Trim the leaderboard history once. Returns total rows deleted."""
    global _last_tick_at, _last_error, _last_deleted, _total_deleted

    min_snapshots = _min_snapshots()
    retention_days = _retention_days()
    last_err: str | None = None
    deleted = 0
    try:
        status = await db.execute(_TRIM_SQL, min_snapshots, retention_days)
        deleted = _parse_delete_status(status)
        if deleted:
            log.info(
                "sportradar_tournament_leaderboards retention: deleted %s "
                "row(s) older than %sd beyond top %s snapshots per event",
                deleted, retention_days, min_snapshots,
            )
        else:
            log.debug(
                "sportradar_tournament_leaderboards retention: nothing to "
                "delete (min_snapshots=%s, days=%s)",
                min_snapshots, retention_days,
            )
    except Exception as e:  # noqa: BLE001 — counted, retried next tick
        last_err = f"{type(e).__name__}: {e}"
        log.warning(
            "sportradar_tournament_leaderboards retention sweep failed: %s",
            last_err,
        )

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
        log.info("sportradar_tournament_leaderboards retention disabled by env")
        return
    # Re-create the Event in case start() is called from a fresh event
    # loop (test reuse pattern), otherwise the Event remains bound to
    # the previous loop and `wait_for` raises.
    _stop = asyncio.Event()
    _started_at = time.time()
    _task = asyncio.create_task(
        _loop(), name="sportradar-leaderboards-retention"
    )
    log.info(
        "sportradar_tournament_leaderboards retention started "
        "(policy=%s, interval=%ss)",
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
