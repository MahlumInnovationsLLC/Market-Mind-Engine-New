"""Player availability lookup used by the player-prop risk gate.

Reads ``player_injury_status`` rows populated by the per-league
injury feed (ESPN public injury endpoint for WNBA).  The shape mirrors
:func:`app.sports.nfl_context.fetch_player_status` so future
player-prop verticals (NBA / NFL / MLB / NHL) consume the same
primitive.

Status values follow ESPN's vocabulary: ``OUT``, ``QUESTIONABLE``,
``PROBABLE``, ``DAY-TO-DAY``, ``DOUBTFUL``, ``GAME TIME DECISION``,
``ACTIVE``.  ``is_blocking_status`` returns True for any status that
should reject a prop ticket (out / doubtful / questionable / GTD by
default — operators can opt to relax this via env).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable

from .. import db


log = logging.getLogger(__name__)


_DEFAULT_BLOCKING: frozenset[str] = frozenset({
    "OUT",
    "DOUBTFUL",
    "QUESTIONABLE",
    "GAME TIME DECISION",
    "GTD",
})


def _blocking_set() -> frozenset[str]:
    """Env-overridable blocking set so an operator can opt to keep
    QUESTIONABLE props alive once they have enough live data to know
    the league handles GTDs honestly."""
    raw = (os.environ.get("AXIOM_PLAYER_PROPS_BLOCKING_STATUSES") or "").strip()
    if not raw:
        return _DEFAULT_BLOCKING
    return frozenset(s.strip().upper() for s in raw.split(",") if s.strip())


@dataclass(frozen=True)
class PlayerStatus:
    league: str
    player: str
    team: str | None
    status: str
    description: str | None

    def to_dict(self) -> dict:
        return {
            "league": self.league,
            "player": self.player,
            "team": self.team,
            "status": self.status,
            "description": self.description,
        }


def is_blocking_status(status: str | None) -> bool:
    if not status:
        return False
    return status.strip().upper() in _blocking_set()


async def fetch_player_status(
    league: str, player: str
) -> PlayerStatus | None:
    """Latest known status for ``(league, player)``.  Returns ``None``
    when no row exists — callers MUST treat that as "unknown" rather
    than implicitly assume "active"; the gate's default is to ship the
    prop in that case but the dashboard renders an explicit
    ``status='unknown'`` so operators can audit."""
    try:
        row = await db.fetchrow(
            """
            SELECT player, team, status, description
              FROM player_injury_status
             WHERE league = $1
               AND player ILIKE $2
             ORDER BY fetched_at DESC
             LIMIT 1
            """,
            league.upper(),
            player,
        )
    except Exception as e:  # pragma: no cover — fresh DB / table absent
        log.debug(
            "player_injury_status read failed for %s/%s: %s",
            league, player, e,
        )
        return None
    if row is None:
        return None
    return PlayerStatus(
        league=league.upper(),
        player=row["player"] or player,
        team=row["team"],
        status=(row["status"] or "").strip().upper() or "UNKNOWN",
        description=row["description"],
    )


async def fetch_team_statuses(
    league: str, players: Iterable[str]
) -> dict[str, PlayerStatus]:
    """Bulk-lookup helper for the dashboard endpoint."""
    out: dict[str, PlayerStatus] = {}
    for p in players:
        s = await fetch_player_status(league, p)
        if s is not None:
            out[p] = s
    return out
