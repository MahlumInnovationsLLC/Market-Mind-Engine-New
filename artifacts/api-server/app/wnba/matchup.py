"""Pace + opponent-allowed multiplicative adjustment for WNBA player props.

The adjustment is the product of:

* ``pace_factor`` = expected possessions / league-avg possessions, and
* ``opp_allowed_factor`` = (opponent's per-stat allowed rate) /
  (league-avg per-stat rate).

Both factors are clipped per-component (each individually capped so a
single noisy rate doesn't compound) and the **combined** adjustment is
hard-bounded to ``[1 - max_adj, 1 + max_adj]`` so an early-season
sample with 3 games of opponent data can't shift the projected
distribution by more than ±25% (default).  The caller is expected to
treat an adjustment **at the bound** as a quality signal — the player
prop risk gate rejects candidates whose computed adjustment hits the
bound from outside.
"""
from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass
from typing import Iterable

from .. import db


log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# Combined cap: ±25% by default.  Operators can tighten via env.
DEFAULT_MAX_MATCHUP_ADJ: float = _env_float("AXIOM_WNBA_PROPS_MAX_MATCHUP_ADJ", 0.25)
# Per-component cap so neither pace nor opp-allowed alone can exceed
# ±35% before the combined cap kicks in — keeps the algebra honest
# when both push the same direction.
_PER_COMPONENT_CAP: float = 0.35
_PACE_PER_40_FALLBACK: float = 80.0  # WNBA league-average possessions/40


@dataclass(frozen=True)
class MatchupAdjustment:
    """Multiplicative adjustment to apply to the per-player baseline
    projection.  ``factor`` is in absolute terms (1.0 = no change)."""

    factor: float
    pace_factor: float
    opp_allowed_factor: float
    pace_estimate: float
    league_pace: float
    opp_allowed: float
    league_average: float
    sample_size: int  # opponent games used to derive opp_allowed
    bounded: bool  # True iff the combined cap was hit

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
    """League-average per-game rate for ``stat`` over ``rows``.  Used by
    both the matchup helper and the dashboard's "league context" panel."""
    values = _stat_values(rows, stat)
    if not values:
        return float(fallback) if fallback is not None else 0.0
    return float(statistics.fmean(values))


def compute_adjustment(
    *,
    stat: str,
    opponent_rows: list[dict],
    league_rows: list[dict],
    pace_estimate: float | None = None,
    league_pace: float | None = None,
    max_adj: float = DEFAULT_MAX_MATCHUP_ADJ,
    min_opponent_sample: int = 3,
) -> MatchupAdjustment:
    """Pure helper — DB reads happen in the caller.  Lets unit tests
    feed synthetic histories without a Postgres roundtrip."""
    league_pace = float(league_pace) if league_pace else _PACE_PER_40_FALLBACK
    pace_estimate_v = float(pace_estimate) if pace_estimate else league_pace
    if league_pace <= 0:
        league_pace = _PACE_PER_40_FALLBACK

    pace_raw = pace_estimate_v / league_pace if league_pace else 1.0
    pace_factor, _ = _clip(pace_raw, 1 - _PER_COMPONENT_CAP, 1 + _PER_COMPONENT_CAP)

    league_avg = league_average(league_rows, stat)
    opp_avg = league_average(opponent_rows, stat, fallback=league_avg)

    if league_avg <= 0 or len(opponent_rows) < min_opponent_sample:
        # Insufficient opponent sample (or no league baseline) —
        # neutralise the opp-allowed component rather than shipping a
        # ratio of zero.
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
        league_pace=float(league_pace),
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
    before the backfill ships)."""
    base_stat = stat
    if base_stat in ("points_rebounds_assists", "points_rebounds",
                     "points_assists", "rebounds_assists"):
        # Composite props bound their adjustment off the points head —
        # acceptable approximation while we don't track per-stat allowed
        # rates separately.
        base_stat = "points"
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
        max_adj=max_adj,
    )
