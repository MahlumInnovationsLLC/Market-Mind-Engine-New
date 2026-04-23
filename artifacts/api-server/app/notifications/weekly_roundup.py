"""Weekly P&L roundup scheduler.

A small asyncio task that wakes once per minute, checks whether the
configured weekly send window (default: Friday 22:00 UTC) has rolled
past since the last send, and fires `alerts.weekly_roundup` once per
ISO week.

Configuration:
  - WEEKLY_ROUNDUP_ENABLED      (default: 1)
  - WEEKLY_ROUNDUP_DOW          (default: 4 — Friday; Mon=0 .. Sun=6)
  - WEEKLY_ROUNDUP_HOUR_UTC     (default: 22)
  - WEEKLY_ROUNDUP_MINUTE_UTC   (default: 0)

Idempotency: uses the shared `notification_dedupe` table via
`alerts.dedupe_or_skip` with a 7-day TTL keyed by ISO week, so a
process restart on the same Friday after the target time doesn't
double-send and a missed Friday doesn't get backfilled into Saturday.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from . import alerts
from ..health import error_log


log = logging.getLogger("axiom.notifications.weekly_roundup")


def _enabled() -> bool:
    raw = (os.environ.get("WEEKLY_ROUNDUP_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _send_dow() -> int:
    try:
        return max(0, min(6, int(os.environ.get("WEEKLY_ROUNDUP_DOW") or "4")))
    except Exception:
        return 4


def _send_hour() -> int:
    try:
        return max(0, min(23, int(os.environ.get("WEEKLY_ROUNDUP_HOUR_UTC") or "22")))
    except Exception:
        return 22


def _send_minute() -> int:
    try:
        return max(0, min(59, int(os.environ.get("WEEKLY_ROUNDUP_MINUTE_UTC") or "0")))
    except Exception:
        return 0


_task: asyncio.Task | None = None
_stop = asyncio.Event()
_last_sent_week: str | None = None


_ALERT_TYPE = "weekly_roundup"


def _iso_week_key(d: "datetime") -> str:
    """Stable ISO-week identifier (e.g. ``2026-W17``) used as the
    dedupe key so two ticks in the same Friday window can't double-send
    even after a restart."""
    iso = d.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def _week_window_utc(now: "datetime") -> tuple[str, str]:
    """Return ``(start_iso, end_iso)`` for the trailing 7-day window
    ending at ``now`` (exclusive). Used by `collect_summary` so the
    aggregation matches the scheduler's send cadence regardless of
    when in the day the job actually fires."""
    end = now.replace(microsecond=0)
    start = end - timedelta(days=7)
    return start.isoformat(), end.isoformat()


async def collect_summary(now: "datetime") -> dict[str, Any]:
    """Aggregate the prior week's realized + unrealized P&L and trade
    count. All queries are bounded by the 7-day window ending at
    ``now``. Failures fall back to zero so a partial DB outage still
    produces a useful summary."""
    start_iso, end_iso = _week_window_utc(now)
    out: dict[str, Any] = {
        "week_ending_utc": now.date().isoformat(),
        "realized_pnl_cents": 0,
        "unrealized_pnl_cents": 0,
        "trades_settled": 0,
        "win_rate_pct": None,
    }
    # Realized P&L + settled count + win rate over the trailing week.
    try:
        row = await db.fetchrow(
            """
            SELECT
              coalesce(sum(realized_pnl), 0)::float                       AS realized,
              count(*)::int                                               AS n,
              count(*) FILTER (WHERE realized_pnl > 0)::int               AS wins
              FROM trades
             WHERE settled_at IS NOT NULL
               AND settled_at >= $1::timestamptz
               AND settled_at <  $2::timestamptz
            """,
            start_iso, end_iso,
        )
        if row:
            n = int(row["n"] or 0)
            out["realized_pnl_cents"] = int(round(float(row["realized"] or 0.0) * 100))
            out["trades_settled"] = n
            if n > 0:
                out["win_rate_pct"] = round(100.0 * int(row["wins"] or 0) / n, 1)
    except Exception as e:
        error_log("weekly_roundup.realized", f"{type(e).__name__}: {e}")
    # Open positions unrealized snapshot (point-in-time, like daily summary).
    try:
        unr = await db.fetchval(
            """
            SELECT coalesce(sum(unrealized_pnl), 0)::float
              FROM positions
             WHERE coalesce(status,'open') = 'open'
            """,
        )
        out["unrealized_pnl_cents"] = int(round(float(unr or 0.0) * 100))
    except Exception as e:
        error_log("weekly_roundup.positions", f"{type(e).__name__}: {e}")
    return out


async def fire_now(*, dry_run: bool = False) -> dict[str, Any]:
    """Build the trailing-week summary and dispatch via
    `alerts.weekly_roundup`. Used by the test endpoint and by the
    scheduler."""
    now = datetime.now(tz=timezone.utc)
    summary = await collect_summary(now)
    return alerts.weekly_roundup(**summary, dry_run=dry_run)


async def _loop() -> None:
    global _last_sent_week
    log.info(
        "weekly roundup scheduler started: dow=%d hour=%02d:%02d UTC",
        _send_dow(), _send_hour(), _send_minute(),
    )
    while not _stop.is_set():
        try:
            now = datetime.now(tz=timezone.utc)
            target_dow = _send_dow()
            target_hour = _send_hour()
            target_minute = _send_minute()
            week_key = _iso_week_key(now)
            past_target = (
                now.weekday() == target_dow
                and (
                    now.hour > target_hour
                    or (now.hour == target_hour and now.minute >= target_minute)
                )
            )
            if past_target and _last_sent_week != week_key:
                # Persistent claim — restart-safe, TTL of 7 days so the
                # same ISO-week key can't re-fire even if the in-memory
                # marker was lost.
                claimed = await alerts.dedupe_or_skip(
                    _ALERT_TYPE, week_key, ttl_seconds=7 * 24 * 3600,
                )
                if not claimed:
                    _last_sent_week = week_key
                else:
                    log.info("firing weekly roundup for %s", week_key)
                    try:
                        await fire_now()
                        _last_sent_week = week_key
                    except Exception as e:
                        log.exception("weekly roundup send failed: %s", e)
                        error_log("weekly_roundup", f"{type(e).__name__}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("weekly roundup loop error: %s", e)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("weekly roundup scheduler disabled")
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="weekly-roundup")


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
