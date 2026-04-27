"""Soccer settlement drift watcher.

Background asyncio loop that flags soccer Kalshi markets whose linked
Sportradar match has been final for longer than the configured
threshold (default 2h) while trades on the market are still
un-settled.

The settlement cycle settles a soccer market by joining
``swarm_debates`` (`game_id`) into ``sportradar_live_state`` — see
``app.settlement.pipeline._sportradar_three_way_result``. A stale or
missing ``swarm_debates`` row silently skips the market on every
sweep, which means an operator only notices "open positions for a
match that finished hours ago" by manually scanning the dashboard.

Each finding:
  * persists a ``model_health_alerts`` row with
    ``alert_type='soccer_settlement_drift'`` so the operator console
    surfaces it alongside the existing brier_drift / position_drift
    entries (the ``vertical`` column carries the kalshi ticker);
  * dispatches a ``soccer_settlement_drift`` operator email via the
    same ``notifications.alerts._dispatch`` pipeline used by every
    other drift alert, deduped per ``(ticker, game_id)`` for 6h so a
    persistent stale link only pages a handful of times per day.

Configuration (all read from env, falling back to defaults):
  * ``SOCCER_SETTLEMENT_DRIFT_ENABLED``         (default 1)
  * ``SOCCER_SETTLEMENT_DRIFT_FINAL_HOURS``     (default 2.0)
  * ``SOCCER_SETTLEMENT_DRIFT_CHECK_INTERVAL_SECS`` (default 600 — every 10m)
  * ``SOCCER_SETTLEMENT_DRIFT_MAX_FINDINGS``    (default 50 — query LIMIT)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..health import error_log
from ..settlement.pipeline import _SOCCER_FINAL_STATUSES
from ..sports.leagues import LEAGUE_CONFIGS
from . import alerts


log = logging.getLogger("axiom.notifications.soccer_settlement_drift")


# ---------------------------------------------------------------------------
# Env-tunable knobs
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    raw = (os.environ.get("SOCCER_SETTLEMENT_DRIFT_ENABLED") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def _final_hours() -> float:
    try:
        v = float(os.environ.get("SOCCER_SETTLEMENT_DRIFT_FINAL_HOURS") or "2.0")
    except Exception:
        return 2.0
    return max(0.1, v)


def _check_interval_secs() -> int:
    try:
        v = int(os.environ.get("SOCCER_SETTLEMENT_DRIFT_CHECK_INTERVAL_SECS") or "600")
    except Exception:
        return 600
    return max(30, v)


def _max_findings() -> int:
    try:
        v = int(os.environ.get("SOCCER_SETTLEMENT_DRIFT_MAX_FINDINGS") or "50")
    except Exception:
        return 50
    return max(1, v)


def _soccer_leagues() -> list[str]:
    """All league tags the bot treats as soccer (3-way h/d/a markets).

    Sourced from ``LEAGUE_CONFIGS`` so a future soccer league dropped
    into the registry is automatically covered without a code edit
    here. Returned sorted for stable SQL plan caching.
    """
    return sorted(
        league for league, cfg in LEAGUE_CONFIGS.items() if cfg.three_way_market
    )


# ---------------------------------------------------------------------------
# DB query — find stale soccer markets.
# ---------------------------------------------------------------------------


_FINAL_STATUSES_LOWER = sorted(s.lower() for s in _SOCCER_FINAL_STATUSES)


async def find_stuck_markets(
    *,
    final_hours: float,
    limit: int,
) -> list[dict[str, Any]]:
    """Return soccer Kalshi markets whose linked Sportradar match has
    been reporting a final status for at least ``final_hours`` while
    the market still carries un-settled trades.

    Each row contains:
      ``ticker`` (kalshi_market_id), ``game_id`` (sportradar),
      ``league``, ``first_final_at`` (when sportradar_live_state first
      observed a final status for this game), ``last_status`` (most
      recent status row), ``open_trades`` (count of unsettled trades).

    Fails soft — returns ``[]`` on DB error so a transient outage
    doesn't kill the parent loop.
    """
    leagues = _soccer_leagues()
    if not leagues:
        return []
    try:
        rows = await db.fetch(
            """
            WITH soccer_unsettled AS (
              SELECT market_id,
                     count(*)::int   AS open_trades,
                     max(league)     AS league
                FROM trades
               WHERE settled_at IS NULL
                 AND exchange = 'kalshi'
                 AND market_id IS NOT NULL
                 AND league = ANY($1::text[])
               GROUP BY market_id
            ),
            linked AS (
              SELECT u.market_id,
                     u.open_trades,
                     u.league,
                     (SELECT d.game_id
                        FROM swarm_debates d
                       WHERE ((d.game_context->>'kalshi_market_id') = u.market_id
                              OR d.game_id = u.market_id)
                         AND d.game_id IS NOT NULL
                       ORDER BY d.created_at DESC
                       LIMIT 1) AS game_id
                FROM soccer_unsettled u
            ),
            latest_state AS (
              SELECT DISTINCT ON (game_id)
                     game_id, status, fetched_at
                FROM sportradar_live_state
               ORDER BY game_id, fetched_at DESC
            ),
            first_final AS (
              SELECT game_id,
                     min(fetched_at) AS first_final_at
                FROM sportradar_live_state
               WHERE lower(status) = ANY($2::text[])
               GROUP BY game_id
            )
            SELECT l.market_id      AS ticker,
                   l.game_id        AS game_id,
                   l.league         AS league,
                   l.open_trades    AS open_trades,
                   ff.first_final_at,
                   ls.status        AS last_status
              FROM linked l
              JOIN latest_state ls ON ls.game_id = l.game_id
              JOIN first_final  ff ON ff.game_id = l.game_id
             WHERE l.game_id IS NOT NULL
               AND lower(ls.status) = ANY($2::text[])
               AND ff.first_final_at < now() - make_interval(secs => $3)
             ORDER BY ff.first_final_at ASC
             LIMIT $4
            """,
            leagues,
            _FINAL_STATUSES_LOWER,
            float(final_hours) * 3600.0,
            int(limit),
        )
    except Exception as e:
        log.warning("find_stuck_markets query failed: %s", e)
        error_log(
            "soccer_settlement_drift.query",
            f"{type(e).__name__}: {e}",
        )
        return []

    out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for r in rows:
        first_final_at = r.get("first_final_at")
        try:
            if first_final_at and first_final_at.tzinfo is None:
                first_final_at = first_final_at.replace(tzinfo=timezone.utc)
            final_for = (
                (now - first_final_at).total_seconds()
                if first_final_at is not None
                else 0.0
            )
        except Exception:
            final_for = 0.0
        out.append(
            {
                "ticker": str(r.get("ticker") or ""),
                "game_id": str(r.get("game_id") or ""),
                "league": (str(r["league"]) if r.get("league") else None),
                "open_trades": int(r.get("open_trades") or 0),
                "first_final_at": first_final_at,
                "final_for_seconds": float(final_for),
                "last_status": (
                    str(r["last_status"]) if r.get("last_status") else None
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Persistence + dispatch
# ---------------------------------------------------------------------------


def _build_message(finding: dict[str, Any], threshold_seconds: float) -> str:
    minutes = finding["final_for_seconds"] / 60.0
    threshold_min = threshold_seconds / 60.0
    return (
        f"Soccer Kalshi market {finding['ticker']} still has "
        f"{finding['open_trades']} un-settled trade(s) — Sportradar "
        f"game {finding['game_id']} ({finding['league'] or 'unknown'}) "
        f"has been '{finding['last_status'] or 'final'}' for "
        f"{minutes:.0f}m (>{threshold_min:.0f}m threshold)."
    )


async def _record_alert_row(
    *, finding: dict[str, Any], threshold_seconds: float, message: str
) -> bool:
    """Insert a `model_health_alerts` row for this finding, suppressing
    open same-(ticker, game_id) duplicates within 24h.

    Returns True if a fresh row was inserted, False when an open row
    already covers this drift.
    """
    ticker = finding["ticker"]
    game_id = finding["game_id"]
    metric_name = f"soccer_settlement_drift:{game_id}"
    try:
        recent = await db.fetchval(
            """
            SELECT 1 FROM model_health_alerts
             WHERE alert_type = 'soccer_settlement_drift'
               AND vertical = $1
               AND metric_name = $2
               AND created_at > now() - interval '24 hours'
             LIMIT 1
            """,
            ticker,
            metric_name,
        )
        if recent:
            return False
        await db.execute(
            """
            INSERT INTO model_health_alerts
                (vertical, alert_type, severity, metric_name,
                 metric_value, threshold, message, acknowledged)
            VALUES ($1, 'soccer_settlement_drift', $2, $3, $4, $5, $6, false)
            ON CONFLICT (id) DO NOTHING
            """,
            ticker,
            "warn",
            metric_name,
            float(finding["final_for_seconds"]),
            float(threshold_seconds),
            message,
        )
        return True
    except Exception as e:
        log.warning(
            "soccer_settlement_drift alert insert failed for %s/%s: %s",
            ticker, game_id, e,
        )
        return False


async def _dispatch_finding(
    *, finding: dict[str, Any], threshold_seconds: float
) -> bool:
    """Persist the alert row and dispatch the operator email.

    Email dispatch is gated by ``dedupe_or_skip`` so a persistent
    drift only pages once per TTL window. Insert + email are
    independently best-effort: an insert failure still tries to send
    the email so the operator hears about the drift.
    """
    message = _build_message(finding, threshold_seconds)
    raised = await _record_alert_row(
        finding=finding, threshold_seconds=threshold_seconds, message=message
    )
    try:
        ok = await alerts.dedupe_or_skip(
            "soccer_settlement_drift",
            f"{finding['ticker']}:{finding['game_id']}",
            ttl_seconds=6 * 3600,
        )
    except Exception as e:  # pragma: no cover - dedupe fails open
        log.warning("soccer_settlement_drift dedupe claim failed: %s", e)
        ok = True
    if ok:
        try:
            first_final_at = finding.get("first_final_at")
            iso = (
                first_final_at.isoformat(timespec="seconds")
                if isinstance(first_final_at, datetime)
                else None
            )
            alerts.soccer_settlement_drift(
                market_ticker=finding["ticker"],
                game_id=finding["game_id"],
                league=finding.get("league"),
                final_for_seconds=float(finding["final_for_seconds"]),
                threshold_seconds=float(threshold_seconds),
                open_trades=int(finding["open_trades"]),
                sportradar_status=finding.get("last_status"),
                first_final_at=iso,
                note=message,
            )
        except Exception as e:  # pragma: no cover
            log.warning(
                "soccer_settlement_drift email dispatch failed for %s: %s",
                finding.get("ticker"), e,
            )
    return raised


async def check_now() -> dict[str, Any]:
    """Run one evaluation pass and return a summary dict.

    Safe to call from the scheduler loop, the test endpoint, or a
    one-shot admin task. Always returns a dict (never raises) so a
    transient DB outage doesn't kill the parent loop.
    """
    final_hours = _final_hours()
    threshold_seconds = final_hours * 3600.0
    out: dict[str, Any] = {
        "checked_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "final_hours": final_hours,
        "findings": [],
        "alerts_raised": 0,
    }
    findings = await find_stuck_markets(
        final_hours=final_hours, limit=_max_findings()
    )
    out["findings"] = [
        {
            "ticker": f["ticker"],
            "game_id": f["game_id"],
            "league": f["league"],
            "open_trades": f["open_trades"],
            "final_for_seconds": round(f["final_for_seconds"], 1),
            "last_status": f["last_status"],
        }
        for f in findings
    ]
    raised_count = 0
    for f in findings:
        try:
            if await _dispatch_finding(
                finding=f, threshold_seconds=threshold_seconds
            ):
                raised_count += 1
        except Exception as e:  # pragma: no cover
            log.warning(
                "soccer_settlement_drift dispatch error for %s: %s",
                f.get("ticker"), e,
            )
    out["alerts_raised"] = raised_count
    return out


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


_task: asyncio.Task | None = None
_stop = asyncio.Event()


async def _loop() -> None:
    interval = _check_interval_secs()
    log.info(
        "soccer_settlement_drift loop started: interval=%ss final_hours=%.2f",
        interval, _final_hours(),
    )
    while not _stop.is_set():
        try:
            await check_now()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover
            log.warning("soccer_settlement_drift loop error: %s", e)
            error_log(
                "soccer_settlement_drift.loop",
                f"{type(e).__name__}: {e}",
            )
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return


async def start() -> None:
    global _task
    if not _enabled():
        log.info("soccer_settlement_drift disabled")
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="soccer-settlement-drift")


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
