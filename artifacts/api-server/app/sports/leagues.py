"""Per-league trading config for the sports vertical.

The sports `VerticalSpec` carries a single `default_allocation_pct`,
which is fine for an aggregate cap but doesn't cover league-specific
risk profiles. NFL in particular needs:

  * a tighter per-league bankroll cap (5%) because game volume is
    concentrated on a single Sunday slate;
  * Sunday-window concentration cap (limit total NFL exposure during
    the kickoff-cluster hours where correlated outcomes spike risk);
  * blackout windows around the inactive-list deadline (90 minutes
    pre-kickoff for Sunday games) where roster news is in flight and
    pricing is unreliable.

The same `LeagueConfig` shape is reused for the rest of the supported
leagues so future verticals (international soccer, combat sports, etc.)
plug in by adding entries here, not by editing the gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone, timedelta
from typing import Iterable


# ET (America/New_York) is the canonical schedule timezone for North
# American sports. We approximate by applying a fixed UTC-5 offset to
# the input timestamp's date — DST drift here only shifts the boundary
# by an hour, which is well inside our 30-minute Sunday window padding.
_ET_OFFSET = timedelta(hours=-5)


@dataclass(frozen=True)
class LeagueConfig:
    """Per-league trading knobs."""

    league: str  # canonical uppercase name (NFL, NBA, ...)
    priority: int  # 1 = highest, 10 = lowest; orchestrator hint
    allocation_pct: float  # share of bankroll cap for this league
    weather_sensitive: bool = False
    sunday_window_cap_pct: float | None = None  # None disables
    # Sunday-window is approximated as the ET kickoff-cluster band. NFL
    # ships with 12:30-19:30 ET to cover early/late/SNF kickoffs.
    sunday_window_start_et: time = field(default=time(12, 30))
    sunday_window_end_et: time = field(default=time(19, 30))
    # Per-game (per-matchup) notional cap as a share of bankroll.
    per_game_cap_pct: float | None = None
    # Pre-kickoff blackout: minutes before kickoff during which we
    # refuse new positions on a market.  Mainly useful for leagues that
    # publish inactive lists close to game time (NFL: 90m).
    pre_kickoff_blackout_minutes: int = 0
    # Free-form persona prompt augment when this league is on the
    # candidate.  Injected by `app.sports.persona_context_augment` so the
    # injury_roster + matchup_historian personas reason about the right
    # signals.
    persona_context: str = ""
    # 3-way moneyline (home / draw / away) — soccer leagues, including
    # cup competitions.  Settlement / margin-regressor / per-side caps
    # gate on this to avoid treating a draw outcome as a 2-way miss.
    three_way_market: bool = False
    # Per-league daily trade count cap.  When set, gate_per_vertical_cap
    # rejects new candidates after the cap is reached for the UTC day.
    # None disables (default — no cap).  EPL ships at 10/day per spec
    # because the league has 10 fixtures per matchweek and we don't want
    # the entire bankroll churning through one matchday.
    max_daily_trades: int | None = None


_SOCCER_PERSONA_CONTEXT = (
    "SOCCER-SPECIFIC SIGNALS:\n"
    "  * Markets are 3-way (home / draw / away) — a 2-way moneyline frame "
    "will systematically misprice draw-prone matchups (low xG, defensive "
    "managers, derbies).  Reason about the draw share explicitly.\n"
    "  * Confirmed lineups land ~60 minutes before kickoff and dominate "
    "short-term price moves; rotation, suspensions and 'rested' starters "
    "(midweek UCL, international break, congested fixture lists) are the "
    "primary edge sources.\n"
    "  * xG and shot-quality differentials over the last 6-10 matches "
    "predict win probability better than table position.  Anchor on xG, "
    "then nudge for matchup, venue and referee.\n"
    "  * Set-pieces account for ~30% of goals — corner counts, free-kick "
    "specialists and aerial duel rates carry signal that totals models "
    "miss.\n"
    "  * Late-window markets are extremely thin in pre-season, "
    "international breaks and final matchday dead-rubber fixtures; "
    "default toward PASS when liquidity is absent."
)


LEAGUE_CONFIGS: dict[str, LeagueConfig] = {
    "NFL": LeagueConfig(
        league="NFL",
        priority=5,
        allocation_pct=0.05,
        weather_sensitive=True,
        sunday_window_cap_pct=0.03,
        sunday_window_start_et=time(12, 30),
        sunday_window_end_et=time(19, 30),
        per_game_cap_pct=0.015,
        pre_kickoff_blackout_minutes=90,
        persona_context=(
            "NFL-SPECIFIC SIGNALS:\n"
            "  * Inactive list publishes 90 minutes before kickoff — late OL/DL "
            "scratches can swing a spread by 1-2 points.\n"
            "  * Snap counts last week tell you who is actually on the field; "
            "starter usage on the depth chart can be misleading.\n"
            "  * Weather (wind > 15mph, sustained precipitation, sub-20F game-time "
            "temp) compresses scoring and favors UNDER + the run-game side.\n"
            "  * Sunday slate has high cross-game correlation (weather fronts, "
            "ref crews, late-window bye-week swings) — size accordingly.\n"
            "  * Pace + OL injuries dominate the totals market more than the "
            "moneyline; weight your probability accordingly when market_type=total."
        ),
    ),
    "NBA": LeagueConfig(
        league="NBA",
        priority=4,
        allocation_pct=0.10,
        per_game_cap_pct=0.025,
        pre_kickoff_blackout_minutes=30,
        persona_context=(
            "NBA-SPECIFIC SIGNALS:\n"
            "  * Late star scratches and back-to-back rest are the dominant "
            "edge sources — confirm starting lineups before sizing.\n"
            "  * Pace differentials drive totals; coach-on-coach history matters "
            "more than season-long offensive rating."
        ),
    ),
    "MLB": LeagueConfig(
        league="MLB",
        priority=6,
        allocation_pct=0.08,
        weather_sensitive=True,
        per_game_cap_pct=0.02,
        pre_kickoff_blackout_minutes=15,
        persona_context=(
            "MLB-SPECIFIC SIGNALS:\n"
            "  * Starting pitcher confirmation is the single biggest input; "
            "a late SP swap rewrites the line.\n"
            "  * Park factors + wind direction + umpire strike-zone tendencies "
            "should anchor your totals lean."
        ),
    ),
    "NHL": LeagueConfig(
        league="NHL",
        priority=6,
        allocation_pct=0.08,
        per_game_cap_pct=0.02,
        pre_kickoff_blackout_minutes=15,
        persona_context=(
            "NHL-SPECIFIC SIGNALS:\n"
            "  * Confirmed starting goaltender is the dominant input; a 'TBD' "
            "tag should compress your confidence."
        ),
    ),
    "NCAAB": LeagueConfig(
        league="NCAAB",
        priority=7,
        allocation_pct=0.05,
        per_game_cap_pct=0.015,
        pre_kickoff_blackout_minutes=15,
    ),
    "NCAAF": LeagueConfig(
        league="NCAAF",
        priority=7,
        allocation_pct=0.05,
        weather_sensitive=True,
        per_game_cap_pct=0.015,
        pre_kickoff_blackout_minutes=30,
    ),
    "MLS": LeagueConfig(
        league="MLS",
        priority=8,
        allocation_pct=0.02,
        per_game_cap_pct=0.01,
        three_way_market=True,
        max_daily_trades=8,
        persona_context=_SOCCER_PERSONA_CONTEXT,
    ),
    "EPL": LeagueConfig(
        league="EPL",
        priority=5,
        allocation_pct=0.03,
        per_game_cap_pct=0.01,
        three_way_market=True,
        max_daily_trades=10,
        persona_context=_SOCCER_PERSONA_CONTEXT,
    ),
    "LALIGA": LeagueConfig(
        league="LALIGA",
        priority=6,
        allocation_pct=0.02,
        per_game_cap_pct=0.008,
        three_way_market=True,
        max_daily_trades=8,
        persona_context=_SOCCER_PERSONA_CONTEXT,
    ),
    "SERIEA": LeagueConfig(
        league="SERIEA",
        priority=6,
        allocation_pct=0.02,
        per_game_cap_pct=0.008,
        three_way_market=True,
        max_daily_trades=8,
        persona_context=_SOCCER_PERSONA_CONTEXT,
    ),
    "BUNDES": LeagueConfig(
        league="BUNDES",
        priority=6,
        allocation_pct=0.02,
        per_game_cap_pct=0.008,
        three_way_market=True,
        max_daily_trades=8,
        persona_context=_SOCCER_PERSONA_CONTEXT,
    ),
    "LIGUE1": LeagueConfig(
        league="LIGUE1",
        priority=7,
        allocation_pct=0.02,
        per_game_cap_pct=0.008,
        three_way_market=True,
        max_daily_trades=8,
        persona_context=_SOCCER_PERSONA_CONTEXT,
    ),
    "UCL": LeagueConfig(
        league="UCL",
        priority=4,
        allocation_pct=0.01,
        per_game_cap_pct=0.005,
        three_way_market=True,
        max_daily_trades=6,
        persona_context=_SOCCER_PERSONA_CONTEXT,
    ),
}


def get_league_config(league: str | None) -> LeagueConfig | None:
    """Lookup config for a league name (case-insensitive). None when
    the league is unknown — callers must treat that as "no per-league
    overrides apply"."""
    if not league:
        return None
    return LEAGUE_CONFIGS.get(str(league).strip().upper())


def _to_et(dt: datetime) -> datetime:
    """Convert any aware/naive datetime to the approximate ET wall
    clock used for Sunday-window detection."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc) + _ET_OFFSET


def is_sunday_window(league: str | None, when: datetime | None = None) -> bool:
    """True iff `when` falls inside the league's Sunday concentration
    window. False for leagues with no configured window."""
    cfg = get_league_config(league)
    if cfg is None or cfg.sunday_window_cap_pct is None:
        return False
    when = when or datetime.now(timezone.utc)
    et = _to_et(when)
    if et.weekday() != 6:  # 6 = Sunday in Python
        return False
    t = et.time()
    return cfg.sunday_window_start_et <= t <= cfg.sunday_window_end_et


def league_blackout_until(
    league: str | None, kickoff: datetime | None
) -> datetime | None:
    """Returns the UTC datetime through which the market is blacked out
    by the league's pre-kickoff inactive-list rule, or None when no
    blackout applies (no kickoff known, league has no rule, or we're
    already past kickoff).
    """
    cfg = get_league_config(league)
    if cfg is None or not cfg.pre_kickoff_blackout_minutes or kickoff is None:
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    blackout_start = kickoff - timedelta(minutes=cfg.pre_kickoff_blackout_minutes)
    now = datetime.now(timezone.utc)
    if now < blackout_start or now >= kickoff:
        return None
    return kickoff  # blacked out until kickoff


def persona_context_augment(league: str | None) -> str:
    """Persona-prompt augment block for `league`. Empty string when no
    augment is configured — keeps prompts clean for legacy leagues."""
    cfg = get_league_config(league)
    if cfg is None:
        return ""
    return cfg.persona_context
