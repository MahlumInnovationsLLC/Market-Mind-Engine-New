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
import json
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
_last_gc_date: str | None = None


# How many days of expired dedupe rows we keep around for forensic
# debugging before the daily GC sweeps them. Daily-summary markers
# (`expires_at IS NULL`) are always preserved. Operator override via
# `AXIOM_NOTIFICATION_DEDUPE_RETENTION_DAYS` — see
# `alerts._resolve_dedupe_retention_days`.
_DEDUPE_GC_RETAIN_DAYS = 7


def _dedupe_gc_retention_days() -> int:
    return alerts._resolve_dedupe_retention_days(_DEDUPE_GC_RETAIN_DAYS)


# ---------------------------------------------------------------------------
# Restart-safe dedupe — persists "last sent UTC date" to a tiny table so a
# process restart on the same day after the target time doesn't duplicate.
# ---------------------------------------------------------------------------


_DEDUPE_KEY = "daily_pnl_summary"


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
            VALUES ($1, $2::text::date)
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
        "slippage_guard_rejects_today": 0,
        "top_movers": [],
        "by_vertical": [],
        "by_persona": [],
        "slippage_guard_offenders": [],
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
               AND date_trunc('day', settled_at AT TIME ZONE 'UTC') = $1::text::date
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
               AND date_trunc('day', created_at AT TIME ZONE 'UTC') = $1::text::date
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
               AND date_trunc('day', created_at AT TIME ZONE 'UTC') = $1::text::date
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
               AND date_trunc('day', settled_at AT TIME ZONE 'UTC') = $1::text::date
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
    # Per-vertical breakdown — pull today's vertical_daily_pnl rows and
    # join the latest 24h Brier from calibration_snapshots.
    try:
        rows = await db.fetch(
            """
            SELECT v.vertical,
                   v.trades_count::int                    AS n,
                   v.wins::int                            AS wins,
                   v.losses::int                          AS losses,
                   coalesce(v.daily_pnl, 0)::float        AS pnl,
                   coalesce(v.cumulative_pnl, 0)::float   AS cum_pnl,
                   coalesce(v.win_rate, 0)::float         AS win_rate,
                   coalesce(v.avg_edge, 0)::float         AS avg_edge,
                   (
                     SELECT cs.brier_score::float
                       FROM calibration_snapshots cs
                      WHERE cs.vertical = v.vertical
                        AND cs.sub_category = '24h'
                      ORDER BY cs.created_at DESC
                      LIMIT 1
                   )                                      AS brier
              FROM vertical_daily_pnl v
             WHERE v.report_date = $1::text::date
             ORDER BY v.vertical
            """,
            date_utc,
        )
        out["by_vertical"] = [
            {
                "vertical": r["vertical"],
                "trades": int(r["n"] or 0),
                "wins": int(r["wins"] or 0),
                "losses": int(r["losses"] or 0),
                "pnl_cents": int(round(float(r["pnl"] or 0.0) * 100)),
                "cumulative_pnl_cents": int(round(float(r["cum_pnl"] or 0.0) * 100)),
                "win_rate_pct": float(r["win_rate"] or 0.0),
                "avg_edge": float(r["avg_edge"] or 0.0),
                "brier": float(r["brier"]) if r["brier"] is not None else None,
            }
            for r in rows
        ]
    except Exception as e:
        error_log("daily_summary.by_vertical", f"{type(e).__name__}: {e}")
    # Per-persona contribution — see aggregate_persona_day().
    try:
        out["by_persona"] = await aggregate_persona_day(date_utc)
    except Exception as e:
        error_log("daily_summary.by_persona", f"{type(e).__name__}: {e}")
    # Slippage-guard rejects today (count + per-market repeat-offender list).
    # Sourced from `order_lifecycle` rows tagged `reason=slippage_guard`.
    try:
        n = await db.fetchval(
            """
            SELECT count(*)::int FROM order_lifecycle
             WHERE event_type = 'rejected'
               AND metadata->>'reason' = 'slippage_guard'
               AND date_trunc('day', created_at AT TIME ZONE 'UTC') = $1::text::date
            """,
            date_utc,
        )
        out["slippage_guard_rejects_today"] = int(n or 0)
    except Exception as e:
        error_log("daily_summary.slippage_guard_count", f"{type(e).__name__}: {e}")
    try:
        # Lazy import to avoid a circular: dashboard imports notifications.* too.
        from .. import dashboard as _dashboard
        out["slippage_guard_offenders"] = (
            await _dashboard.get_slippage_guard_top_offenders(
                date_utc=date_utc, limit=10,
            )
        )
    except Exception as e:
        error_log("daily_summary.slippage_guard_offenders", f"{type(e).__name__}: {e}")
    return out


async def aggregate_persona_day(date_utc: str) -> list[dict[str, Any]]:
    """Per-persona Brier / accuracy / lean breakdown for `date_utc`.

    Aggregates settled `swarm_debates` rows opened on `date_utc` (UTC)
    by walking each row's `analyst_verdicts` JSON.  Tolerates both the
    legacy `analyst_id` key and the current `persona` key, and silently
    skips malformed verdicts so a single bad row doesn't poison the
    whole table.

    Returned rows are sorted lowest-Brier-first (best persona of the
    day on top), with ties broken by debate count.

    Reused by both the daily summary email and the live operator
    dashboard so the two views can never disagree.
    """
    debates = await db.fetch(
        """
        SELECT id, outcome, analyst_verdicts
          FROM swarm_debates
         WHERE outcome IS NOT NULL
           AND date_trunc('day', created_at AT TIME ZONE 'UTC') = $1::text::date
        """,
        date_utc,
    )
    agg: dict[str, dict[str, Any]] = {}
    for d in debates:
        outcome = (d["outcome"] or "").upper()
        if outcome not in ("WIN", "LOSS"):
            continue
        y = 1 if outcome == "WIN" else 0
        verdicts = d["analyst_verdicts"]
        if isinstance(verdicts, str):
            try:
                verdicts = json.loads(verdicts)
            except Exception:
                continue
        if not isinstance(verdicts, list):
            continue
        for v in verdicts:
            if not isinstance(v, dict):
                continue
            pid = v.get("persona") or v.get("analyst_id")
            if not pid:
                continue
            prob = v.get("probability")
            if prob is None:
                continue
            try:
                p = float(prob)
            except Exception:
                continue
            lean = (v.get("lean") or "").upper() or (
                "YES" if p >= 0.5 else "NO"
            )
            conf = v.get("confidence")
            try:
                conf_f = float(conf) if conf is not None else None
            except Exception:
                conf_f = None
            slot = agg.setdefault(
                pid,
                {
                    "persona_id": pid,
                    "debates": 0,
                    "_sq_err": 0.0,
                    "_correct": 0,
                    "_conf_sum": 0.0,
                    "_conf_n": 0,
                    "lean_yes": 0,
                    "lean_no": 0,
                    "lean_pass": 0,
                },
            )
            slot["debates"] += 1
            slot["_sq_err"] += (p - y) ** 2
            slot["_correct"] += 1 if (p >= 0.5) == (y == 1) else 0
            if conf_f is not None:
                slot["_conf_sum"] += conf_f
                slot["_conf_n"] += 1
            if lean == "YES":
                slot["lean_yes"] += 1
            elif lean == "NO":
                slot["lean_no"] += 1
            elif lean == "PASS":
                slot["lean_pass"] += 1
    personas: list[dict[str, Any]] = []
    for pid, s in agg.items():
        n = max(1, int(s["debates"]))
        personas.append(
            {
                "persona_id": pid,
                "debates": int(s["debates"]),
                "brier": round(float(s["_sq_err"]) / n, 4),
                "accuracy_pct": round(100.0 * float(s["_correct"]) / n, 1),
                "avg_confidence": (
                    round(float(s["_conf_sum"]) / s["_conf_n"], 3)
                    if s["_conf_n"]
                    else None
                ),
                "lean_yes": int(s["lean_yes"]),
                "lean_no": int(s["lean_no"]),
                "lean_pass": int(s["lean_pass"]),
            }
        )
    personas.sort(key=lambda r: (r["brier"], -r["debates"]))
    return personas


async def fire_now(*, dry_run: bool = False) -> dict[str, Any]:
    """Build today's summary (UTC) and dispatch.  Used by the test
    endpoint and by the scheduler."""
    today = datetime.now(tz=timezone.utc).date().isoformat()
    summary = await collect_summary(today)
    return alerts.daily_pnl_summary(**summary, dry_run=dry_run)


async def _run_dedupe_gc(today: str) -> None:
    """Run the daily GC against `notification_dedupe` at most once per
    UTC day. Only marks the day as done when the cleanup actually
    succeeded, so a transient DB outage gets retried on the next tick
    instead of being skipped until tomorrow."""
    global _last_gc_date
    if _last_gc_date == today:
        return
    try:
        result = await alerts.gc_expired_dedupe(
            older_than_days=_dedupe_gc_retention_days()
        )
    except Exception as e:  # pragma: no cover — gc_expired_dedupe already logs
        log.warning("dedupe gc tick failed: %s", e)
        return
    if "error" in result:
        # gc_expired_dedupe swallowed a DB error — leave _last_gc_date
        # alone so the next loop iteration tries again.
        return
    _last_gc_date = today


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
            # Daily GC of expired dedupe rows — runs once per UTC day,
            # independent of whether the summary itself has fired yet,
            # so the table stays bounded even when the send-time window
            # is in the future.
            await _run_dedupe_gc(today)
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
