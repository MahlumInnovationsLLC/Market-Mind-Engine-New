"""Daily P&L summary scheduler.

A small asyncio task that wakes once per minute, checks whether the
configured local-time-of-day has rolled past since the last send, and
fires `alerts.daily_pnl_summary` once per UTC date.

Configuration:
  - DAILY_SUMMARY_ENABLED       (default: 1)
  - DAILY_SUMMARY_HOUR_UTC      (default: 13 — ~9am ET)
  - DAILY_SUMMARY_MINUTE_UTC    (default: 0)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from .. import db
from . import alerts
from ..health import error_log


log = logging.getLogger("axiom.notifications.daily_summary")


def _enabled() -> bool:
    raw = (os.environ.get("DAILY_SUMMARY_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _send_hour() -> int:
    try:
        return max(0, min(23, int(os.environ.get("DAILY_SUMMARY_HOUR_UTC") or "13")))
    except Exception:
        return 13


def _send_minute() -> int:
    try:
        return max(0, min(59, int(os.environ.get("DAILY_SUMMARY_MINUTE_UTC") or "0")))
    except Exception:
        return 0


_task: asyncio.Task | None = None
_stop = asyncio.Event()
_last_sent_date: str | None = None


# ---------------------------------------------------------------------------
# Restart-safe dedupe — persists "last sent UTC date" to a tiny table so a
# process restart on the same day after the target time doesn't duplicate.
# ---------------------------------------------------------------------------


_DEDUPE_KEY = "daily_pnl_summary"


async def _ensure_dedupe_table() -> None:
    try:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_dedupe (
              key  TEXT PRIMARY KEY,
              date_utc DATE NOT NULL,
              sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    except Exception as e:
        log.warning("notification_dedupe table create failed: %s", e)


async def _get_last_sent_date() -> str | None:
    try:
        row = await db.fetchrow(
            "SELECT date_utc::text AS d FROM notification_dedupe WHERE key=$1",
            _DEDUPE_KEY,
        )
        return row["d"] if row else None
    except Exception as e:
        log.warning("dedupe read failed: %s", e)
        return None


async def _mark_sent(date_utc: str) -> bool:
    """Atomically claim today's date.  Returns True if this caller is the
    one that successfully claimed it (i.e. should send), False if another
    actor / earlier run already did."""
    try:
        row = await db.fetchrow(
            """
            INSERT INTO notification_dedupe (key, date_utc)
            VALUES ($1, $2::date)
            ON CONFLICT (key) DO UPDATE
              SET date_utc = EXCLUDED.date_utc,
                  sent_at  = now()
              WHERE notification_dedupe.date_utc < EXCLUDED.date_utc
            RETURNING 1 AS ok
            """,
            _DEDUPE_KEY, date_utc,
        )
        return bool(row)
    except Exception as e:
        log.warning("dedupe claim failed: %s", e)
        # Fail open — better to risk a dup than silently skip.
        return True


async def collect_summary(date_utc: str) -> dict[str, Any]:
    """Pull yesterday's PnL, fills, settle counts, etc.  All queries are
    bounded by `date_utc` (UTC YYYY-MM-DD).  Failures fall back to zero
    so a partial DB outage still produces a useful summary."""
    out: dict[str, Any] = {
        "date_utc": date_utc,
        "realized_pnl_cents": 0,
        "unrealized_pnl_cents": 0,
        "trades_settled": 0,
        "fills_today": 0,
        "open_positions": 0,
        "cycles_completed": 0,
        "halts_today": 0,
        "drift_alerts_today": 0,
        "top_movers": [],
    }
    # Realized + settled count
    try:
        row = await db.fetchrow(
            """
            SELECT
              coalesce(sum(realized_pnl), 0)::float AS realized,
              count(*)::int                         AS n
              FROM trades
             WHERE settled_at IS NOT NULL
               AND date_trunc('day', settled_at AT TIME ZONE 'UTC') = $1::date
            """,
            date_utc,
        )
        if row:
            out["realized_pnl_cents"] = int(round(float(row["realized"] or 0.0) * 100))
            out["trades_settled"] = int(row["n"] or 0)
    except Exception as e:
        error_log("daily_summary.realized", f"{type(e).__name__}: {e}")
    # Open positions count + unrealized (best effort)
    try:
        row = await db.fetchrow(
            """
            SELECT count(*)::int AS n,
                   coalesce(sum(unrealized_pnl), 0)::float AS unr
              FROM positions
             WHERE coalesce(status,'open') = 'open'
            """,
        )
        if row:
            out["open_positions"] = int(row["n"] or 0)
            out["unrealized_pnl_cents"] = int(round(float(row["unr"] or 0.0) * 100))
    except Exception as e:
        error_log("daily_summary.positions", f"{type(e).__name__}: {e}")
    # Fills today (across paper + live)
    try:
        n = await db.fetchval(
            """
            SELECT count(*)::int FROM order_lifecycle
             WHERE event_type = 'filled'
               AND date_trunc('day', created_at AT TIME ZONE 'UTC') = $1::date
            """,
            date_utc,
        )
        out["fills_today"] = int(n or 0)
    except Exception as e:
        error_log("daily_summary.fills", f"{type(e).__name__}: {e}")
    # Drift alerts today
    try:
        n = await db.fetchval(
            """
            SELECT count(*)::int FROM model_health_alerts
             WHERE alert_type = 'brier_drift'
               AND date_trunc('day', created_at AT TIME ZONE 'UTC') = $1::date
            """,
            date_utc,
        )
        out["drift_alerts_today"] = int(n or 0)
    except Exception as e:
        error_log("daily_summary.drift", f"{type(e).__name__}: {e}")
    # Top movers (largest realized PnL absolute today)
    try:
        rows = await db.fetch(
            """
            SELECT market_id AS market_ticker,
                   sum(realized_pnl)::float AS realized_pnl,
                   sum(quantity)::float     AS position
              FROM trades
             WHERE settled_at IS NOT NULL
               AND date_trunc('day', settled_at AT TIME ZONE 'UTC') = $1::date
             GROUP BY market_id
             ORDER BY abs(sum(realized_pnl)) DESC
             LIMIT 10
            """,
            date_utc,
        )
        out["top_movers"] = [
            {
                "market_ticker": r["market_ticker"],
                "realized_pnl_cents": int(round(float(r["realized_pnl"] or 0.0) * 100)),
                "position": float(r["position"] or 0.0),
            }
            for r in rows
        ]
    except Exception as e:
        error_log("daily_summary.movers", f"{type(e).__name__}: {e}")
    return out


async def fire_now(*, dry_run: bool = False) -> dict[str, Any]:
    """Build today's summary (UTC) and dispatch.  Used by the test
    endpoint and by the scheduler."""
    today = datetime.now(tz=timezone.utc).date().isoformat()
    summary = await collect_summary(today)
    return alerts.daily_pnl_summary(**summary, dry_run=dry_run)


async def _loop() -> None:
    global _last_sent_date
    log.info(
        "daily summary scheduler started: hour=%02d:%02d UTC",
        _send_hour(), _send_minute(),
    )
    # Hydrate in-memory marker from the persistent table so we don't even
    # attempt the claim query when we already know we sent today.
    _last_sent_date = await _get_last_sent_date()
    while not _stop.is_set():
        try:
            now = datetime.now(tz=timezone.utc)
            today = now.date().isoformat()
            target_hour = _send_hour()
            target_minute = _send_minute()
            past_target = (
                now.hour > target_hour
                or (now.hour == target_hour and now.minute >= target_minute)
            )
            if past_target and _last_sent_date != today:
                # Atomic claim — survives process restarts.  If another
                # worker already sent for `today`, the INSERT...ON CONFLICT
                # WHERE date_utc < EXCLUDED.date_utc returns no row.
                claimed = await _mark_sent(today)
                if not claimed:
                    _last_sent_date = today
                else:
                    log.info("firing daily summary for %s", today)
                    try:
                        await fire_now()
                        _last_sent_date = today
                    except Exception as e:
                        log.exception("daily summary send failed: %s", e)
                        error_log("daily_summary", f"{type(e).__name__}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("daily summary loop error: %s", e)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("daily summary scheduler disabled")
        return
    if _task and not _task.done():
        return
    await _ensure_dedupe_table()
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="daily-summary")


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
