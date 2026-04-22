"""The 12 risk gates that sit between swarm verdict and order submission.

Gate ordering matters — cheaper / kill-switch gates first so we don't
waste a DB round-trip on a candidate that's about to be rejected
anyway.  Every gate returns a `GateResult` with a stable `name` so the
dashboard can render the same column across cycles.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from .. import db, verticals as vertical_registry
from ..config import get_settings
from ..runtime_settings import profile_for as _aggression_profile
from ..sports import (
    get_league_config,
    is_sunday_window,
    league_blackout_until,
)


log = logging.getLogger(__name__)


def _mode_default(ctx: "RiskContext", key: str, fallback: float) -> float:
    """Default value for a tunable gate threshold given the active
    aggression mode (read off ``operator_overrides['aggression_mode']``).

    An explicit ``operator_overrides[key]`` always wins — operators can
    still pin a single knob without leaving the active mode.
    """
    mode = ctx.operator_overrides.get("aggression_mode")
    if not mode:
        return fallback
    profile = _aggression_profile(mode)
    return float(profile.get(key, fallback))


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_event(self) -> dict[str, Any]:
        return {
            "gate": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "details": self.details,
        }


@dataclass
class RiskContext:
    """Everything the gates need to evaluate a single candidate.

    Built once per candidate by the orchestrator before invoking
    `run_all_gates`.  Mutating fields after construction is undefined.
    """

    # Candidate identity + sizing
    market_id: str
    side: str  # 'YES' | 'NO'
    vertical: str  # 'sports' | 'crypto' | ...
    league: str | None
    market_type: str | None
    matchup: str | None
    # Pricing + alpha
    current_price: float | None  # YES-side mid, 0..1
    model_probability: float | None  # swarm consensus
    edge: float | None  # model_probability - current_price (signed)
    confidence_band_low: float | None
    confidence_band_high: float | None
    agreement_rate: float | None
    # Swarm gate inputs
    swarm_approved: bool
    swarm_recommended_action: str | None  # 'BUY_YES' | 'BUY_NO' | 'PASS'
    debate_id: int | None
    # Sizing intent
    intended_quantity: int  # contracts
    intended_notional_usd: float  # = qty * price
    # Bankroll snapshot at cycle start
    bankroll_usd: float | None
    # Open-position aggregates (cycle-wide cache so each gate doesn't re-query)
    open_positions_count: int
    open_positions_notional_usd: float
    open_positions_per_vertical_usd: dict[str, float]
    # Ops controls
    operator_overrides: dict[str, Any]
    market_cooldown_until: datetime | None
    # Per-league sub-aggregates (NFL Sunday-window concentration, per-game
    # cap, etc).  Defaulted so legacy call sites that don't populate them
    # behave identically to the previous behavior — the per-league /
    # per-game / Sunday-window sub-checks short-circuit when their input
    # data is empty.
    open_positions_per_league_usd: dict[str, float] = field(default_factory=dict)
    open_positions_per_matchup_usd: dict[str, float] = field(default_factory=dict)
    open_positions_sunday_window_usd: dict[str, float] = field(default_factory=dict)
    # Per-league trade count for the current UTC day.  Populated by the
    # cycle aggregator; defaulted empty so legacy call sites + leagues
    # without `LeagueConfig.max_daily_trades` are no-ops.  Soccer leagues
    # ship this cap to throttle matchday churn (task #196).
    trades_today_per_league: dict[str, int] = field(default_factory=dict)
    # Game start time (UTC) for the candidate's market.  Used by
    # gate_market_cooldown to honor per-league pre-kickoff blackouts
    # (NFL inactive-list window).  None when the market isn't game-bound.
    game_time: datetime | None = None
    # Event identifier shared across every market that belongs to the
    # same UFC fight night / F1 GP / tennis tournament / PGA tournament
    # (task #197).  Per-event concentration sub-check aggregates open
    # exposure on this key so the bot can't accidentally pile the
    # whole bankroll into one weekend's slate.  None for markets that
    # aren't event-bound (legacy team-sport candidates carry None and
    # the sub-check no-ops).
    event_id: str | None = None
    # Per-event open exposure aggregate (USD) — populated by the cycle
    # aggregator alongside `open_positions_per_matchup_usd`.  Defaults
    # empty so legacy call sites short-circuit the per-event sub-check.
    open_positions_per_event_usd: dict[str, float] = field(default_factory=dict)
    # Whether the candidate's underlying event is currently in-progress
    # (UFC fight underway, F1 race lights-out, PGA tournament Sunday
    # back-9, tennis match in-play).  When True and the league has
    # `live_action_blackout=True`, gate_market_cooldown rejects the
    # candidate as a pre-event entry against an active event.  Defaults
    # False so legacy candidates are unaffected.
    event_in_progress: bool = False


# ---------------------------------------------------------------------------
# Gate implementations.  All async to allow gates that touch the DB.
# ---------------------------------------------------------------------------


async def gate_kill_switch(ctx: RiskContext) -> GateResult:
    """Global trading_mode != 'halt'.  AXIOM_TRADING_MODE env or
    operator_overrides['trading_mode'] both honored — operator override
    wins so a runtime kill-switch doesn't require a redeploy."""
    settings = get_settings()
    mode = (
        ctx.operator_overrides.get("trading_mode")
        or settings.trading_mode
        or "paper"
    ).lower()
    if mode == "halt":
        return GateResult(
            "kill_switch",
            False,
            "global trading_mode=halt",
            {"source": "operator_overrides" if "trading_mode" in ctx.operator_overrides else "env"},
        )
    return GateResult("kill_switch", True, "", {"trading_mode": mode})


async def gate_market_cooldown(ctx: RiskContext) -> GateResult:
    """`watchdog_market_cooldowns` — exchange or risk has muted this market.

    Also enforces per-league pre-kickoff blackouts (e.g. NFL inactive-list
    window, 90 minutes before kickoff) when `ctx.game_time` is populated.
    """
    if ctx.market_cooldown_until is not None:
        now = datetime.now(timezone.utc)
        cd = ctx.market_cooldown_until
        if cd.tzinfo is None:
            cd = cd.replace(tzinfo=timezone.utc)
        if cd > now:
            return GateResult(
                "market_cooldown",
                False,
                "market on cooldown",
                {"cooldown_until": cd.isoformat()},
            )
    blackout = league_blackout_until(ctx.league, ctx.game_time)
    if blackout is not None:
        return GateResult(
            "market_cooldown",
            False,
            f"{ctx.league} pre-kickoff blackout",
            {
                "cooldown_until": blackout.isoformat(),
                "reason": "pre_kickoff_blackout",
                "league": ctx.league,
            },
        )
    # Live-action blackout (task #197): combat & individual sports
    # opt-in via `LeagueConfig.live_action_blackout`.  Once the
    # underlying event is in-progress, pre-event outright markets are
    # not eligible for new entries.
    lcfg = get_league_config(ctx.league)
    if (
        lcfg is not None
        and lcfg.live_action_blackout
        and ctx.event_in_progress
    ):
        return GateResult(
            "market_cooldown",
            False,
            f"{ctx.league} live-action blackout (event in progress)",
            {
                "reason": "live_action_blackout",
                "league": ctx.league,
                "event_id": ctx.event_id,
            },
        )
    return GateResult("market_cooldown", True)


async def gate_swarm_approval(ctx: RiskContext) -> GateResult:
    """Swarm gatekeeper must have approved.  Reuse its veto reason verbatim
    when present so a single 'pipeline_event' carries the full provenance."""
    if not ctx.swarm_approved:
        return GateResult(
            "swarm_approval",
            False,
            "swarm gatekeeper vetoed",
            {
                "debate_id": ctx.debate_id,
                "swarm_recommended_action": ctx.swarm_recommended_action,
            },
        )
    if ctx.swarm_recommended_action == "PASS":
        return GateResult(
            "swarm_approval",
            False,
            "swarm recommended PASS",
            {"debate_id": ctx.debate_id},
        )
    return GateResult("swarm_approval", True, "", {"debate_id": ctx.debate_id})


async def gate_min_edge(ctx: RiskContext) -> GateResult:
    """edge >= operator override / default."""
    default_pp = _mode_default(ctx, "min_edge_pp", 3.0)
    min_edge = float(ctx.operator_overrides.get("min_edge_pp", default_pp)) / 100.0
    edge = ctx.edge if ctx.edge is not None else 0.0
    if edge < min_edge:
        return GateResult(
            "min_edge",
            False,
            f"edge {edge:.4f} < min {min_edge:.4f}",
            {"edge": edge, "min_edge": min_edge},
        )
    return GateResult("min_edge", True, "", {"edge": edge, "min_edge": min_edge})


async def gate_drift(ctx: RiskContext) -> GateResult:
    """Confidence band must not be wider than allowed drift cap.
    Wide bands signal the personas disagree dramatically on the
    underlying probability and the edge is unreliable."""
    cap = float(
        ctx.operator_overrides.get(
            "max_confidence_band",
            _mode_default(ctx, "max_confidence_band", 0.30),
        )
    )
    if ctx.confidence_band_low is None or ctx.confidence_band_high is None:
        return GateResult("drift", True, "no band reported", {"cap": cap})
    width = float(ctx.confidence_band_high - ctx.confidence_band_low)
    if width > cap:
        return GateResult(
            "drift",
            False,
            f"confidence band width {width:.3f} > cap {cap:.3f}",
            {"width": width, "cap": cap},
        )
    return GateResult("drift", True, "", {"width": width, "cap": cap})


async def gate_agreement(ctx: RiskContext) -> GateResult:
    """Persona agreement rate >= floor."""
    floor = float(
        ctx.operator_overrides.get(
            "min_agreement_rate",
            _mode_default(ctx, "min_agreement_rate", 0.50),
        )
    )
    if ctx.agreement_rate is None:
        return GateResult("agreement", True, "no agreement reported", {"floor": floor})
    if ctx.agreement_rate < floor:
        return GateResult(
            "agreement",
            False,
            f"agreement {ctx.agreement_rate:.2f} < floor {floor:.2f}",
            {"agreement": ctx.agreement_rate, "floor": floor},
        )
    return GateResult(
        "agreement", True, "", {"agreement": ctx.agreement_rate, "floor": floor}
    )


async def gate_kelly_fraction(ctx: RiskContext) -> GateResult:
    """Quarter-Kelly position size cap.  Computes the optimal Kelly
    fraction for the candidate and rejects if intended_quantity exceeds
    the operator-configured fraction of it (default 0.25)."""
    p = ctx.model_probability
    price = ctx.current_price
    if p is None or price is None or price <= 0 or price >= 1:
        return GateResult(
            "kelly_fraction", True, "missing probability/price", {}
        )
    # Kelly for a 0/1 binary contract priced at `price` with true prob p.
    # b = (1 - price) / price  (payoff multiple on win for YES side)
    if ctx.side.upper() == "NO":
        p = 1.0 - p
        price = 1.0 - price
    if price <= 0 or price >= 1:
        return GateResult("kelly_fraction", True, "degenerate price after side flip", {})
    b = (1.0 - price) / price
    q = 1.0 - p
    kelly = (b * p - q) / b
    if kelly <= 0:
        return GateResult(
            "kelly_fraction",
            False,
            f"kelly {kelly:.4f} <= 0",
            {"kelly": kelly},
        )
    fraction_cap = float(
        ctx.operator_overrides.get(
            "kelly_fraction_cap",
            _mode_default(ctx, "kelly_fraction_cap", 0.25),
        )
    )
    # Per-vertical Kelly multiplier from the registry (operator override
    # via "kelly_multiplier" still wins).  Defaults to 1.0 so existing
    # verticals behave identically until a spec opts into a different
    # multiplier.
    from .. import verticals as _verticals
    spec = _verticals.get_spec(ctx.vertical) if ctx.vertical else None
    spec_multiplier = spec.kelly_multiplier if spec is not None else 1.0
    kelly_multiplier = float(
        ctx.operator_overrides.get("kelly_multiplier", spec_multiplier)
    )
    bankroll = ctx.bankroll_usd or 0.0
    max_notional = bankroll * kelly * fraction_cap * kelly_multiplier
    if ctx.intended_notional_usd > max_notional and bankroll > 0:
        return GateResult(
            "kelly_fraction",
            False,
            (
                f"intended ${ctx.intended_notional_usd:.2f} > "
                f"{fraction_cap:.2f}-Kelly cap ${max_notional:.2f}"
            ),
            {
                "kelly": kelly,
                "fraction_cap": fraction_cap,
                "kelly_multiplier": kelly_multiplier,
                "max_notional": max_notional,
                "intended_notional": ctx.intended_notional_usd,
            },
        )
    return GateResult(
        "kelly_fraction",
        True,
        "",
        {
            "kelly": kelly,
            "fraction_cap": fraction_cap,
            "kelly_multiplier": kelly_multiplier,
            "max_notional": max_notional,
            "intended_notional": ctx.intended_notional_usd,
        },
    )


async def gate_bankroll_floor(ctx: RiskContext) -> GateResult:
    """Refuse to trade when bankroll has been drawn down below floor."""
    floor = float(ctx.operator_overrides.get("bankroll_floor_usd", 100.0))
    if ctx.bankroll_usd is None:
        return GateResult("bankroll_floor", True, "no bankroll snapshot", {"floor": floor})
    if ctx.bankroll_usd < floor:
        return GateResult(
            "bankroll_floor",
            False,
            f"bankroll ${ctx.bankroll_usd:.2f} < floor ${floor:.2f}",
            {"bankroll": ctx.bankroll_usd, "floor": floor},
        )
    return GateResult(
        "bankroll_floor", True, "", {"bankroll": ctx.bankroll_usd, "floor": floor}
    )


async def gate_global_exposure_cap(ctx: RiskContext) -> GateResult:
    """Total open notional + intended <= cap_pct of bankroll."""
    cap_pct = float(ctx.operator_overrides.get("global_exposure_cap_pct", 0.50))
    bankroll = ctx.bankroll_usd or 0.0
    if bankroll <= 0:
        return GateResult("global_exposure_cap", True, "no bankroll", {"cap_pct": cap_pct})
    cap_usd = bankroll * cap_pct
    projected = ctx.open_positions_notional_usd + ctx.intended_notional_usd
    if projected > cap_usd:
        return GateResult(
            "global_exposure_cap",
            False,
            f"projected exposure ${projected:.2f} > cap ${cap_usd:.2f}",
            {"projected": projected, "cap_usd": cap_usd, "cap_pct": cap_pct},
        )
    return GateResult(
        "global_exposure_cap",
        True,
        "",
        {"projected": projected, "cap_usd": cap_usd, "cap_pct": cap_pct},
    )


async def gate_per_vertical_cap(ctx: RiskContext) -> GateResult:
    """Per-vertical exposure cap (sports/crypto/etc).

    Default is taken from the registry's `VerticalSpec.default_allocation_pct`
    (so a new vertical can ship its own cap by registering a spec) and
    operator overrides via `risk.per_vertical_cap_pct` still win.

    Also enforces three sports-vertical sub-caps when configured for the
    candidate's league (see `app.sports.leagues.LeagueConfig`):

    * **per-league cap** — share of bankroll allocated to a single league
      (NFL ships at 5% by default; football game volume concentrates on a
      single Sunday so the per-vertical sports cap is not enough on its
      own);
    * **per-game cap** — share of bankroll per matchup (limits exposure
      to a single correlated outcome — e.g. weather, ref crew, late
      injury);
    * **Sunday-window concentration cap** — share of bankroll across all
      games kicking off in the league's Sunday slate window (NFL only by
      default).

    Each sub-cap is skipped when the corresponding `LeagueConfig` field
    is `None`, so non-NFL leagues retain prior behavior.
    """
    spec = vertical_registry.get_spec(ctx.vertical or "")
    spec_default = spec.default_allocation_pct if spec is not None else 0.30
    cap_pct = float(ctx.operator_overrides.get("per_vertical_cap_pct", spec_default))
    bankroll = ctx.bankroll_usd or 0.0
    if bankroll <= 0:
        return GateResult("per_vertical_cap", True, "no bankroll", {"cap_pct": cap_pct})
    cap_usd = bankroll * cap_pct
    vertical_open = ctx.open_positions_per_vertical_usd.get(ctx.vertical, 0.0)
    projected = vertical_open + ctx.intended_notional_usd
    if projected > cap_usd:
        return GateResult(
            "per_vertical_cap",
            False,
            f"{ctx.vertical} projected ${projected:.2f} > cap ${cap_usd:.2f}",
            {
                "vertical": ctx.vertical,
                "projected": projected,
                "cap_usd": cap_usd,
                "cap_pct": cap_pct,
            },
        )

    # League-scoped sub-checks (NFL is the canonical user; other leagues
    # opt in by populating the corresponding LeagueConfig fields).
    lcfg = get_league_config(ctx.league)
    if lcfg is not None:
        league_key = lcfg.league

        # Per-league daily trade-count cap (task #196 — soccer matchday
        # churn throttle).  Opt-in via `LeagueConfig.max_daily_trades`;
        # operator override key mirrors the per-league cap convention.
        max_daily = ctx.operator_overrides.get(
            f"max_daily_trades.{league_key.lower()}",
            lcfg.max_daily_trades,
        )
        if max_daily is not None:
            max_daily_int = int(max_daily)
            today_count = int(
                ctx.trades_today_per_league.get(league_key, 0)
            )
            if today_count >= max_daily_int:
                return GateResult(
                    "per_vertical_cap",
                    False,
                    (
                        f"{league_key} daily trade count "
                        f"{today_count} >= cap {max_daily_int}"
                    ),
                    {
                        "vertical": ctx.vertical,
                        "league": league_key,
                        "sub_check": "max_daily_trades",
                        "trades_today": today_count,
                        "cap": max_daily_int,
                    },
                )

        # Per-league cap.
        league_cap_pct = float(
            ctx.operator_overrides.get(
                f"per_league_cap_pct.{league_key.lower()}",
                lcfg.allocation_pct,
            )
        )
        league_cap_usd = bankroll * league_cap_pct
        league_open = ctx.open_positions_per_league_usd.get(league_key, 0.0)
        league_projected = league_open + ctx.intended_notional_usd
        if league_projected > league_cap_usd:
            return GateResult(
                "per_vertical_cap",
                False,
                (
                    f"{league_key} projected ${league_projected:.2f} > "
                    f"per-league cap ${league_cap_usd:.2f}"
                ),
                {
                    "vertical": ctx.vertical,
                    "league": league_key,
                    "sub_check": "per_league_cap",
                    "projected": league_projected,
                    "cap_usd": league_cap_usd,
                    "cap_pct": league_cap_pct,
                },
            )

        # Per-game (matchup) cap.
        if lcfg.per_game_cap_pct is not None and ctx.matchup:
            game_cap_pct = float(
                ctx.operator_overrides.get(
                    f"per_game_cap_pct.{league_key.lower()}",
                    lcfg.per_game_cap_pct,
                )
            )
            game_cap_usd = bankroll * game_cap_pct
            matchup_key = f"{league_key}:{ctx.matchup}"
            game_open = ctx.open_positions_per_matchup_usd.get(matchup_key, 0.0)
            game_projected = game_open + ctx.intended_notional_usd
            if game_projected > game_cap_usd:
                return GateResult(
                    "per_vertical_cap",
                    False,
                    (
                        f"{matchup_key} projected ${game_projected:.2f} > "
                        f"per-game cap ${game_cap_usd:.2f}"
                    ),
                    {
                        "vertical": ctx.vertical,
                        "league": league_key,
                        "matchup": ctx.matchup,
                        "sub_check": "per_game_cap",
                        "projected": game_projected,
                        "cap_usd": game_cap_usd,
                        "cap_pct": game_cap_pct,
                    },
                )

        # Per-event concentration cap (task #197).  Combat & individual
        # sports opt in via `LeagueConfig.per_event_cap_pct`: a single
        # UFC fight night / F1 GP / tennis tournament / PGA tournament
        # can spawn 8-30 outright legs; without an explicit per-event
        # cap the per-league cap can bunch all of them onto one weekend.
        if (
            lcfg.per_event_cap_pct is not None
            and ctx.event_id
        ):
            event_cap_pct = float(
                ctx.operator_overrides.get(
                    f"per_event_cap_pct.{league_key.lower()}",
                    lcfg.per_event_cap_pct,
                )
            )
            event_cap_usd = bankroll * event_cap_pct
            event_key = f"{league_key}:{ctx.event_id}"
            event_open = ctx.open_positions_per_event_usd.get(event_key, 0.0)
            event_projected = event_open + ctx.intended_notional_usd
            if event_projected > event_cap_usd:
                return GateResult(
                    "per_vertical_cap",
                    False,
                    (
                        f"{event_key} projected ${event_projected:.2f} > "
                        f"per-event cap ${event_cap_usd:.2f}"
                    ),
                    {
                        "vertical": ctx.vertical,
                        "league": league_key,
                        "event_id": ctx.event_id,
                        "sub_check": "per_event_cap",
                        "projected": event_projected,
                        "cap_usd": event_cap_usd,
                        "cap_pct": event_cap_pct,
                    },
                )

        # Sunday-window concentration cap (NFL slate, etc).
        # Evaluate against the candidate's game kickoff when available so the
        # cap correctly applies to the slate the new bet would land in,
        # falling back to wall-clock now() when game_time isn't carried.
        sunday_check_time = ctx.game_time or datetime.now(timezone.utc)
        if (
            lcfg.sunday_window_cap_pct is not None
            and is_sunday_window(league_key, sunday_check_time)
        ):
            sun_cap_pct = float(
                ctx.operator_overrides.get(
                    f"sunday_window_cap_pct.{league_key.lower()}",
                    lcfg.sunday_window_cap_pct,
                )
            )
            sun_cap_usd = bankroll * sun_cap_pct
            sun_open = ctx.open_positions_sunday_window_usd.get(league_key, 0.0)
            sun_projected = sun_open + ctx.intended_notional_usd
            if sun_projected > sun_cap_usd:
                return GateResult(
                    "per_vertical_cap",
                    False,
                    (
                        f"{league_key} Sunday window projected "
                        f"${sun_projected:.2f} > cap ${sun_cap_usd:.2f}"
                    ),
                    {
                        "vertical": ctx.vertical,
                        "league": league_key,
                        "sub_check": "sunday_window_cap",
                        "projected": sun_projected,
                        "cap_usd": sun_cap_usd,
                        "cap_pct": sun_cap_pct,
                    },
                )

    return GateResult(
        "per_vertical_cap",
        True,
        "",
        {
            "vertical": ctx.vertical,
            "projected": projected,
            "cap_usd": cap_usd,
            "cap_pct": cap_pct,
        },
    )


async def gate_max_concurrent_positions(ctx: RiskContext) -> GateResult:
    cap = int(ctx.operator_overrides.get("max_concurrent_positions", 25))
    if ctx.open_positions_count >= cap:
        return GateResult(
            "max_concurrent_positions",
            False,
            f"open positions {ctx.open_positions_count} >= cap {cap}",
            {"open": ctx.open_positions_count, "cap": cap},
        )
    return GateResult(
        "max_concurrent_positions",
        True,
        "",
        {"open": ctx.open_positions_count, "cap": cap},
    )


async def gate_duplicate_trade(ctx: RiskContext) -> GateResult:
    """Production has `idx_trades_unique_daily` — match it exactly so we
    don't even attempt an insert that would conflict.  Verticals can
    opt out via `VerticalSpec.duplicate_trade_exempt` (currently the
    spot-crypto vertical, where the production index does not apply)."""
    spec = vertical_registry.get_spec(ctx.vertical or "")
    if spec is not None and spec.duplicate_trade_exempt:
        return GateResult(
            "duplicate_trade",
            True,
            f"{spec.name} vertical exempt",
            {"vertical": spec.name},
        )
    # Use UTC date everywhere — host-local date would create
    # day-boundary races at the seam between UTC and local TZ.  The
    # production unique index `idx_trades_unique_daily` is built on
    # `(market_id, lower(side), (executed_at at time zone 'UTC')::date)`
    # so we mirror it exactly.
    today_utc = datetime.now(timezone.utc).date()
    try:
        n = await db.fetchval(
            """
            SELECT count(*) FROM trades
             WHERE market_id = $1
               AND lower(side) = lower($2)
               AND ((executed_at at time zone 'UTC')::date) = $3
            """,
            ctx.market_id,
            ctx.side,
            today_utc,
        )
    except Exception as e:
        log.warning("duplicate_trade gate query failed: %s", e)
        return GateResult(
            "duplicate_trade", True, "query degraded", {"error": str(e)}
        )
    if int(n or 0) > 0:
        return GateResult(
            "duplicate_trade",
            False,
            "trade already exists for (market, side, today)",
            {"existing": int(n), "market_id": ctx.market_id, "side": ctx.side},
        )
    return GateResult("duplicate_trade", True, "", {"market_id": ctx.market_id})


# Ordered registry — kill-switch and cheap checks first, DB queries last.
GateFn = Callable[[RiskContext], Awaitable[GateResult]]
GATES: tuple[tuple[str, GateFn], ...] = (
    ("kill_switch", gate_kill_switch),
    ("market_cooldown", gate_market_cooldown),
    ("swarm_approval", gate_swarm_approval),
    ("min_edge", gate_min_edge),
    ("drift", gate_drift),
    ("agreement", gate_agreement),
    ("kelly_fraction", gate_kelly_fraction),
    ("bankroll_floor", gate_bankroll_floor),
    ("global_exposure_cap", gate_global_exposure_cap),
    ("per_vertical_cap", gate_per_vertical_cap),
    ("max_concurrent_positions", gate_max_concurrent_positions),
    ("duplicate_trade", gate_duplicate_trade),
)
assert len(GATES) == 12, "exactly 12 gates required by spec"


async def run_all_gates(
    ctx: RiskContext,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[bool, list[GateResult]]:
    """Run gates in order, short-circuiting on first FAIL.  Each gate's
    result is forwarded via `on_event` (if provided) before evaluating
    the next gate so the WS subscriber sees them stream live.

    Returns (all_passed, results_so_far).  `results_so_far` ends at the
    first failure when short-circuited; otherwise contains all 12.
    """
    results: list[GateResult] = []
    for name, fn in GATES:
        try:
            r = await fn(ctx)
        except Exception as e:
            log.exception("gate %s raised", name)
            r = GateResult(name, False, f"gate raised: {e}", {"error": str(e)})
        results.append(r)
        if on_event is not None:
            try:
                await on_event(r.to_event())
            except Exception as e:
                log.warning("pipeline event publish failed: %s", e)
        if not r.passed:
            return False, results
    return True, results
