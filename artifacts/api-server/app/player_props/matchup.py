"""Sport-agnostic pace + opponent-allowed matchup adjustment (task #829).

Generalised from :mod:`app.wnba.matchup` so NBA / NHL / MLB / NFL all
share the same bounded multiplicative-adjustment helper.  The WNBA
package continues to import its own copy for backward compatibility,
but new leagues plug straight into this module via
:func:`app.player_props.league_specs.get_league_spec`.

Per-component caps and the combined cap mirror the WNBA defaults
(±35% per component, ±25% combined).  The pace fallback comes from
the per-league spec so basketball/hockey/baseball/football use the
right denominator without ad-hoc constants.
"""
from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass
from typing import Iterable

from .. import db
from .league_specs import get_league_spec


log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


DEFAULT_MAX_MATCHUP_ADJ: float = _env_float(
    "AXIOM_PLAYER_PROPS_MAX_MATCHUP_ADJ", 0.25
)
_PER_COMPONENT_CAP: float = 0.35


@dataclass(frozen=True)
class MatchupAdjustment:
    factor: float
    pace_factor: float
    opp_allowed_factor: float
    pace_estimate: float
    league_pace: float
    opp_allowed: float
    league_average: float
    sample_size: int
    bounded: bool

    def to_dict(self) -> dict:
        return {
            "factor": self.factor,
            "pace_factor": self.pace_factor,
            "opp_allowed_factor": self.opp_allowed_factor,
            "pace_estimate": self.pace_estimate,
            "league_pace": self.league_pace,
            "opp_allowed": self.opp_allowed,
            "league_average": self.league_average,
            "sample_size": self.sample_size,
            "bounded": self.bounded,
        }


def _clip(value: float, lo: float, hi: float) -> tuple[float, bool]:
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _stat_values(rows: Iterable[dict], stat: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        v = r.get(stat)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def league_average(
    rows: Iterable[dict], stat: str, *, fallback: float | None = None
) -> float:
    values = _stat_values(rows, stat)
    if not values:
        return float(fallback) if fallback is not None else 0.0
    return float(statistics.fmean(values))


# Composite → base-stat collapse for matchup purposes.  Composite props
# borrow their adjustment from the dominant component (points for
# basketball PRA, hits for MLB TB, etc.).
_COMPOSITE_BASE: dict[str, str] = {
    "points_rebounds_assists": "points",
    "points_rebounds": "points",
    "points_assists": "points",
    "rebounds_assists": "rebounds",
    "steals_blocks": "steals",
    "total_bases": "hits",
    "hockey_points": "shots_on_goal",
    "touchdowns": "receiving_yards",
}


def _base_stat(stat: str) -> str:
    return _COMPOSITE_BASE.get(stat, stat)


def compute_adjustment(
    *,
    stat: str,
    opponent_rows: list[dict],
    league_rows: list[dict],
    pace_estimate: float | None = None,
    league_pace: float | None = None,
    pace_fallback: float = 80.0,
    max_adj: float = DEFAULT_MAX_MATCHUP_ADJ,
    min_opponent_sample: int = 3,
) -> MatchupAdjustment:
    league_pace_v = float(league_pace) if league_pace else float(pace_fallback)
    pace_estimate_v = (
        float(pace_estimate) if pace_estimate else league_pace_v
    )
    if league_pace_v <= 0:
        league_pace_v = float(pace_fallback)

    pace_raw = pace_estimate_v / league_pace_v if league_pace_v else 1.0
    pace_factor, _ = _clip(
        pace_raw, 1 - _PER_COMPONENT_CAP, 1 + _PER_COMPONENT_CAP
    )

    base = _base_stat(stat)
    league_avg = league_average(league_rows, base)
    opp_avg = league_average(opponent_rows, base, fallback=league_avg)

    if league_avg <= 0 or len(opponent_rows) < min_opponent_sample:
        opp_factor = 1.0
    else:
        opp_raw = opp_avg / league_avg
        opp_factor, _ = _clip(
            opp_raw, 1 - _PER_COMPONENT_CAP, 1 + _PER_COMPONENT_CAP
        )

    combined_raw = pace_factor * opp_factor
    combined, bounded = _clip(combined_raw, 1 - max_adj, 1 + max_adj)

    return MatchupAdjustment(
        factor=float(combined),
        pace_factor=float(pace_factor),
        opp_allowed_factor=float(opp_factor),
        pace_estimate=float(pace_estimate_v),
        league_pace=float(league_pace_v),
        opp_allowed=float(opp_avg),
        league_average=float(league_avg),
        sample_size=int(len(opponent_rows)),
        bounded=bool(bounded),
    )


async def fetch_adjustment(
    *,
    league: str,
    stat: str,
    opponent_team: str,
    pace_estimate: float | None = None,
    max_adj: float = DEFAULT_MAX_MATCHUP_ADJ,
) -> MatchupAdjustment | None:
    """SQL-driven wrapper around :func:`compute_adjustment`.  Returns
    None if the league has no stat history at all yet (fresh deploy
    before the per-league Sportradar backfill ships).

    Per-league pace fallbacks come from
    :data:`app.player_props.league_specs.LEAGUE_SPECS`.
    """
    spec = get_league_spec(league)
    pace_fallback = spec.pace_fallback if spec is not None else 80.0
    base_stat = _base_stat(stat)
    try:
        opp_rows = [
            dict(r)
            for r in await db.fetch(
                f"""
                SELECT {base_stat} AS {base_stat}, played_at
                  FROM player_stat_history
                 WHERE league = $1
                   AND opponent ILIKE $2
                   AND played_at > now() - interval '180 days'
                """,
                league.upper(), opponent_team,
            )
        ]
        league_rows = [
            dict(r)
            for r in await db.fetch(
                f"""
                SELECT {base_stat} AS {base_stat}
                  FROM player_stat_history
                 WHERE league = $1
                   AND played_at > now() - interval '180 days'
                """,
                league.upper(),
            )
        ]
    except Exception as e:  # pragma: no cover — defensive
        log.debug(
            "matchup adjustment read failed for league=%s opp=%s: %s",
            league, opponent_team, e,
        )
        return None
    if not league_rows:
        return None
    league_pace = None
    if pace_estimate is None:
        try:
            v = await db.fetchval(
                """
                SELECT avg(pace_estimate)::float
                  FROM player_stat_history
                 WHERE league = $1
                   AND played_at > now() - interval '180 days'
                   AND pace_estimate IS NOT NULL
                """,
                league.upper(),
            )
            if v is not None:
                league_pace = float(v)
        except Exception as e:  # pragma: no cover
            log.debug("league pace lookup failed: %s", e)
    return compute_adjustment(
        stat=stat,
        opponent_rows=opp_rows,
        league_rows=league_rows,
        pace_estimate=pace_estimate,
        league_pace=league_pace,
        pace_fallback=pace_fallback,
        max_adj=max_adj,
    )
