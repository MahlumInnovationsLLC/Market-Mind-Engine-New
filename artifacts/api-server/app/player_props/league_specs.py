"""Per-league player-prop specifications (task #829).

A single registry that ties together every per-league knob the
generalized predictor/matchup/dashboard needs, so adding a fifth
sport reduces to one entry in this table rather than a copy-paste
of the WNBA package.

Each :class:`PlayerPropLeagueSpec` carries:

* ``league``   — canonical uppercase league code (NBA / NHL / MLB / NFL …)
* ``vertical`` — orchestrator vertical name (matches
  ``VerticalSpec.name`` and ``ctx.vertical``)
* ``markets``  — tuple of stat codes the vertical knows how to price
  (subset of :data:`app.player_props.baseline.SUPPORTED_STATS`)
* ``calibrator_bucket`` — key passed to :func:`app.calibrators.apply`.
  We register a single per-league bucket per spec (``NBA_PROPS`` …);
  per-stat sub-bucketing is a follow-up once each league accrues
  enough settled props for a stable isotonic fit.
* ``pace_fallback`` — league-average possessions/40 (basketball) or
  expected per-game share denominator used when matchup data is thin.
* ``persona_key`` — sticky persona profile id (mirrored from
  ``persona_profile`` on the vertical spec).
* ``inactive_window_minutes`` — minutes pre-tipoff during which the
  per-league inactive list is the dominant short-term mover; used by
  the dashboard to surface a "blackout active" badge.  Generalised
  from the NFL 90-minute inactive-list cooldown (task #823 / #829).
* ``injury_status_label`` — human label for the per-league
  cooldown style (NBA GTD / NHL morning-skate / MLB lineup-card
  scratches / NFL inactive list).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class PlayerPropLeagueSpec:
    league: str
    vertical: str
    markets: tuple[str, ...]
    calibrator_bucket: str
    pace_fallback: float
    persona_key: str
    inactive_window_minutes: int
    injury_status_label: str
    description: str


# Per-league stat menus from task #829.
_NBA_MARKETS: tuple[str, ...] = (
    "points", "rebounds", "assists", "threes",
    "points_rebounds_assists", "steals_blocks",
)
_NHL_MARKETS: tuple[str, ...] = (
    "shots_on_goal", "hockey_points", "goals",
    "hockey_assists", "saves",
)
_MLB_MARKETS: tuple[str, ...] = (
    "hits", "total_bases", "strikeouts",
    "earned_runs", "outs_recorded",
)
_NFL_MARKETS: tuple[str, ...] = (
    "passing_yards", "rushing_yards", "receiving_yards",
    "receptions", "touchdowns",
)


LEAGUE_SPECS: Mapping[str, PlayerPropLeagueSpec] = {
    "NBA": PlayerPropLeagueSpec(
        league="NBA",
        vertical="nba_player_props",
        markets=_NBA_MARKETS,
        calibrator_bucket="NBA_PROPS",
        pace_fallback=100.0,  # NBA league-avg possessions/48
        persona_key="nba_player_props",
        inactive_window_minutes=30,
        injury_status_label="NBA GTD",
        description=(
            "NBA player props — PTS / REB / AST / 3PM / PRA / "
            "STL+BLK with GTD cooldown."
        ),
    ),
    "NHL": PlayerPropLeagueSpec(
        league="NHL",
        vertical="nhl_player_props",
        markets=_NHL_MARKETS,
        calibrator_bucket="NHL_PROPS",
        pace_fallback=30.0,  # ~30 shots/team/game NHL avg
        persona_key="nhl_player_props",
        inactive_window_minutes=60,
        injury_status_label="NHL morning skate",
        description=(
            "NHL player props — SOG / Points / Goals / Assists / "
            "Saves with morning-skate cooldown."
        ),
    ),
    "MLB": PlayerPropLeagueSpec(
        league="MLB",
        vertical="mlb_player_props",
        markets=_MLB_MARKETS,
        calibrator_bucket="MLB_PROPS",
        pace_fallback=4.0,  # ~4 PA/game for a starting position player
        persona_key="mlb_player_props",
        inactive_window_minutes=60,
        injury_status_label="MLB lineup card",
        description=(
            "MLB player props — Hits / TB / Ks / ER / Outs with "
            "lineup-card cooldown."
        ),
    ),
    "NFL": PlayerPropLeagueSpec(
        league="NFL",
        vertical="nfl_player_props",
        markets=_NFL_MARKETS,
        calibrator_bucket="NFL_PROPS",
        pace_fallback=65.0,  # ~65 offensive plays/game NFL avg
        persona_key="nfl_player_props",
        inactive_window_minutes=90,
        injury_status_label="NFL inactive list",
        description=(
            "NFL player props — PassYds / RushYds / RecYds / Rec / "
            "TDs with 90-min inactive-list cooldown."
        ),
    ),
}


def get_league_spec(league: str | None) -> PlayerPropLeagueSpec | None:
    if not league:
        return None
    return LEAGUE_SPECS.get(str(league).strip().upper())


def get_vertical_spec(vertical: str | None) -> PlayerPropLeagueSpec | None:
    if not vertical:
        return None
    v = str(vertical).strip().lower()
    for spec in LEAGUE_SPECS.values():
        if spec.vertical == v:
            return spec
    return None


def calibrator_buckets() -> tuple[str, ...]:
    """Per-league calibrator bucket names — registered in
    :data:`app.calibrators.SUPPORTED_LEAGUES` so the calibration
    snapshot loader picks them up."""
    return tuple(s.calibrator_bucket for s in LEAGUE_SPECS.values())
