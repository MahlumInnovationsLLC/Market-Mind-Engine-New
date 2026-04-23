"""Scheduler entry that periodically tops up `team_elo` from finished games.

The one-shot operator CLI (`scripts/seed_team_elo.py`) handles the
initial 5-year backfill from a CSV.  This feed keeps ratings warm
between full reloads by replaying finished rows already cached in the
local `games` table — newly-settled games picked up by the Sportradar
pollers will land here on the next tick.

Default cadence: 24h.  The feed is registered as
`enabled=False` unless `AXIOM_TEAM_ELO_AUTOSEED=1` so it never runs
silently in environments where operators want to drive seeding by hand.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from .. import db, elo as _elo
from .registry import Feed, registry


log = logging.getLogger("axiom.feeds.team_elo_seed")


_FINISHED_STATUSES = (
    "closed", "complete", "completed", "final", "finished", "ended", "ft",
)


def _to_utc(dt) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _load_finished_games_from_db(since_hours: int) -> list[dict]:
    """Return finished `games` rows newer than `since_hours`, oldest-first."""
    try:
        rows = await db.fetch(
            """
            SELECT game_id, league, home_team, away_team,
                   home_score, away_score, scheduled_start
              FROM games
             WHERE home_score IS NOT NULL
               AND away_score IS NOT NULL
               AND home_team IS NOT NULL AND home_team <> ''
               AND away_team IS NOT NULL AND away_team <> ''
               AND lower(coalesce(status, '')) = ANY($1::text[])
               AND scheduled_start >= NOW() - ($2 || ' hours')::interval
             ORDER BY scheduled_start ASC
            """,
            list(_FINISHED_STATUSES), str(int(since_hours)),
        )
    except Exception as e:
        log.warning("team_elo_seed db fetch failed: %s", e)
        return []
    out: list[dict] = []
    for r in rows:
        try:
            played_at = r["scheduled_start"]
            if played_at is None:
                continue
            out.append({
                "league": str(r["league"] or "").strip(),
                "home_team": str(r["home_team"]).strip(),
                "away_team": str(r["away_team"]).strip(),
                "home_score": float(r["home_score"]),
                "away_score": float(r["away_score"]),
                "game_id": str(r["game_id"]).strip(),
                "played_at": _to_utc(played_at),
            })
        except Exception as e:
            log.warning("team_elo_seed row skipped (%s): %s", r.get("game_id"), e)
    return out


async def _poll_team_elo_seed() -> int:
    """Replay newly-finished games into team_elo / team_elo_history.

    Returns the count of games actually applied (skipped duplicates do
    not count toward `last_records_written`).
    """
    since_hours = int(os.environ.get("AXIOM_TEAM_ELO_AUTOSEED_HOURS") or 48)
    games = await _load_finished_games_from_db(since_hours=since_hours)
    if not games:
        return 0
    res = await _elo.seed_from_history(games)
    if res.errors:
        log.warning("team_elo_seed produced %d errors (first: %s)",
                    len(res.errors), res.errors[0])
    return res.games_processed


def register_team_elo_seed_feed() -> None:
    """Register the periodic Elo top-up job.

    Disabled by default — operators opt in by setting
    `AXIOM_TEAM_ELO_AUTOSEED=1` so production deployments don't start
    auto-replaying without an explicit choice.  When disabled, the feed
    still appears in `/api/feeds/status` with a clear `skipped_reason`
    so the operator console surfaces the gap.
    """
    if registry.status_of("team_elo_seed"):
        return  # already registered
    enabled = os.environ.get("AXIOM_TEAM_ELO_AUTOSEED", "").strip() in ("1", "true", "yes", "on")
    cadence = max(60, int(os.environ.get("AXIOM_TEAM_ELO_AUTOSEED_CADENCE") or str(24 * 3600)))
    registry.register(Feed(
        name="team_elo_seed",
        cadence_seconds=float(cadence),
        poll=_poll_team_elo_seed,
        enabled=enabled,
        skipped_reason=None if enabled else "AXIOM_TEAM_ELO_AUTOSEED not set",
    ))


__all__ = ["register_team_elo_seed_feed"]
