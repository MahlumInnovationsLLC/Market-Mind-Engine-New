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
    # N-way winner market — UFC fight card / tennis tournament / F1 race
    # / PGA tournament.  Distinct from `three_way_market` (soccer h/d/a)
    # because settlement and predictor branch on a list of participants
    # rather than a fixed 3-outcome frame.  Combat / individual sports
    # ship this flag (task #197).
    n_way_market: bool = False
    # Per-event concentration cap as a share of bankroll.  Aggregates
    # across every market that shares the same `event_id` (UFC fight
    # night, F1 GP, tennis tournament, PGA tournament) so the bot can't
    # accidentally pile 30% of bankroll on a single weekend's slate.
    # None disables (default — no cap).
    per_event_cap_pct: float | None = None
    # If True, gate_market_cooldown blocks NEW pre-event entries while
    # the underlying event is in-progress (status=live).  Combat /
    # individual sports flip this on so a UFC main-card pick doesn't
    # land mid-fight or an F1 outright after lights-out.  In-play
    # markets are out of scope for #197 so the bot has no business
    # touching the same market once it's live.
    live_action_blackout: bool = False


_SOCCER_PERSONA_CONTEXT = (
    "SOCCER-SPECIFIC SIGNALS:\n"
    "  * Markets are 3-way (home / draw / away) — a 2-way moneyline frame "
    "will systematically misprice draw-prone matchups (low xG, defensive "
    "managers, derbies).  Reason about the draw share explicitly.\n"
    "  * Confirmed lineups land ~60 minutes before kickoff and dominate "
    "short-term price moves; rotation, suspensions and 'rested' starters "
    "(midweek UCL, international break, congested fixture lists) are the "
    "primary edge sources.\n"
    "  * When OBSERVED CONDITIONS carries a per-side `lineup severity` "
    "and a `Net lineup swing` line, treat them as the deterministic "
    "anchor for your win-share nudge: shave the more-depleted side's "
    "win share by its own severity, then apply the net swing direction "
    "to the spread between home and away.  A goalkeeper out is the "
    "single biggest single-player swing.\n"
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


_COMBAT_INDIVIDUAL_PERSONA_CONTEXT = (
    "COMBAT & INDIVIDUAL-SPORT SIGNALS:\n"
    "  * Outcomes are per-fighter / per-player / per-driver — there is "
    "NO 'home / away' frame.  N-way tournament-winner markets (UFC "
    "card winners, F1 race winners, ATP/WTA/PGA outright) require "
    "reasoning about every entrant, not just two sides.  Use rolling "
    "rating priors (Elo / ATP-points / OWGR) and Bradley-Terry / "
    "Plackett-Luce conversion when you sketch a probability.\n"
    "  * Surface / track / course matters as much as raw form: clay vs "
    "grass vs hard for tennis, octagon-vs-cage and weight-class for "
    "UFC, street vs permanent circuit and weather for F1, links vs "
    "parkland and wind for PGA.  Anchor on surface-conditional H2H "
    "before nudging on recent form.\n"
    "  * Recent fitness + travel / time-zone change dominates short-term "
    "edges.  A driver carrying a grid penalty, a fighter cutting weight "
    "the day before, or a tennis player on day-3 of a back-to-back tour "
    "stop should compress the implied probability.\n"
    "  * Live-action blackout: once the event is in-progress (lights-out, "
    "first-round bell, opening tee, first ball), pre-event outright "
    "markets are not eligible — recommend PASS rather than chase a "
    "moving line we cannot price reliably.\n"
    "  * Per-event concentration risk is high — UFC card upsets, F1 "
    "safety-car chaos, tennis upset draws and PGA weather waves can "
    "wipe out the whole slate.  Default toward smaller sizing and "
    "fewer concurrent legs per event."
)


# Task #829: player-prop calibrator buckets are registered as
# pseudo-leagues in ``app.calibrators.SUPPORTED_LEAGUES`` so the
# calibration loader picks them up.  They share the operational
# knobs (allocation_pct, blackout) of their parent league but use
# the per-league player-prop persona context.  Per-game cap is
# kept tight because v1 ships uncalibrated — see
# ``app.verticals.builtins`` for the matching VerticalSpec entries.
_PROPS_PERSONA = (
    "PLAYER-PROPS SIGNALS (task #829):\n"
    "  * Player props are inherently small-sample: rolling-mean + "
    "MAD-σ is the floor, not a ceiling — never project off fewer "
    "than the configured `min_sample` games.\n"
    "  * Per-(league, stat) calibrator buckets ship UNCALIBRATED — "
    "every candidate is blocked by ``gate_uncalibrated_vertical`` "
    "until an operator opts into raw mode via the matching "
    "``risk.uncalibrated_raw_mode_*`` toggle.\n"
    "  * Player props are intentionally OUTSIDE the cross-book "
    "best-line gate (which only fires for vertical='sports') "
    "because per-player-per-stat lines fragment across books in "
    "ways that the team-level gate is not parameterised for."
)


LEAGUE_CONFIGS: dict[str, LeagueConfig] = {
    # Task #829 player-prop calibrator buckets.  Listed first so the
    # invariant test (`test_league_configs_table_covers_every_supported
    # _league`) sees them at the top of the table.
    "NBA_PROPS": LeagueConfig(
        league="NBA_PROPS",
        priority=9,
        allocation_pct=0.03,
        per_game_cap_pct=0.01,
        pre_kickoff_blackout_minutes=30,
        persona_context=_PROPS_PERSONA,
    ),
    "NHL_PROPS": LeagueConfig(
        league="NHL_PROPS",
        priority=9,
        allocation_pct=0.03,
        per_game_cap_pct=0.01,
        pre_kickoff_blackout_minutes=60,
        persona_context=_PROPS_PERSONA,
    ),
    "MLB_PROPS": LeagueConfig(
        league="MLB_PROPS",
        priority=9,
        allocation_pct=0.03,
        per_game_cap_pct=0.01,
        pre_kickoff_blackout_minutes=60,
        persona_context=_PROPS_PERSONA,
    ),
    "NFL_PROPS": LeagueConfig(
        league="NFL_PROPS",
        priority=9,
        allocation_pct=0.03,
        per_game_cap_pct=0.01,
        pre_kickoff_blackout_minutes=90,
        persona_context=_PROPS_PERSONA,
    ),
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
    # WNBA — task #823.  Player-prop vertical only on day one (the
    # `wnba_player_props` VerticalSpec ships disabled).  The conservative
    # allocation_pct + per_game_cap_pct keeps the league cap meaningful
    # the moment the operator opts the vertical in.  Pre-kickoff
    # blackout matches NBA because WNBA inactive lists publish on a
    # similar schedule and rotation news is the dominant short-term
    # mover.
    "WNBA": LeagueConfig(
        league="WNBA",
        priority=8,
        allocation_pct=0.05,
        per_game_cap_pct=0.015,
        pre_kickoff_blackout_minutes=30,
        persona_context=(
            "WNBA-SPECIFIC SIGNALS:\n"
            "  * Roster news + minutes restrictions move player-prop lines "
            "more than spread/total in this league — the rotation is "
            "shorter so a starter-out is a much larger share of available "
            "minutes than in the NBA.\n"
            "  * Pace varies widely game-to-game; opponent pace is the "
            "single biggest single-game adjustment to a player's "
            "rolling-mean projection.\n"
            "  * Sample sizes are small — a 21-2 opening-weekend run is "
            "almost certainly noise.  Anchor on the calibration panel, "
            "not on early-season hit-rate."
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
    # Combat & individual sports (task #197).  All five leagues are
    # event-shaped (one fight night / GP / tournament dominates the
    # weekend) so per-event concentration matters more than per-game,
    # and the bot blacks out NEW pre-event entries once the underlying
    # event is live (in-play prop trading is out of scope for #197).
    "UFC": LeagueConfig(
        league="UFC",
        priority=6,
        allocation_pct=0.02,
        per_game_cap_pct=0.005,           # per fight (head-to-head)
        per_event_cap_pct=0.015,          # whole fight card / numbered event
        n_way_market=False,               # individual fights are 2-way
        live_action_blackout=True,
        max_daily_trades=6,
        persona_context=_COMBAT_INDIVIDUAL_PERSONA_CONTEXT,
    ),
    "ATP": LeagueConfig(
        league="ATP",
        priority=7,
        allocation_pct=0.02,
        per_game_cap_pct=0.005,           # per match (h2h)
        per_event_cap_pct=0.015,          # tournament outright + R32..final
        n_way_market=True,                # tournament-winner markets
        live_action_blackout=True,
        max_daily_trades=8,
        persona_context=_COMBAT_INDIVIDUAL_PERSONA_CONTEXT,
    ),
    "WTA": LeagueConfig(
        league="WTA",
        priority=7,
        allocation_pct=0.02,
        per_game_cap_pct=0.005,
        per_event_cap_pct=0.015,
        n_way_market=True,
        live_action_blackout=True,
        max_daily_trades=8,
        persona_context=_COMBAT_INDIVIDUAL_PERSONA_CONTEXT,
    ),
    "F1": LeagueConfig(
        league="F1",
        priority=6,
        allocation_pct=0.015,
        per_game_cap_pct=0.005,           # per session (qualifying / race)
        per_event_cap_pct=0.012,          # entire GP weekend
        n_way_market=True,                # race-winner / pole markets
        live_action_blackout=True,
        max_daily_trades=4,
        persona_context=_COMBAT_INDIVIDUAL_PERSONA_CONTEXT,
    ),
    "PGA": LeagueConfig(
        league="PGA",
        priority=7,
        allocation_pct=0.015,
        per_game_cap_pct=0.004,           # per single golfer outright
        per_event_cap_pct=0.012,          # tournament-wide cap
        n_way_market=True,                # tournament-winner markets
        live_action_blackout=True,
        max_daily_trades=6,
        persona_context=_COMBAT_INDIVIDUAL_PERSONA_CONTEXT,
    ),
    # Commodities binary-close (task #198).  Per-symbol 1h/4h/1d markets;
    # event-shaped concentration doesn't apply (each contract settles
    # independently on its own bar close).  The real concentration
    # limiter for this vertical is the 11-pair correlation sub-check on
    # `gate_per_vertical_cap`, so we explicitly disable per_event_cap
    # here (None) to avoid double-counting.  Live blackout disabled —
    # bot trades right up to settlement.
    "COMMODITIES": LeagueConfig(
        league="COMMODITIES",
        priority=8,
        allocation_pct=0.10,
        per_game_cap_pct=0.01,
        per_event_cap_pct=None,
        n_way_market=False,
        live_action_blackout=False,
        max_daily_trades=20,
    ),
    # Tech / AI binary-close (task #199).  Same shape as commodities:
    # each contract settles independently on its own resolution date,
    # so per_event concentration doesn't apply.  Allocation_pct mirrors
    # the conservative 0.10 vertical default (paper-grade until the
    # auto-promote pipeline bumps the Kelly multiplier).  Live blackout
    # disabled — there's no in-progress event to fade against.
    "TECH_AI": LeagueConfig(
        league="TECH_AI",
        priority=8,
        allocation_pct=0.10,
        per_game_cap_pct=0.01,
        per_event_cap_pct=None,
        n_way_market=False,
        live_action_blackout=False,
        max_daily_trades=20,
    ),
    # Elections (task #201).  Single bucket — `per_event_cap_pct` is
    # set so a single race can't accumulate too much exposure across
    # YES/NO + tie-breaker legs.  Live blackout disabled (election
    # markets stay open through tape).  Conservative max_daily_trades
    # to throttle churn during the 24-48h before resolution when
    # polls update most aggressively.
    "ELECTIONS": LeagueConfig(
        league="ELECTIONS",
        priority=9,
        allocation_pct=0.10,
        per_game_cap_pct=0.01,
        per_event_cap_pct=0.05,
        n_way_market=False,
        live_action_blackout=False,
        max_daily_trades=15,
    ),
    # Events / macro economics (task #202).  Each macro print resolves
    # independently — there's no "event in progress" frame, but the
    # gate enforces a 5-minute pre-release blackout via the events-econ
    # sub-check on `gate_market_cooldown`.  Conservative max_daily so
    # the bot doesn't churn around a single CPI day.
    "EVENTS_ECON": LeagueConfig(
        league="EVENTS_ECON",
        priority=9,
        allocation_pct=0.10,
        per_game_cap_pct=0.01,
        per_event_cap_pct=0.05,
        n_way_market=False,
        live_action_blackout=False,
        max_daily_trades=15,
    ),
    # Entertainment (task #203).  Each release / awards ceremony
    # resolves independently.  No "event in progress" frame — the
    # release-day blackout for box-office tickers is enforced via the
    # entertainment sub-check on `gate_market_cooldown`.
    "ENTERTAINMENT": LeagueConfig(
        league="ENTERTAINMENT",
        priority=10,
        allocation_pct=0.10,
        per_game_cap_pct=0.01,
        per_event_cap_pct=0.05,
        n_way_market=False,
        live_action_blackout=False,
        max_daily_trades=15,
    ),
    # Crypto-binary (task #754).  Each KXBTC/KXETH/etc price-target
    # contract resolves on its own clock (5m / 1h / etc cadence) so
    # there's no "event in progress" frame to gate on.  Conservative
    # per-coin/per-event cap and a low max-daily-trades because the
    # crypto stack still leans heavily on the V1-bootstrap calibrator
    # — once the 6-h scheduler refit accumulates a meaningful live
    # population we can revisit these numbers.
    "CRYPTO": LeagueConfig(
        league="CRYPTO",
        priority=11,
        allocation_pct=0.05,
        per_game_cap_pct=0.005,
        per_event_cap_pct=0.02,
        n_way_market=False,
        live_action_blackout=False,
        max_daily_trades=20,
    ),
    # Weather (task #767).  WEATHER joined `SUPPORTED_LEAGUES` so the
    # GRU+EMOS temperature stack and the precipitation predictor stop
    # silently bypassing the calibrator.  The
    # `test_league_configs_table_covers_every_supported_league`
    # invariant requires every supported league to carry a
    # `LeagueConfig`, so we add a conservative entry here:
    # weather markets resolve once-per-day on a fixed schedule and
    # carry no in-progress event frame, so per-event/live-blackout
    # mirror the commodities/tech-AI shape and `max_daily_trades`
    # caps churn at one trade per location/day in expectation.
    "WEATHER": LeagueConfig(
        league="WEATHER",
        priority=11,
        allocation_pct=0.05,
        per_game_cap_pct=0.005,
        per_event_cap_pct=0.02,
        n_way_market=False,
        live_action_blackout=False,
        max_daily_trades=20,
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


def persona_context_augment(
    league: str | None,
    game_context: dict | None = None,
) -> str:
    """Persona-prompt augment block for `league`.

    When ``game_context`` carries structured ``weather`` /
    ``player_status`` blocks (currently populated for NFL by
    :mod:`app.sports.nfl_context`), an "OBSERVED CONDITIONS" tail is
    appended so the personas reason on real numbers instead of just
    being told to think about them.

    Empty string when no augment is configured — keeps prompts clean
    for legacy leagues.
    """
    cfg = get_league_config(league)
    if cfg is None:
        return ""
    base = cfg.persona_context
    if not game_context:
        return base
    tail = _render_game_context(game_context)
    if not tail:
        return base
    if not base:
        return tail
    return base + "\n\n" + tail


def _render_game_context(game_context: dict) -> str:
    """Render the structured game_context dict as a prompt-friendly
    "OBSERVED CONDITIONS" block.  Only includes sub-blocks that are
    actually populated so the prompt stays signal-dense."""
    lines: list[str] = []

    weather = game_context.get("weather") if isinstance(game_context, dict) else None
    if isinstance(weather, dict):
        venue = weather.get("venue") or "(unknown venue)"
        venue_type = weather.get("venue_type")
        if venue_type == "indoor":
            lines.append(
                f"  * Weather ({venue}, indoor / dome): N/A — sealed venue, "
                f"weather is not a factor."
            )
        elif weather.get("forecast_unavailable"):
            lines.append(
                f"  * Weather ({venue}, outdoor): forecast feed unavailable; "
                f"do not assume neutral conditions."
            )
        else:
            parts: list[str] = []
            if weather.get("temperature_f") is not None:
                parts.append(f"{weather['temperature_f']:.0f}F")
            if weather.get("wind_mph") is not None:
                gust = weather.get("wind_gust_mph")
                if gust is not None:
                    parts.append(
                        f"wind {weather['wind_mph']:.0f} mph (gust {gust:.0f})"
                    )
                else:
                    parts.append(f"wind {weather['wind_mph']:.0f} mph")
            if weather.get("precip_in_per_hr") is not None:
                parts.append(f"precip {weather['precip_in_per_hr']:.2f} in/hr")
            if weather.get("humidity_pct") is not None:
                parts.append(f"humidity {weather['humidity_pct']:.0f}%")
            detail = ", ".join(parts) if parts else "no detailed readings"
            lines.append(f"  * Weather ({venue}, outdoor): {detail}.")

    player_status = (
        game_context.get("player_status") if isinstance(game_context, dict) else None
    )
    if isinstance(player_status, dict):
        for side_key, side_label in (("home", "Home"), ("away", "Away")):
            side = player_status.get(side_key)
            if not isinstance(side, dict):
                continue
            team = side.get("team") or "(unknown)"
            inj_summary = _summarise_injuries(side.get("injuries") or [])
            snap_summary = _summarise_snap_counts(side.get("snap_counts") or [])
            lines.append(
                f"  * {side_label} ({team}) injuries: {inj_summary}"
            )
            lines.append(
                f"  * {side_label} ({team}) skill snap %: {snap_summary}"
            )

    # Soccer lineup status — the structured cousin of the NFL
    # `player_status` block, populated by `app.sports.soccer_context`
    # from the cached sportradar_rosters + missing-players feeds.
    # Renders one line per side plus an explicit signed swing so the
    # lineup_rotation persona has a numeric anchor to nudge from.
    lineup_status = (
        game_context.get("lineup_status") if isinstance(game_context, dict) else None
    )
    if isinstance(lineup_status, dict):
        for side_key, side_label in (("home", "Home"), ("away", "Away")):
            side = lineup_status.get(side_key)
            if not isinstance(side, dict):
                continue
            team = side.get("team") or "(unknown)"
            missing_summary = _summarise_missing_players(
                side.get("missing_players") or []
            )
            severity_pp = side.get("lineup_severity_pp")
            roster_n = side.get("roster_size")
            sev_pct = (
                f"{float(severity_pp) * 100:.1f}pp"
                if isinstance(severity_pp, (int, float))
                else "n/a"
            )
            depth_chunk = (
                f", squad depth {int(roster_n)}"
                if isinstance(roster_n, int) and roster_n > 0
                else ""
            )
            lines.append(
                f"  * {side_label} ({team}) lineup severity {sev_pct}"
                f"{depth_chunk}: {missing_summary}"
            )
        net_swing = lineup_status.get("net_home_swing_pp")
        if isinstance(net_swing, (int, float)):
            direction = "toward HOME" if net_swing > 0 else (
                "toward AWAY" if net_swing < 0 else "neutral"
            )
            lines.append(
                f"  * Net lineup swing: {float(net_swing) * 100:+.1f}pp "
                f"({direction}) — anchor your win-share nudge on this delta."
            )

    if not lines:
        return ""
    return "OBSERVED CONDITIONS for this matchup:\n" + "\n".join(lines)


def _summarise_injuries(injuries: list) -> str:
    if not injuries:
        return "no notable OL/DL/skill scratches in feed cache."
    chunks: list[str] = []
    for inj in injuries[:8]:
        if not isinstance(inj, dict):
            continue
        status = (inj.get("status") or "UNKNOWN").upper()
        pos = (inj.get("position") or "??").upper()
        player = inj.get("player") or "(unknown)"
        chunks.append(f"{status} {pos} {player}")
    return "; ".join(chunks) if chunks else "no notable OL/DL/skill scratches in feed cache."


def _summarise_snap_counts(snap_counts: list) -> str:
    if not snap_counts:
        return "no snap-count data cached for the most recent game."
    chunks: list[str] = []
    for row in snap_counts[:6]:
        if not isinstance(row, dict):
            continue
        player = row.get("player") or "(unknown)"
        pos = (row.get("position") or "??").upper()
        pct = row.get("snap_pct")
        if pct is None:
            chunks.append(f"{pos} {player}")
        else:
            chunks.append(f"{pos} {player} {float(pct):.0f}%")
    return ", ".join(chunks) if chunks else (
        "no snap-count data cached for the most recent game."
    )


def _summarise_missing_players(missing: list) -> str:
    """Render the soccer missing-players list as a single prompt line."""
    if not missing:
        return "no missing players reported in feed cache."
    chunks: list[str] = []
    for row in missing[:8]:
        if not isinstance(row, dict):
            continue
        status = (row.get("status") or "missing").upper()
        pos = (row.get("position") or "??").upper()
        player = row.get("player") or "(unknown)"
        chunks.append(f"{status} {pos} {player}")
    return "; ".join(chunks) if chunks else (
        "no missing players reported in feed cache."
    )
