"""The 12 risk gates that sit between swarm verdict and order submission.

Gate ordering matters — cheaper / kill-switch gates first so we don't
waste a DB round-trip on a candidate that's about to be rejected
anyway.  Every gate returns a `GateResult` with a stable `name` so the
dashboard can render the same column across cycles.
"""
from __future__ import annotations

import logging
import time
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


# Throttle window for the "no daily-loss thresholds configured" reminder.
# The gate is evaluated per-candidate (potentially many per orchestrator
# cycle), so a stock `log.warning` would emit dozens of identical lines per
# cycle when neither AXIOM_DAILY_LOSS_HALT_USD nor _PCT is set. Operators
# only need the reminder once per cycle — 60s is comfortably longer than a
# normal cycle and short enough that a fresh deploy still prints the
# safety-net hint promptly.
_NO_THRESHOLDS_WARNING_THROTTLE_SECONDS = 60.0
# `-inf` (rather than 0.0) guarantees the first warning fires immediately,
# even on processes whose monotonic clock starts below 60s.
_last_no_thresholds_warning_at: float = float("-inf")


def _reset_no_thresholds_warning_throttle() -> None:
    """Test hook: clear the throttle so the next call re-emits the warning."""
    global _last_no_thresholds_warning_at
    _last_no_thresholds_warning_at = float("-inf")


# ---------------------------------------------------------------------------
# Tail-risk gate trip alert helper (Task #750).
#
# Both `gate_tail_price_size_cap` and `gate_jumbo_quantity_cap` call
# this on a veto so operators get an info-severity broadcast about the
# trip.  Dedupes on `(gate_name, market_id)` so a market that keeps
# tripping the gate every cycle doesn't spam mailboxes.
#
# Wrapped in a top-level try/except so an alert-system failure (DB
# down, postmark unavailable) NEVER shadows the actual gate verdict —
# the gate decision must always reach the orchestrator.
# ---------------------------------------------------------------------------


# Dedupe TTL — long enough that a market re-evaluated every cycle
# (typically a few seconds) doesn't generate a fresh email each time,
# short enough that a fresh trip for the same market the next trading
# day still surfaces.
_TAIL_RISK_TRIP_DEDUPE_TTL_SECONDS = 6 * 3600


async def _persist_risk_gate_trip(
    *,
    ctx: "RiskContext",
    gate_name: str,
    reason: str,
    details: dict[str, Any],
) -> None:
    """Best-effort write of one row into ``risk_gate_trips``.

    Backs the daily honesty report's ``gate_trips_24h`` panel
    (Task #756).  We persist *every* trip even when the email/Telegram
    fan-out is deduped, because the operator's daily report needs the
    real "this gate fired N times today" count, not the (smaller)
    deduped notification count.

    DB failures are swallowed — the gate decision and the alert
    fan-out below must always reach their respective consumers.
    """
    try:
        import json as _json

        await db.execute(
            """
            INSERT INTO risk_gate_trips
                (gate_name, market_id, vertical, side,
                 intended_quantity, current_price, reason, details)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            gate_name,
            ctx.market_id,
            ctx.vertical,
            ctx.side,
            int(ctx.intended_quantity)
            if ctx.intended_quantity is not None
            else None,
            float(ctx.current_price)
            if ctx.current_price is not None
            else None,
            reason,
            _json.dumps(details or {}),
        )
    except Exception as e:  # pragma: no cover — log persistence is best-effort
        log.warning(
            "risk_gate_trips persist failed for gate=%s market=%s: %s",
            gate_name,
            getattr(ctx, "market_id", None),
            e,
        )


async def _emit_tail_risk_trip_alert(
    *,
    ctx: "RiskContext",
    gate_name: str,
    reason: str,
    details: dict[str, Any],
) -> None:
    """Best-effort fire of the tail-risk trip alert. Never raises.

    Note: ``risk_gate_trips`` persistence is handled centrally inside
    :func:`run_all_gates` for *every* failed gate, so this helper
    deliberately does not write a row of its own — that would
    double-count the trip in the daily honesty report's
    ``gate_trips_24h`` panel.
    """
    try:
        from ..notifications import alerts as _alerts

        market_id = ctx.market_id or "(unknown)"
        dedupe_key = f"{gate_name}:{market_id}"
        try:
            should_send = await _alerts.dedupe_or_skip(
                "tail_risk_gate_trip",
                dedupe_key,
                ttl_seconds=_TAIL_RISK_TRIP_DEDUPE_TTL_SECONDS,
            )
        except Exception as e:
            log.warning(
                "tail_risk_gate_trip dedupe claim failed for %s: %s",
                dedupe_key,
                e,
            )
            should_send = True
        if not should_send:
            return
        _alerts.tail_risk_gate_trip(
            gate_name=gate_name,
            market_id=market_id,
            vertical=ctx.vertical,
            side=ctx.side,
            intended_quantity=int(ctx.intended_quantity)
            if ctx.intended_quantity is not None
            else None,
            current_price=float(ctx.current_price)
            if ctx.current_price is not None
            else None,
            reason=reason,
            details=details,
        )
    except Exception as e:  # pragma: no cover — alert failures must never break a gate
        log.warning(
            "tail_risk_gate_trip alert dispatch failed for gate=%s market=%s: %s",
            gate_name,
            getattr(ctx, "market_id", None),
            e,
        )


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


async def gate_vertical_halted(ctx: RiskContext) -> GateResult:
    """Reject candidates from an operator-halted vertical (Task #271).

    Per-vertical halts live in ``system_settings`` under the key
    ``risk.vertical_halted.<name>`` so the orchestrator's per-cycle
    ``_load_operator_overrides`` pre-fetches them; the gate only looks
    at ``ctx.operator_overrides`` — no extra DB round-trip.

    A truthy value means halted (the value itself is a JSON blob with
    who halted it + when, not used here — it's surfaced on the operator
    console instead).
    """
    vertical = (ctx.vertical or "").strip().lower()
    if not vertical:
        return GateResult("vertical_halted", True, "no vertical on candidate", {})
    flag = ctx.operator_overrides.get(f"vertical_halted.{vertical}")
    if flag:
        details: dict[str, Any] = {"vertical": vertical}
        # Best-effort decode of the attribution blob for the pipeline event.
        if isinstance(flag, str):
            try:
                import json as _json
                meta = _json.loads(flag)
                if isinstance(meta, dict):
                    for k in ("by_email", "by_user_id", "at", "reason"):
                        if k in meta:
                            details[k] = meta[k]
            except Exception:
                pass
        return GateResult(
            "vertical_halted",
            False,
            f"vertical '{vertical}' is halted",
            details,
        )
    return GateResult("vertical_halted", True, "", {"vertical": vertical})


def _raw_mode_override_key(vertical: str) -> str:
    """Operator-override key that opts a vertical out of the
    `gate_uncalibrated_vertical` block.  Naming matches the rest of
    the per-vertical knob convention (`risk.<knob>_<vertical>`)."""
    return f"uncalibrated_raw_mode_{(vertical or '').strip().lower()}"


async def gate_uncalibrated_vertical(ctx: RiskContext) -> GateResult:
    """Refuse to ship a live trade whose ``(vertical, league)`` does
    not have a fitted calibrator snapshot (Task #767).

    Until task #767 the ``apply()`` call in every predictor would
    silently bypass with ``info.reason="unsupported league"`` /
    ``"no fitted calibrator"`` and return the raw stack output, which
    then fed Kelly sizing — a textbook EV leak.  This gate closes the
    loop by rejecting any candidate whose ``ctx.league`` is missing
    from ``calibrators.SUPPORTED_LEAGUES`` *or* whose champion
    snapshot has not yet been fit.

    Operators can opt a single vertical into "ship raw" via the
    runtime override ``risk.uncalibrated_raw_mode_<vertical>``.  When
    opted in, the gate **passes** but persists a
    ``risk_gate_trips`` row with ``gate_name='uncalibrated_vertical_raw_mode'``
    so the daily honesty report's "Uncalibrated live trades (24h)" row
    can surface the count in red.

    Fail-closed: missing league, missing vertical, calibrators import
    failure, or state-lookup error all REJECT (unless the operator has
    set the per-vertical raw-mode override).  This is deliberate — the
    whole point of the gate is to prevent silent uncalibrated EV leaks,
    so any "we don't know" must be treated as "no calibrator".
    """
    league = (ctx.league or "").strip().upper()
    vertical = (ctx.vertical or "").strip().lower()
    base_details: dict[str, Any] = {"vertical": vertical, "league": league or None}

    # Resolve calibrator state up front; track the failure mode so the
    # raw-mode escape hatch below can persist it.
    supported = False
    snapshot = None
    state_error: str | None = None
    if not league:
        state_error = "missing league on candidate"
    elif not vertical:
        state_error = "missing vertical on candidate"
    else:
        try:
            from .. import calibrators as _cal
            supported = league in getattr(_cal, "SUPPORTED_LEAGUES", ())
            try:
                snapshot = _cal.get_state().champion(league)
            except Exception as e:  # pragma: no cover — defensive
                log.warning(
                    "uncalibrated_vertical: state.champion(%s) raised: %s",
                    league,
                    e,
                )
                snapshot = None
                state_error = f"calibrator state lookup failed: {e}"
        except Exception as e:  # pragma: no cover — fatal everywhere else
            state_error = f"calibrators import failed: {e}"

    if state_error is None and supported and snapshot is not None:
        return GateResult(
            "uncalibrated_vertical",
            True,
            "",
            {
                **base_details,
                "calibrator_loaded": True,
                "calibrator_type": getattr(snapshot, "type", None),
                "fit_at": getattr(snapshot, "fit_at", None),
                "sample_count": getattr(snapshot, "sample_count", None),
            },
        )

    # No calibrator — check the raw-mode escape hatch.
    raw_key = _raw_mode_override_key(vertical)
    raw_flag = ctx.operator_overrides.get(raw_key)
    raw_enabled = False
    if isinstance(raw_flag, bool):
        raw_enabled = raw_flag
    elif isinstance(raw_flag, (int, float)):
        raw_enabled = bool(raw_flag)
    elif isinstance(raw_flag, str):
        raw_enabled = raw_flag.strip().lower() in ("1", "true", "yes", "on", "y")

    if state_error is not None:
        fail_reason = state_error
    elif not supported:
        fail_reason = f"calibrator missing for league {league!r}"
    else:
        fail_reason = f"no fitted calibrator snapshot for league {league!r}"
    details = {
        **base_details,
        "calibrator_loaded": False,
        "supported": supported,
        "raw_mode_key": raw_key,
        "state_error": state_error,
    }
    if not raw_enabled:
        return GateResult(
            "uncalibrated_vertical",
            False,
            fail_reason + " (no raw-mode override)",
            details,
        )

    # Raw-mode opt-in: PASS but persist a tracking row so the daily
    # honesty report can surface the count in red.  Best-effort —
    # persistence failure must never block a candidate the operator
    # has explicitly opted in.
    try:
        await _persist_risk_gate_trip(
            ctx=ctx,
            gate_name="uncalibrated_vertical_raw_mode",
            reason=fail_reason + " (operator opted into raw mode)",
            details={**details, "raw_mode": True},
        )
    except Exception as e:  # pragma: no cover — persist is best-effort
        log.warning(
            "uncalibrated_vertical raw-mode persist failed for vertical=%s league=%s: %s",
            vertical,
            league,
            e,
        )
    return GateResult(
        "uncalibrated_vertical",
        True,
        "raw-mode override active (uncalibrated trade shipped)",
        {**details, "raw_mode": True},
    )


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


async def gate_daily_loss(ctx: RiskContext) -> GateResult:
    """Auto-halt when today's realized + unrealized P&L draws down past
    the configured caps.

    Two independent thresholds:

    * ``AXIOM_DAILY_LOSS_HALT_USD`` — absolute dollar cap.
    * ``AXIOM_DAILY_LOSS_HALT_PCT`` — percent of start-of-day bankroll.

    Either (or both) may be unset; when neither is configured the gate
    is a no-op with a warning so the operator knows the safety net
    isn't wired. Once tripped the gate flips
    ``system_settings.risk.trading_mode = 'halt'`` (the same end state
    an operator EMERGENCY STOP lands on) so the kill-switch gate keeps
    rejecting candidates without a process restart; only an operator
    can re-enable trading.

    Operator overrides (``daily_loss_halt_usd`` /
    ``daily_loss_halt_pct``) take precedence over the env vars — the
    same pattern the rest of the risk knobs use.
    """
    from . import daily_loss as _dl
    try:
        snap = await _dl.compute_snapshot(ctx.operator_overrides)
    except Exception as e:
        log.warning("daily_loss snapshot failed (degraded): %s", e)
        return GateResult("daily_loss", True, "snapshot degraded", {"error": str(e)})
    details: dict[str, Any] = {
        "date_local": snap.date_local,
        "timezone": snap.timezone,
        "loss_usd": snap.loss_usd,
        "net_pnl_usd": snap.net_pnl_usd,
        "realized_pnl_usd": snap.realized_pnl_usd,
        "unrealized_pnl_usd": snap.unrealized_pnl_usd,
        "threshold_usd": snap.threshold_usd,
        "threshold_pct": snap.threshold_pct,
        "threshold_pct_usd": snap.threshold_pct_usd,
        "start_of_day_bankroll_usd": snap.start_of_day_bankroll_usd,
    }
    if snap.threshold_usd is None and snap.threshold_pct is None:
        # Throttled so a per-cycle burst of candidates only logs once.
        global _last_no_thresholds_warning_at
        now_m = time.monotonic()
        if (
            now_m - _last_no_thresholds_warning_at
            >= _NO_THRESHOLDS_WARNING_THROTTLE_SECONDS
        ):
            _last_no_thresholds_warning_at = now_m
            log.warning(
                "gate_daily_loss configured but no thresholds set "
                "(AXIOM_DAILY_LOSS_HALT_USD / _PCT) — gate is a no-op"
            )
        return GateResult("daily_loss", True, "no thresholds configured", details)
    if not snap.tripped:
        return GateResult("daily_loss", True, "", details)
    # Tripped — run the halt side-effect (idempotent within the day)
    # and reject the candidate.  apply_halt() takes care of dedupe so a
    # burst of candidates during the tripping cycle only fires one alert.
    try:
        await _dl.apply_halt(snap)
    except Exception as e:
        log.warning("daily_loss apply_halt failed: %s", e)
    return GateResult(
        "daily_loss",
        False,
        snap.tripped_reason or "daily loss cap breached",
        details,
    )


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
    # Pre-release blackout (task #202): events-econ markets are
    # blacked out from 5 minutes before the upcoming release until the
    # actual print lands.  Best-effort lookup against `econ_releases`
    # via `app.events_econ.calendar.next_release_at`; any failure
    # short-circuits to a no-op so a sick cache table never silently
    # widens the blackout window.
    if ctx.vertical == "events-econ":
        try:
            from ..events_econ.calendar import (
                resolved_release_for_ticker as _resolved_release_for_ticker,
            )
            from ..events_econ.tickers import (
                InvalidEventsEconTicker as _InvalidEventsEconTicker,
                parse_events_econ_ticker as _parse_events_econ_ticker,
            )
            try:
                ticker = _parse_events_econ_ticker(ctx.market_id)
            except _InvalidEventsEconTicker:
                ticker = None
            if ticker is not None:
                blackout_minutes = int(
                    ctx.operator_overrides.get("events_econ_pre_release_blackout_minutes", 5)
                )
                # Scope to THIS ticker's release instance, not just the
                # next same-type release.  Prevents a CPI-Jan blackout
                # from incorrectly halting trading on CPI-Mar.
                resolved = await _resolved_release_for_ticker(ticker)
                next_at = resolved.release_at if resolved is not None else None
                if next_at is not None:
                    if next_at.tzinfo is None:
                        next_at = next_at.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    from datetime import timedelta as _td
                    window_start = next_at - _td(minutes=max(0, blackout_minutes))
                    if window_start <= now < next_at:
                        return GateResult(
                            "market_cooldown",
                            False,
                            (
                                f"events-econ pre-release blackout "
                                f"({blackout_minutes}m before {ticker.release_type})"
                            ),
                            {
                                "reason": "pre_release_blackout",
                                "release_type": ticker.release_type,
                                "release_at": next_at.isoformat(),
                                "blackout_minutes": blackout_minutes,
                            },
                        )
        except Exception as e:  # pragma: no cover — never kill the gate
            log.debug("events-econ pre-release blackout sub-check failed: %s", e)
    # Release-day blackout (task #203): box-office tickers are blacked
    # out from the event-day open until the weekend actuals print.
    # Best-effort lookup — any failure short-circuits to a no-op so a
    # sick entertainment_signals cache can never silently widen the
    # blackout window.
    if ctx.vertical == "entertainment":
        try:
            from ..entertainment.tickers import (
                InvalidEntertainmentTicker as _InvalidEntertainmentTicker,
                parse_entertainment_ticker as _parse_entertainment_ticker,
            )
            from ..entertainment.signals import (
                resolved_signal_for_ticker as _resolved_signal_for_ticker,
            )
            try:
                ticker = _parse_entertainment_ticker(ctx.market_id)
            except _InvalidEntertainmentTicker:
                ticker = None
            # Only box-office (and, by extension, RT score) tickers
            # carry a release-day blackout.  Awards / charts resolve
            # at scheduled show / chart-week endpoints handled by the
            # cycle's cooldown directly.
            if ticker is not None and ticker.signal_type in {"BOX", "RT"}:
                blackout_minutes = int(
                    ctx.operator_overrides.get(
                        "entertainment_release_day_blackout_minutes", 60
                    )
                )
                resolved = await _resolved_signal_for_ticker(ticker)
                next_at = resolved.event_at if resolved is not None else None
                if next_at is not None:
                    if next_at.tzinfo is None:
                        next_at = next_at.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    from datetime import timedelta as _td
                    window_start = next_at - _td(minutes=max(0, blackout_minutes))
                    if window_start <= now < next_at:
                        return GateResult(
                            "market_cooldown",
                            False,
                            (
                                f"entertainment release-day blackout "
                                f"({blackout_minutes}m before {ticker.signal_type})"
                            ),
                            {
                                "reason": "release_day_blackout",
                                "signal_type": ticker.signal_type,
                                "event_at": next_at.isoformat(),
                                "blackout_minutes": blackout_minutes,
                            },
                        )
        except Exception as e:  # pragma: no cover — never kill the gate
            log.debug("entertainment release-day blackout sub-check failed: %s", e)
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


def _resolve_per_vertical_override(
    ctx: RiskContext, knob: str, fallback: float
) -> float:
    """Look up a knob value with per-vertical precedence (Task #714).

    Resolution order:

      1. ``risk.<knob>.<vertical>``  → ``ctx.operator_overrides['<knob>.<vertical>']``
      2. ``risk.<knob>``             → ``ctx.operator_overrides['<knob>']``
      3. ``fallback``                (typically the aggression-mode default)

    ``ctx.operator_overrides`` is populated by ``_load_operator_overrides``
    which strips the ``risk.`` prefix from every ``system_settings`` row,
    so the per-vertical row ``risk.min_edge_pp.crypto`` shows up here
    as the key ``min_edge_pp.crypto``.

    Vertical names are lower-cased and dash→underscore-normalised so
    the lookup key matches what the gatekeeper-tuning admin endpoint
    writes (``app.runtime_settings._system_settings_key``).
    """
    if ctx.vertical:
        vert = ctx.vertical.strip().lower()
        if vert:
            per_key = f"{knob}.{vert}"
            v = ctx.operator_overrides.get(per_key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    g = ctx.operator_overrides.get(knob)
    if g is not None:
        try:
            return float(g)
        except (TypeError, ValueError):
            pass
    return float(fallback)


async def gate_min_edge(ctx: RiskContext) -> GateResult:
    """edge >= operator override / default."""
    default_pp = _mode_default(ctx, "min_edge_pp", 3.0)
    min_edge = _resolve_per_vertical_override(ctx, "min_edge_pp", default_pp) / 100.0
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
    cap = _resolve_per_vertical_override(
        ctx, "max_confidence_band",
        _mode_default(ctx, "max_confidence_band", 0.30),
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
    floor = _resolve_per_vertical_override(
        ctx, "min_agreement_rate",
        _mode_default(ctx, "min_agreement_rate", 0.50),
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


# ---------------------------------------------------------------------------
# Tail-risk policy gates (task #743 — May 2026 forensic audit).
#
# YTD 2026 SYSTEM trade audit (placed_by IN orchestrator/autotrader/swarm/
# operator_recovery, n=5,347, net −$5,878) found two tail patterns that
# accounted for the entire loss:
#
#   * Deep-OTM bets (min(price, 1−price) < $0.10): 65 trades, 18.5% win
#     rate, **−$5,607**.  Worst single trade — Feb-20 NBA Boston/GSW NO
#     at qty=4,394, YES_price=$0.04 (so NO entry = $0.96, side-aware
#     max-loss = 4,394 × $0.96 ≈ $4,218) — alone exceeded the entire
#     YTD net loss.
#   * Jumbo bets (qty >= 1,000): 15 trades, 33% win rate, **−$5,985**.
#
# Without those 15 worst trades the bot is **+$3,726 YTD**.  The 30-day
# trailing sample (after avg qty fell 63.9 → 10.3 and jumbo trades fell
# 14 → 1) flipped the bot to **+$2,113 at the same 19.5% win rate**, so
# the policy direction is proven.  The good edge ($0.60–$0.90 favorites:
# +$6,326 across 1,500+ trades) is left untouched — these gates are
# defensive caps on the tail, not a strategy change.
# ---------------------------------------------------------------------------


async def gate_tail_price_size_cap(ctx: RiskContext) -> GateResult:
    """Cap exposure on tail-priced markets (task #743).

    A market is "tail-priced" when one side is trading near zero, i.e.
    ``min(YES_price, 1 - YES_price) < tail_price_threshold`` (default
    $0.10).  This catches both directions of the failure mode the
    forensic audit found:

      * Deep-underdog YES bets (small per-contract entry, fragile —
        jumbo qty inflates exposure dramatically).
      * Deep-favorite NO bets (large per-contract entry — the Feb-20
        NBA case: YES@$0.04 → NO@$0.96, qty=4,394, max-loss $4,218).

    When the gate fires, two side-aware caps are checked:

      * ``intended_notional_usd``  (= qty × YES_price; the bot's
        existing notional accounting) must be ≤ ``max_tail_notional_usd``
        (default $400).
      * ``max_loss_usd``  (= qty × entry_price, where entry_price =
        YES_price for YES, (1 − YES_price) for NO) must be ≤
        ``max_tail_loss_usd`` (default $400).  This is what you actually
        lose if the contract expires worthless.

    Both caps support per-vertical override (e.g. crypto deep-OTM is
    flat in the audit and can use a looser cap; sports is the bleeder).
    Any cap set to ``0`` disables that sub-check, and a ``None``
    ``current_price`` short-circuits to PASS so a missing tick never
    nukes a cycle.

    Knobs (resolved via ``_resolve_per_vertical_override``):
      * ``tail_price_threshold``    — default 0.10
      * ``max_tail_notional_usd``   — default 400.0
      * ``max_tail_loss_usd``       — default 400.0
    """
    if ctx.current_price is None:
        return GateResult(
            "tail_price_size_cap", True, "no current_price", {}
        )
    threshold = _resolve_per_vertical_override(
        ctx, "tail_price_threshold", 0.10
    )
    if threshold <= 0:
        return GateResult(
            "tail_price_size_cap",
            True,
            "disabled",
            {"threshold": threshold},
        )
    price = float(ctx.current_price)
    # Side-aware "skew" measure: how close the cheaper leg is to zero.
    # min(price, 1-price) is the displayed price of the cheaper side,
    # so when it's below the threshold we know SOME leg of the market
    # is trading deep in the tail — independent of which side the bot
    # is taking.
    tail_skew = min(price, 1.0 - price)
    if tail_skew >= threshold:
        return GateResult(
            "tail_price_size_cap",
            True,
            "",
            {
                "threshold": threshold,
                "tail_skew": tail_skew,
                "current_price": price,
            },
        )
    side = (ctx.side or "").upper()
    entry_price = price if side == "YES" else (1.0 - price)
    qty = max(int(ctx.intended_quantity), 0)
    max_loss_usd = qty * entry_price
    notional_usd = float(ctx.intended_notional_usd)
    max_loss_cap = _resolve_per_vertical_override(
        ctx, "max_tail_loss_usd", 400.0
    )
    max_notional_cap = _resolve_per_vertical_override(
        ctx, "max_tail_notional_usd", 400.0
    )
    details = {
        "threshold": threshold,
        "tail_skew": tail_skew,
        "current_price": price,
        "entry_price": entry_price,
        "side": side,
        "intended_quantity": qty,
        "intended_notional_usd": notional_usd,
        "max_loss_usd": max_loss_usd,
        "max_loss_cap_usd": max_loss_cap,
        "max_notional_cap_usd": max_notional_cap,
    }
    if max_loss_cap > 0 and max_loss_usd > max_loss_cap:
        reason = (
            f"tail-priced market ({side} entry ${entry_price:.3f}); "
            f"max-loss ${max_loss_usd:.2f} > cap ${max_loss_cap:.2f}"
        )
        await _emit_tail_risk_trip_alert(
            ctx=ctx,
            gate_name="tail_price_size_cap",
            reason=reason,
            details=details,
        )
        return GateResult(
            "tail_price_size_cap", False, reason, details,
        )
    if max_notional_cap > 0 and notional_usd > max_notional_cap:
        reason = (
            f"tail-priced market ({side} entry ${entry_price:.3f}); "
            f"notional ${notional_usd:.2f} > cap ${max_notional_cap:.2f}"
        )
        await _emit_tail_risk_trip_alert(
            ctx=ctx,
            gate_name="tail_price_size_cap",
            reason=reason,
            details=details,
        )
        return GateResult(
            "tail_price_size_cap", False, reason, details,
        )
    return GateResult("tail_price_size_cap", True, "", details)


async def gate_jumbo_quantity_cap(ctx: RiskContext) -> GateResult:
    """Reject any candidate whose contract count is in the jumbo zone
    (task #743).

    Belt-and-suspenders against Kelly + override drift pushing size
    into the catastrophic zone.  The forensic audit found 15 trades
    with ``qty >= 1,000`` totalled **−$5,985** (33% win rate, −$399
    EV/trade) — independent of price, large qty kept blowing up.  The
    default cap of 750 sits comfortably below the audit's jumbo
    boundary so an operator running a deliberate "big swing" still has
    to opt in via the per-vertical override.

    Set ``jumbo_quantity_cap`` to 0 (per-vertical or global) to
    disable the gate entirely — useful for paper-only environments.

    Knobs:
      * ``jumbo_quantity_cap`` — default 750 contracts.
    """
    cap = int(_resolve_per_vertical_override(ctx, "jumbo_quantity_cap", 750.0))
    qty = int(ctx.intended_quantity)
    details = {"intended_quantity": qty, "cap": cap}
    if cap <= 0:
        return GateResult(
            "jumbo_quantity_cap", True, "disabled", details
        )
    if qty > cap:
        reason = f"intended quantity {qty} > jumbo cap {cap}"
        await _emit_tail_risk_trip_alert(
            ctx=ctx,
            gate_name="jumbo_quantity_cap",
            reason=reason,
            details=details,
        )
        return GateResult(
            "jumbo_quantity_cap", False, reason, details,
        )
    return GateResult("jumbo_quantity_cap", True, "", details)


async def gate_sharpe_ratio(ctx: RiskContext) -> GateResult:
    """Round-2 audit gate: realised trailing-Sharpe floor.

    Walks the most recent N closed trades scoped to the candidate's
    ``(vertical, league)`` (league join via ``swarm_debates``) and
    rejects when the simple ``mean(returns) / pstdev(returns)`` ratio
    has decayed below ``risk.sharpe_min_ratio`` (default 0.0).

    Returns from each historical trade are computed as
    ``pnl / max(notional, 1.0)`` so a single oversized loss can't
    dominate; ``notional = quantity * price`` (snapshot at fill).

    Operator knobs (read via ``_resolve_per_vertical_override`` so the
    existing per-vertical override pipeline applies):
      * ``sharpe_min_ratio``     — floor to clear (-3.0 .. 5.0).
      * ``sharpe_window_trades`` — trailing window size (5 .. 500).
      * ``sharpe_min_samples``   — minimum sample count before the gate
        can veto; below this it returns PASS so cold-start verticals
        aren't blocked by their own newness.

    Insufficient history, zero variance, or DB failure all PASS — this
    gate exists to catch *decayed* trades, not to block fresh books.
    """
    window = int(_resolve_per_vertical_override(
        ctx, "sharpe_window_trades",
        ctx.operator_overrides.get("sharpe_window_trades", 30),
    ))
    min_samples = int(_resolve_per_vertical_override(
        ctx, "sharpe_min_samples",
        ctx.operator_overrides.get("sharpe_min_samples", 10),
    ))
    from ..runtime_settings import tuned_default as _tuned_default
    _sharpe_default = _tuned_default("sharpe_min_ratio", ctx.vertical)
    threshold = float(_resolve_per_vertical_override(
        ctx, "sharpe_min_ratio",
        ctx.operator_overrides.get(
            "sharpe_min_ratio",
            _sharpe_default if _sharpe_default is not None else 0.0,
        ),
    ))
    if window < 5:
        window = 5
    if min_samples < 1:
        min_samples = 1
    vertical = (ctx.vertical or "").strip().lower()
    league = (ctx.league or "").strip().upper()
    try:
        rows = await db.fetch(
            """
            SELECT
                t.pnl::float                                      AS pnl,
                GREATEST(
                    coalesce((t.quantity * t.price)::float, 0.0),
                    1.0
                )                                                  AS notional
              FROM trades t
              LEFT JOIN swarm_debates d ON d.id = t.debate_id
             WHERE t.pnl IS NOT NULL
               AND t.settled_at IS NOT NULL
               AND ($1 = '' OR lower(coalesce(t.vertical, '')) = $1)
               AND ($2 = '' OR upper(coalesce(d.league, '')) = $2)
             ORDER BY t.settled_at DESC
             LIMIT $3
            """,
            vertical, league, int(window),
        )
    except Exception as e:
        log.debug("sharpe_ratio query failed: %s", e)
        return GateResult(
            "sharpe_ratio", True, "query degraded",
            {"error": str(e), "threshold": threshold},
        )
    base = {
        "vertical": vertical or None,
        "league": league or None,
        "window": int(window),
        "min_samples": int(min_samples),
        "threshold": threshold,
    }
    if len(rows) < min_samples:
        return GateResult(
            "sharpe_ratio", True, "insufficient history",
            {**base, "samples": len(rows)},
        )
    returns = [
        float(r["pnl"]) / float(r["notional"])
        for r in rows if r["notional"]
    ]
    if len(returns) < min_samples:
        return GateResult(
            "sharpe_ratio", True, "insufficient priced history",
            {**base, "samples": len(returns)},
        )
    import statistics
    mean = statistics.fmean(returns)
    stdev = statistics.pstdev(returns)
    if stdev <= 0:
        return GateResult(
            "sharpe_ratio", True, "zero variance",
            {
                **base,
                "samples": len(returns),
                "mean_return": mean,
                "stdev_return": stdev,
            },
        )
    sharpe = mean / stdev
    details = {
        **base,
        "samples": len(returns),
        "mean_return": mean,
        "stdev_return": stdev,
        "sharpe": sharpe,
    }
    if sharpe < threshold:
        return GateResult(
            "sharpe_ratio", False,
            f"trailing Sharpe {sharpe:.3f} < floor {threshold:.3f}",
            details,
        )
    return GateResult("sharpe_ratio", True, "", details)


async def gate_ruin_monte_carlo(ctx: RiskContext) -> GateResult:
    """Round-2 audit gate: bounded-path probability-of-ruin estimator.

    Bootstraps the trailing-N realised return distribution and runs
    ``risk.ruin_paths`` Monte-Carlo paths of ``risk.ruin_horizon_trades``
    trades each, sized to the same intended-notional fraction this
    candidate would carry.  A path "ruins" the moment the simulated
    bankroll dips below the operator's bankroll floor.  Vetos when
    ``P(ruin) > risk.ruin_max_probability`` (default 0.05).

    Operator knobs (per-vertical override pipeline applies):
      * ``ruin_max_probability`` — fail above this (0.0 .. 1.0).
      * ``ruin_paths``           — MC paths (200 .. 50_000).
      * ``ruin_horizon_trades``  — trades simulated per path (10 .. 500).
      * ``ruin_min_samples``     — minimum return samples before the gate
        can veto; below this it PASSes (cold-start protection).
      * ``bankroll_floor_usd``   — re-uses the same floor that
        ``gate_bankroll_floor`` enforces, so the two gates agree on
        what "ruined" means.

    Cold-start, missing bankroll snapshot, or DB failure all PASS.
    """
    from ..runtime_settings import tuned_default as _tuned_default
    _ruin_default = _tuned_default("ruin_max_probability", ctx.vertical)
    threshold = float(_resolve_per_vertical_override(
        ctx, "ruin_max_probability",
        ctx.operator_overrides.get(
            "ruin_max_probability",
            _ruin_default if _ruin_default is not None else 0.05,
        ),
    ))
    paths = int(_resolve_per_vertical_override(
        ctx, "ruin_paths",
        ctx.operator_overrides.get("ruin_paths", 10_000),
    ))
    horizon = int(_resolve_per_vertical_override(
        ctx, "ruin_horizon_trades",
        ctx.operator_overrides.get("ruin_horizon_trades", 100),
    ))
    min_samples = int(_resolve_per_vertical_override(
        ctx, "ruin_min_samples",
        ctx.operator_overrides.get("ruin_min_samples", 20),
    ))
    floor = float(ctx.operator_overrides.get("bankroll_floor_usd", 100.0))
    # Defensive bounds so an operator typo can't blow up the loop.
    paths = max(200, min(paths, 50_000))
    horizon = max(10, min(horizon, 500))
    min_samples = max(1, min_samples)

    bankroll = ctx.bankroll_usd or 0.0
    base = {
        "threshold": threshold,
        "paths": paths,
        "horizon": horizon,
        "min_samples": min_samples,
        "floor_usd": floor,
        "bankroll_usd": bankroll,
        "intended_notional_usd": ctx.intended_notional_usd,
    }
    if bankroll <= 0:
        return GateResult(
            "ruin_monte_carlo", True, "no bankroll snapshot", base
        )
    vertical = (ctx.vertical or "").strip().lower()
    league = (ctx.league or "").strip().upper()
    try:
        rows = await db.fetch(
            """
            SELECT
                t.pnl::float                                      AS pnl,
                GREATEST(
                    coalesce((t.quantity * t.price)::float, 0.0),
                    1.0
                )                                                  AS notional
              FROM trades t
              LEFT JOIN swarm_debates d ON d.id = t.debate_id
             WHERE t.pnl IS NOT NULL
               AND t.settled_at IS NOT NULL
               AND ($1 = '' OR lower(coalesce(t.vertical, '')) = $1)
               AND ($2 = '' OR upper(coalesce(d.league, '')) = $2)
             ORDER BY t.settled_at DESC
             LIMIT 500
            """,
            vertical, league,
        )
    except Exception as e:
        log.debug("ruin_monte_carlo query failed: %s", e)
        return GateResult(
            "ruin_monte_carlo", True, "query degraded",
            {**base, "error": str(e)},
        )
    returns = [
        float(r["pnl"]) / float(r["notional"])
        for r in rows if r["notional"]
    ]
    if len(returns) < min_samples:
        return GateResult(
            "ruin_monte_carlo", True, "insufficient history",
            {**base, "samples": len(returns)},
        )
    # Each MC step compounds the candidate's intended notional by a
    # bootstrap-sampled return — a $X notional bet pays $X * r per step
    # on the realised distribution.  Using the candidate notional (not
    # the historical notional) keeps the simulation aligned with the
    # *actual* trade we're about to place.
    import random
    rng = random.Random(0xA1C7)  # deterministic seed → repeatable details
    notional = max(float(ctx.intended_notional_usd), 1.0)
    target = floor
    ruined = 0
    n_returns = len(returns)
    for _ in range(paths):
        balance = bankroll
        for _ in range(horizon):
            r = returns[rng.randrange(n_returns)]
            balance += notional * r
            if balance < target:
                ruined += 1
                break
    p_ruin = ruined / float(paths)
    details = {
        **base,
        "samples": len(returns),
        "p_ruin": p_ruin,
        "ruined_paths": ruined,
    }
    if p_ruin > threshold:
        return GateResult(
            "ruin_monte_carlo", False,
            f"P(ruin) {p_ruin:.4f} > cap {threshold:.4f}",
            details,
        )
    return GateResult("ruin_monte_carlo", True, "", details)


async def gate_segment_throttler(ctx: RiskContext) -> GateResult:
    """Round-2 audit gate: per-segment recent-loss throttle.

    A *segment* is the ``(league, market_type)`` tuple — moneyline,
    spread, total, etc.  When a single segment racks up
    ``risk.segment_max_losses`` (default 3) settled losing trades inside
    ``risk.segment_window_hours`` (default 24) the gate vetoes new
    candidates from that segment so a localised model regression doesn't
    bleed the bankroll while we wait for the per-vertical kill switch
    to be tripped manually.

    Both knobs respect the per-vertical override pipeline.  When the
    candidate has no ``league`` *or* no ``market_type`` (off-sports
    verticals, exotic books) the gate PASSes — segments are only
    well-defined inside the sports/market-type lattice.
    """
    league = (ctx.league or "").strip().upper()
    market_type = (ctx.market_type or "").strip().lower()
    from ..runtime_settings import tuned_default as _tuned_default
    _seg_default = _tuned_default("segment_max_losses", ctx.vertical)
    max_losses = int(_resolve_per_vertical_override(
        ctx, "segment_max_losses",
        ctx.operator_overrides.get(
            "segment_max_losses",
            _seg_default if _seg_default is not None else 3,
        ),
    ))
    window_hours = float(_resolve_per_vertical_override(
        ctx, "segment_window_hours",
        ctx.operator_overrides.get("segment_window_hours", 24.0),
    ))
    if max_losses <= 0:
        return GateResult(
            "segment_throttler", True, "disabled",
            {"max_losses": max_losses},
        )
    base = {
        "league": league or None,
        "market_type": market_type or None,
        "max_losses": max_losses,
        "window_hours": window_hours,
    }
    if not league or not market_type:
        return GateResult(
            "segment_throttler", True, "no segment on candidate", base
        )
    try:
        n = await db.fetchval(
            """
            SELECT count(*)::int
              FROM trades t
              JOIN swarm_debates d ON d.id = t.debate_id
             WHERE t.pnl IS NOT NULL
               AND t.pnl < 0
               AND t.settled_at IS NOT NULL
               AND t.settled_at > now() - ($3 || ' hours')::interval
               AND upper(coalesce(d.league, '')) = $1
               AND lower(coalesce(d.market_type, '')) = $2
            """,
            league, market_type, str(window_hours),
        )
    except Exception as e:
        log.debug("segment_throttler query failed: %s", e)
        return GateResult(
            "segment_throttler", True, "query degraded",
            {**base, "error": str(e)},
        )
    losses = int(n or 0)
    details = {**base, "losses_in_window": losses}
    if losses >= max_losses:
        return GateResult(
            "segment_throttler", False,
            (
                f"{league}/{market_type}: {losses} losses in last "
                f"{window_hours:g}h >= cap {max_losses}"
            ),
            details,
        )
    return GateResult("segment_throttler", True, "", details)


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
    fraction_cap = _resolve_per_vertical_override(
        ctx, "kelly_fraction_cap",
        _mode_default(ctx, "kelly_fraction_cap", 0.25),
    )
    # Per-vertical Kelly multiplier from the registry (operator override
    # via "kelly_multiplier" still wins).  Defaults to 1.0 so existing
    # verticals behave identically until a spec opts into a different
    # multiplier.
    from .. import verticals as _verticals
    spec = _verticals.get_spec(ctx.vertical) if ctx.vertical else None
    spec_multiplier = spec.kelly_multiplier if spec is not None else 1.0
    # Per-vertical override: the auto-promote pipeline (e.g. tech-ai)
    # writes `kelly_multiplier_<vertical>` into operator_overrides /
    # system_settings so a single vertical can be bumped past the
    # spec default without touching the others.  Hyphens in vertical
    # names are normalised to underscores for the lookup key.
    vertical_key: str | None = None
    if ctx.vertical:
        vertical_key = f"kelly_multiplier_{ctx.vertical.replace('-', '_')}"
    per_vertical_override = (
        ctx.operator_overrides.get(vertical_key) if vertical_key else None
    )
    if per_vertical_override is not None:
        kelly_multiplier = float(per_vertical_override)
    else:
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

    # Commodities correlation sub-check (task #198).  Inside the
    # per_vertical_cap gate so the 12-gate invariant is preserved.
    # Projects the candidate's intended notional against open exposure
    # in correlated commodities (11 high-magnitude pairs from
    # `app.commodities.correlations`) and rejects if the correlation-
    # weighted projection exceeds the operator cap (default 25%
    # bankroll).  Best-effort: any failure short-circuits to a no-op
    # so the gate falls back to the per-vertical aggregate above.
    if ctx.vertical == "commodities" and bankroll > 0:
        try:
            from ..commodities.correlations import correlated_exposure
            from ..commodities.tickers import (
                InvalidCommodityTicker,
                parse_commodity_ticker,
            )
            try:
                cand_ticker = parse_commodity_ticker(ctx.market_id)
                cand_symbol = cand_ticker.symbol
            except InvalidCommodityTicker:
                cand_symbol = None
            if cand_symbol is not None:
                corr_cap_pct = float(
                    ctx.operator_overrides.get("commodities_correlation_cap_pct", 0.25)
                )
                corr_cap_usd = bankroll * corr_cap_pct
                # `open_positions_per_matchup_usd` is keyed `LEAGUE:matchup`;
                # the orchestrator persists commodities matchup as the
                # canonical symbol so we can reuse the same dict here.
                open_per_symbol: dict[str, float] = {}
                for k, v in ctx.open_positions_per_matchup_usd.items():
                    if k.upper().startswith("COMMODITIES:"):
                        sym = k.split(":", 1)[1].upper()
                        open_per_symbol[sym] = open_per_symbol.get(sym, 0.0) + float(v)
                corr_report = correlated_exposure(
                    open_per_symbol,
                    cand_symbol,
                    ctx.intended_notional_usd,
                )
                if corr_report["projected"] > corr_cap_usd:
                    return GateResult(
                        "per_vertical_cap",
                        False,
                        (
                            f"commodities correlated exposure "
                            f"${corr_report['projected']:.2f} > cap "
                            f"${corr_cap_usd:.2f}"
                        ),
                        {
                            "vertical": ctx.vertical,
                            "sub_check": "commodities_correlation",
                            "intended_symbol": cand_symbol,
                            "projected": corr_report["projected"],
                            "cap_usd": corr_cap_usd,
                            "cap_pct": corr_cap_pct,
                            "pair_contributions": corr_report["pair_contributions"],
                        },
                    )
        except Exception as e:  # pragma: no cover — never kill the gate
            log.debug("commodities correlation sub-check failed: %s", e)

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
_PLAYER_PROP_OVERRIDE_KEY = "_player_prop_meta"


async def gate_player_props(ctx: RiskContext) -> GateResult:
    """Player-level pre-trade gate (task #823).

    No-op (PASS) for any candidate that doesn't carry a
    ``ctx.operator_overrides[_player_prop_meta]`` block — i.e. every
    non-player-prop vertical, plus player-prop candidates whose
    discovery path forgot to populate it (which is itself logged as a
    PASS so the operator can see the count of unmetered tickets in
    the daily honesty report).

    When the meta block IS present, rejects the candidate if any of
    the following are true:

    * ``status`` is in :data:`~app.player_props.injuries._DEFAULT_BLOCKING`
      — the player is OUT / DOUBTFUL / QUESTIONABLE / GTD.
    * ``sample_count`` is below the configured ``min_sample`` (default
      from ``AXIOM_WNBA_PROPS_MIN_SAMPLE``) — the per-player baseline
      is too thin to trust.
    * ``line_move_bps`` exceeds ``AXIOM_WNBA_PROPS_LINE_MOVE_BPS``
      (default 300) — the prop line has moved enough since the
      candidate was built that we'd be chasing.
    * ``matchup_bounded`` is True — the pace × opp-allowed adjustment
      hit the ±25% combined cap, which is a quality signal that the
      adjustment is being driven by a noisy early-season sample.

    Operator-side knobs:

    * ``risk.player_props_min_sample_<vertical>`` — per-vertical
      override of the default minimum sample.
    * ``risk.player_props_line_move_bps_<vertical>`` — per-vertical
      line-move threshold.
    * ``risk.player_props_allow_questionable_<vertical>`` — when
      truthy, downgrades QUESTIONABLE / GTD to a PASS so the operator
      can ship props on probable starters once they trust the league
      handles GTDs honestly.

    The gate **never** does its own DB lookup — every input arrives on
    ``ctx`` so the gate stays cheap and deterministic in tests.
    """
    import os as _os
    meta = ctx.operator_overrides.get(_PLAYER_PROP_OVERRIDE_KEY)
    if not isinstance(meta, dict) or not meta:
        return GateResult(
            "player_props", True, "", {"player_prop_meta": False},
        )

    vertical = (ctx.vertical or "").strip().lower()
    overrides = ctx.operator_overrides
    details: dict[str, Any] = {
        "vertical": vertical,
        "player": meta.get("player"),
        "stat": meta.get("stat"),
    }

    # 1. Player availability ------------------------------------------------
    status = (meta.get("status") or "").strip().upper()
    if status:
        details["status"] = status
        try:
            from ..player_props.injuries import is_blocking_status
        except Exception:  # pragma: no cover — defensive
            is_blocking_status = lambda s: False  # noqa: E731
        allow_questionable = bool(
            overrides.get(f"player_props_allow_questionable_{vertical}")
        )
        blocking = is_blocking_status(status)
        if blocking and allow_questionable and status in {
            "QUESTIONABLE", "GAME TIME DECISION", "GTD",
        }:
            blocking = False
            details["allow_questionable"] = True
        if blocking:
            return GateResult(
                "player_props", False,
                f"player status {status} blocks the prop",
                details,
            )

    # 2. Per-player sample size --------------------------------------------
    try:
        from ..player_props.baseline import DEFAULT_MIN_SAMPLE
    except Exception:  # pragma: no cover — defensive
        DEFAULT_MIN_SAMPLE = 5
    min_sample = int(
        overrides.get(f"player_props_min_sample_{vertical}")
        or DEFAULT_MIN_SAMPLE
    )
    sample_count = meta.get("sample_count")
    if sample_count is not None:
        try:
            sample_count_int = int(sample_count)
        except (TypeError, ValueError):
            sample_count_int = 0
        details["sample_count"] = sample_count_int
        details["min_sample"] = min_sample
        if sample_count_int < min_sample:
            return GateResult(
                "player_props", False,
                f"baseline sample {sample_count_int} < min {min_sample}",
                details,
            )

    # 3. Line-move guard ----------------------------------------------------
    line_move_bps = meta.get("line_move_bps")
    if line_move_bps is not None:
        try:
            line_move_v = abs(float(line_move_bps))
        except (TypeError, ValueError):
            line_move_v = 0.0
        max_line_move = float(
            overrides.get(f"player_props_line_move_bps_{vertical}")
            or _os.environ.get("AXIOM_WNBA_PROPS_LINE_MOVE_BPS", "300")
        )
        details["line_move_bps"] = line_move_v
        details["line_move_bps_threshold"] = max_line_move
        if line_move_v > max_line_move:
            return GateResult(
                "player_props", False,
                f"line moved {line_move_v:.0f}bps > {max_line_move:.0f}",
                details,
            )

    # 4. Matchup adjustment hit the bound ----------------------------------
    if meta.get("matchup_bounded"):
        details["matchup_bounded"] = True
        details["matchup_factor"] = meta.get("matchup_factor")
        return GateResult(
            "player_props", False,
            "matchup adjustment hit configured bound — refusing prop",
            details,
        )

    return GateResult("player_props", True, "", details)


GATES: tuple[tuple[str, GateFn], ...] = (
    ("kill_switch", gate_kill_switch),
    ("daily_loss", gate_daily_loss),
    ("vertical_halted", gate_vertical_halted),
    # Task #767 — block any candidate whose (vertical, league) lacks a
    # calibrator snapshot, unless the operator has opted that vertical
    # into "ship raw" via runtime settings.  Sits before the cooldown /
    # swarm gates because (a) it's a pure in-process state read (no
    # DB round-trip) and (b) calibrator coverage is a precondition for
    # *every* downstream sizing decision — no point doing more work
    # on a candidate we wouldn't ship even if it cleared agreement.
    ("uncalibrated_vertical", gate_uncalibrated_vertical),
    # Player-prop gate (task #823).  No-op for non-player verticals —
    # only fires when ctx.operator_overrides carries a `_player_prop`
    # block populated by the player-props candidate-discovery path.
    # Sits before market_cooldown / swarm_approval so a stale player
    # status / sub-min baseline / line-move trip doesn't burn a debate
    # round-trip on a candidate we'd reject anyway.
    ("player_props", gate_player_props),
    ("market_cooldown", gate_market_cooldown),
    ("swarm_approval", gate_swarm_approval),
    ("min_edge", gate_min_edge),
    ("drift", gate_drift),
    ("agreement", gate_agreement),
    # Tail-risk policy gates (task #743 — May 2026 forensic audit).
    # Cheap, no-DB caps that reject deep-OTM and jumbo-sized
    # candidates BEFORE the round-2 audit gates spend DB round-trips on
    # trailing-Sharpe / Monte-Carlo ruin / segment-throttler queries.
    # Defaults are sized so the forensic audit's worst-case Feb-20 NBA
    # trade (qty=4,394, NO entry=$0.96, max-loss $4,218) would have
    # been rejected; see each gate's docstring for the full audit
    # provenance.
    ("tail_price_size_cap", gate_tail_price_size_cap),
    ("jumbo_quantity_cap", gate_jumbo_quantity_cap),
    # Round-2 audit gates (task #730) — model-side decay + portfolio
    # ruin + per-segment loss throttle.  Sit between the swarm/agreement
    # gates and the sizing gates so a decayed book is rejected BEFORE
    # we burn DB round-trips on the bankroll snapshot + position
    # aggregates that gate_kelly_fraction / gate_global_exposure_cap
    # need.
    ("sharpe_ratio", gate_sharpe_ratio),
    ("ruin_monte_carlo", gate_ruin_monte_carlo),
    ("segment_throttler", gate_segment_throttler),
    ("kelly_fraction", gate_kelly_fraction),
    ("bankroll_floor", gate_bankroll_floor),
    ("global_exposure_cap", gate_global_exposure_cap),
    ("per_vertical_cap", gate_per_vertical_cap),
    ("max_concurrent_positions", gate_max_concurrent_positions),
    ("duplicate_trade", gate_duplicate_trade),
)
assert len(GATES) == 21, (
    "21 gates: 12 original + daily_loss (task #268) + vertical_halted "
    "(task #271) + sharpe_ratio + ruin_monte_carlo + segment_throttler "
    "(task #730) + tail_price_size_cap + jumbo_quantity_cap (task #743) "
    "+ uncalibrated_vertical (task #767) + player_props (task #823)"
)


async def run_all_gates(
    ctx: RiskContext,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[bool, list[GateResult]]:
    """Run gates in order, short-circuiting on first FAIL.  Each gate's
    result is forwarded via `on_event` (if provided) before evaluating
    the next gate so the WS subscriber sees them stream live.

    Returns (all_passed, results_so_far).  `results_so_far` ends at the
    first failure when short-circuited; otherwise contains all 12.

    On any FAIL we also write one append-only row to ``risk_gate_trips``
    so the daily honesty report's "Gate trips (last 24h)" panel reflects
    every gate, not just the tail-risk gate.  Persistence is best-effort
    and never affects the gate decision.
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
            try:
                await _persist_risk_gate_trip(
                    ctx=ctx,
                    gate_name=name,
                    reason=r.reason or "",
                    details=dict(r.details or {}),
                )
            except Exception as e:  # pragma: no cover — persistence must never break the gate
                log.warning(
                    "risk_gate_trips persist (run_all_gates) failed for %s: %s",
                    name,
                    e,
                )
            return False, results
    return True, results
