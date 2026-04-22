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
    """`watchdog_market_cooldowns` — exchange or risk has muted this market."""
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
