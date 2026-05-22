"""Per-player baseline distribution for any player-prop vertical.

For a ``(league, player, stat)`` triple this returns a 3-component
descriptor:

* ``rolling_mean`` — mean of the last N games (default 10).
* ``season_median`` — robust central tendency over the full window.
* ``mad_sigma`` — robust scale via 1.4826 * median-absolute-deviation
  so a single 35-point outlier doesn't blow up the variance.

Returns ``None`` when the player has fewer than ``min_sample`` games
in the window — the calling predictor must surface "insufficient
sample" rather than ship a fabricated distribution.

Stat names accepted: ``points``, ``rebounds``, ``assists``, ``threes``,
``blocks``, ``steals``, ``turnovers``, plus the composite buckets
``points_rebounds_assists``, ``points_rebounds``, ``points_assists``
and ``rebounds_assists`` (computed at read time so we don't have to
materialise extra columns).
"""
from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .. import db


log = logging.getLogger(__name__)


_BASE_STATS: tuple[str, ...] = (
    # Basketball (WNBA / NBA)
    "points",
    "rebounds",
    "assists",
    "threes",
    "blocks",
    "steals",
    "turnovers",
    # NHL (task #829)
    "shots_on_goal",
    "goals",
    "hockey_assists",
    "hockey_points",
    "saves",
    # MLB (task #829)
    "hits",
    "total_bases",
    "strikeouts",
    "earned_runs",
    "outs_recorded",
    # NFL (task #829)
    "passing_yards",
    "rushing_yards",
    "receiving_yards",
    "receptions",
    "touchdowns",
)
_COMPOSITE_STATS: dict[str, tuple[str, ...]] = {
    "points_rebounds_assists": ("points", "rebounds", "assists"),
    "points_rebounds": ("points", "rebounds"),
    "points_assists": ("points", "assists"),
    "rebounds_assists": ("rebounds", "assists"),
    # NBA STL+BLK composite per task #829.
    "steals_blocks": ("steals", "blocks"),
}
SUPPORTED_STATS: tuple[str, ...] = _BASE_STATS + tuple(_COMPOSITE_STATS.keys())


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


DEFAULT_MIN_SAMPLE: int = _env_int("AXIOM_WNBA_PROPS_MIN_SAMPLE", 5)
DEFAULT_ROLLING_WINDOW: int = 10
DEFAULT_LOOKBACK_DAYS: int = 540  # 1.5 seasons covers WNBA + offseason


@dataclass(frozen=True)
class BaselineDistribution:
    league: str
    player: str
    stat: str
    sample_count: int
    rolling_mean: float
    season_median: float
    mad_sigma: float
    last_played_at: datetime | None
    rolling_window_used: int

    def projected(self) -> float:
        """Best single-point projection: rolling mean (recency-weighted)."""
        return float(self.rolling_mean)

    def sigma(self) -> float:
        """Robust σ; floor at a small positive so the predictor doesn't
        blow up when a player has zero variance over the window
        (e.g. logged 0 threes every game)."""
        return max(0.5, float(self.mad_sigma))

    def to_dict(self) -> dict:
        return {
            "league": self.league,
            "player": self.player,
            "stat": self.stat,
            "sample_count": self.sample_count,
            "rolling_mean": self.rolling_mean,
            "season_median": self.season_median,
            "mad_sigma": self.mad_sigma,
            "last_played_at": (
                self.last_played_at.isoformat()
                if isinstance(self.last_played_at, datetime)
                else None
            ),
            "rolling_window_used": self.rolling_window_used,
        }


def _mad(values: list[float]) -> float:
    """Median absolute deviation × 1.4826 (≈ σ for normal data)."""
    if not values:
        return 0.0
    med = statistics.median(values)
    devs = [abs(v - med) for v in values]
    return 1.4826 * statistics.median(devs)


def summarize(
    league: str,
    player: str,
    stat: str,
    rows: Iterable[dict],
    *,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    rolling_window: int = DEFAULT_ROLLING_WINDOW,
) -> BaselineDistribution | None:
    """Pure-Python summarizer split out so the SQL-touching call path
    is trivial to test with synthetic rows."""
    if stat not in SUPPORTED_STATS:
        return None
    rows = list(rows)
    if len(rows) < min_sample:
        return None

    components = _COMPOSITE_STATS.get(stat)

    def _value(row: dict) -> float | None:
        if components is None:
            v = row.get(stat)
        else:
            parts = [row.get(c) for c in components]
            if any(p is None for p in parts):
                return None
            v = sum(float(p) for p in parts)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # Most-recent-first ordering is the caller's contract.
    valued: list[tuple[datetime | None, float]] = []
    for r in rows:
        v = _value(r)
        if v is None:
            continue
        played_at = r.get("played_at")
        if isinstance(played_at, str):
            try:
                played_at = datetime.fromisoformat(played_at.replace("Z", "+00:00"))
            except ValueError:
                played_at = None
        valued.append((played_at, v))
    if len(valued) < min_sample:
        return None

    all_values = [v for _, v in valued]
    rolling_values = [v for _, v in valued[:rolling_window]]
    rolling_window_used = len(rolling_values)

    rolling_mean = statistics.fmean(rolling_values)
    season_median = statistics.median(all_values)
    mad_sigma = _mad(all_values)

    last_played_at = next((d for d, _ in valued if isinstance(d, datetime)), None)
    if last_played_at is not None and last_played_at.tzinfo is None:
        last_played_at = last_played_at.replace(tzinfo=timezone.utc)

    return BaselineDistribution(
        league=league.upper(),
        player=player,
        stat=stat,
        sample_count=len(valued),
        rolling_mean=float(rolling_mean),
        season_median=float(season_median),
        mad_sigma=float(mad_sigma),
        last_played_at=last_played_at,
        rolling_window_used=rolling_window_used,
    )


async def fetch_baseline(
    league: str,
    player: str,
    stat: str,
    *,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    rolling_window: int = DEFAULT_ROLLING_WINDOW,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> BaselineDistribution | None:
    """Read recent box-score rows from ``player_stat_history`` and
    summarise them.  Returns None when sample is insufficient or the
    table is empty (fresh deploy with no backfill yet)."""
    if stat not in SUPPORTED_STATS:
        return None
    try:
        # SELECT * so newly-added per-league columns (NHL saves, MLB
        # outs_recorded, NFL passing_yards …) are picked up without
        # touching this query each time the schema grows.  The
        # summariser is dict-driven and ignores unknown columns.
        rows = await db.fetch(
            """
            SELECT *
              FROM player_stat_history
             WHERE league = $1
               AND player ILIKE $2
               AND played_at > now() - ($3 || ' days')::interval
             ORDER BY played_at DESC
             LIMIT 200
            """,
            league.upper(),
            player,
            str(int(lookback_days)),
        )
    except Exception as e:  # pragma: no cover — defensive; fresh DB
        log.debug("player_stat_history read failed for %s/%s: %s", league, player, e)
        return None
    return summarize(
        league,
        player,
        stat,
        [dict(r) for r in rows],
        min_sample=min_sample,
        rolling_window=rolling_window,
    )
