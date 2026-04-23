"""Periodic worker that feeds finished games into the Elo updater.

The settlement pipeline only handles binary Kalshi market resolution —
it never calls `record_game()`.  This scheduler closes that gap: every
`AXIOM_ELO_CYCLE_SECONDS` (default 5 minutes) it scans the `games`
table for finished matchups that don't yet have a `team_elo_history`
row and invokes `record_game()` for each.

Idempotency is preserved end-to-end:
  * the SQL `NOT EXISTS` filter skips games that have already been
    recorded, so each cycle only does work for genuinely new finals;
  * `record_game()` is itself idempotent on (league, team, game_id),
    so the rare race where two workers grab the same game collapses
    to a single ratings update.

Per-cycle counters (`games_examined`, `games_updated`, `teams_touched`)
are logged at INFO level when any updates land and exposed via
`scheduler.status()` so the operator dashboard can render them.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import db
from . import ensure_schema, record_game


log = logging.getLogger(__name__)


DEFAULT_CYCLE_SECONDS: float = float(
    os.environ.get("AXIOM_ELO_CYCLE_SECONDS", "300")
)
DEFAULT_BATCH_LIMIT: int = int(
    os.environ.get("AXIOM_ELO_BATCH_LIMIT", "200")
)

# Statuses the feed pollers stamp on finished games.  Keep in sync with
# `app/margin_regressor.py`'s training query so the same set of games
# that backs the regressor also feeds the Elo updater.
FINISHED_STATUSES: tuple[str, ...] = ("closed", "final", "complete", "completed")


@dataclass
class CycleResult:
    started_at: str
    completed_at: str | None = None
    duration_seconds: float = 0.0
    games_examined: int = 0
    games_updated: int = 0
    games_skipped: int = 0
    teams_touched: int = 0
    errors: list[str] = field(default_factory=list)


def _league_for_game(row: dict[str, Any]) -> str | None:
    league = (row.get("league") or "").strip()
    if league:
        return league
    sport = (row.get("sport") or "").strip().upper()
    return sport or None


async def _fetch_unrecorded_finished_games(limit: int) -> list[dict[str, Any]]:
    """Finished games we haven't yet fed into the Elo engine.

    LEFT-anti-joins `team_elo_history` on `game_id` so once *either*
    perspective row exists for a game we never re-scan it.  Cheap and
    exact — no watermark to drift, no batch needs to remember state.
    """
    sql = """
    SELECT g.game_id, g.league, g.sport, g.home_team, g.away_team,
           g.home_score, g.away_score, g.scheduled_start, g.last_updated
      FROM games g
     WHERE g.status = ANY($1::text[])
       AND g.home_score IS NOT NULL
       AND g.away_score IS NOT NULL
       AND g.home_team  IS NOT NULL
       AND g.away_team  IS NOT NULL
       AND g.home_team <> ''
       AND g.away_team <> ''
       AND NOT EXISTS (
           SELECT 1 FROM team_elo_history h
            WHERE h.game_id = g.game_id
       )
     ORDER BY coalesce(g.last_updated, g.scheduled_start) ASC NULLS LAST
     LIMIT $2
    """
    try:
        rows = await db.fetch(sql, list(FINISHED_STATUSES), int(limit))
    except Exception as e:
        log.debug("elo updater fetch failed: %s", e)
        return []
    return [dict(r) for r in rows]


def _coerce_played_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


async def run_cycle(*, batch_limit: int = DEFAULT_BATCH_LIMIT) -> CycleResult:
    """Scan for newly-finished games and call `record_game()` for each."""
    started = datetime.now(timezone.utc)
    cr = CycleResult(started_at=started.isoformat())

    try:
        await ensure_schema()
    except Exception as e:
        cr.errors.append(f"ensure_schema: {e}")

    games = await _fetch_unrecorded_finished_games(batch_limit)
    cr.games_examined = len(games)

    for g in games:
        league = _league_for_game(g)
        if not league:
            cr.games_skipped += 1
            continue
        played_at = _coerce_played_at(g.get("last_updated") or g.get("scheduled_start"))
        try:
            update = await record_game(
                league=str(league),
                home_team=str(g["home_team"]),
                away_team=str(g["away_team"]),
                home_score=float(g["home_score"]),
                away_score=float(g["away_score"]),
                game_id=str(g["game_id"]),
                played_at=played_at,
            )
            if update.skipped:
                cr.games_skipped += 1
            else:
                cr.games_updated += 1
                cr.teams_touched += 2
        except Exception as e:
            cr.errors.append(f"{g.get('game_id')}: {e}")
            log.warning("elo record_game failed for %s: %s", g.get("game_id"), e)

    completed = datetime.now(timezone.utc)
    cr.completed_at = completed.isoformat()
    cr.duration_seconds = (completed - started).total_seconds()
    if cr.games_updated or cr.errors:
        log.info(
            "elo cycle: examined=%d updated=%d skipped=%d teams=%d errors=%d duration=%.2fs",
            cr.games_examined, cr.games_updated, cr.games_skipped,
            cr.teams_touched, len(cr.errors), cr.duration_seconds,
        )
    return cr


class _EloScheduler:
    """Long-running background loop wrapper around `run_cycle`.

    Same shape as the other app schedulers (`feed_scheduler`,
    `settlement_scheduler`, ...): start/stop/status + run_now, with
    exception backoff so a sick DB never kills the loop.
    """

    def __init__(self, cycle_seconds: float = DEFAULT_CYCLE_SECONDS) -> None:
        self.cycle_seconds = cycle_seconds
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._last_cycle: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        return {
            "running": self._task is not None and not self._task.done(),
            "cycle_seconds": self.cycle_seconds,
            "last_cycle": self._last_cycle,
        }

    async def start(self) -> None:
        if os.environ.get("AXIOM_ELO_UPDATER_DISABLED", "").lower() in ("1", "true", "yes"):
            log.info("elo updater disabled via AXIOM_ELO_UPDATER_DISABLED")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="elo-updater")
        log.info("elo updater scheduler started: interval=%ss", self.cycle_seconds)

    async def stop(self) -> None:
        self._stop.set()
        t = self._task
        self._task = None
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.warning("elo updater stop error: %s", e)
        log.info("elo updater scheduler stopped")

    async def run_now(self) -> CycleResult:
        cr = await run_cycle()
        self._last_cycle = _summarize(cr)
        return cr

    async def _loop(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            try:
                cr = await run_cycle()
                self._last_cycle = _summarize(cr)
                backoff = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("elo updater cycle failed: %s", e)
                backoff = min(300.0, max(15.0, backoff * 2 if backoff else 15.0))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.cycle_seconds + backoff
                )
            except asyncio.TimeoutError:
                pass
            else:
                return


def _summarize(cr: CycleResult) -> dict[str, Any]:
    return {
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "games_examined": cr.games_examined,
        "games_updated": cr.games_updated,
        "games_skipped": cr.games_skipped,
        "teams_touched": cr.teams_touched,
        "errors": list(cr.errors),
    }


scheduler = _EloScheduler()


__all__ = ["scheduler", "run_cycle", "CycleResult", "FINISHED_STATUSES"]
