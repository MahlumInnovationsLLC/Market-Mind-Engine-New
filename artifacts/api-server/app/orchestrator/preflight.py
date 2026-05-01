"""9-point pre-flight checklist for the live-execute toggle (Task #752).

Round-11 audit Bug #7: ``POST /api/orchestrator/live-execute`` would happily
flip the platform from ``paper`` to ``live`` while the system was in any of
several "do not trade real money" states (kill-switch tripped, no recent
calibrator fit, daily-loss already 95% used, etc).  The flip was a single
boolean — no safety interlock.

This module exposes :func:`run_preflight` which evaluates **nine** independent
predicates.  Each predicate returns a :class:`PreflightCheck` with the same
shape so the route can short-circuit on the first failure and surface a
human-actionable hint to the operator.

The checks are intentionally **conservative**: any predicate that errors out
internally returns ``passed=False`` with a "look at logs" hint rather than
silently passing.  Operators should never see a green pre-flight when the
underlying signal is unknowable.

Wire-up: see ``app/main.py`` ``orchestrator_live_execute_toggle`` — the
route calls ``run_preflight()`` only when the caller is requesting
``enabled=True`` (paper-flips remain unrestricted; you can always go back
to paper without ceremony).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .. import db


log = logging.getLogger("axiom.orchestrator.preflight")


# ---------------------------------------------------------------------------
# Tunables — every threshold is env-overridable so SREs can relax a check
# during a known-degraded window without a code change.
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


# Calibrator fit must be no older than this many hours.  24h matches the
# cadence of the daily retrain loop with a generous slack.
def _calibrator_max_age_hours() -> float:
    return _env_float("AXIOM_PREFLIGHT_CALIBRATOR_MAX_HOURS", 24.0)


# Pipeline must have started a cycle in the last N minutes — otherwise the
# orchestrator is wedged and we shouldn't be flipping live.
def _cycle_max_age_minutes() -> float:
    return _env_float("AXIOM_PREFLIGHT_CYCLE_MAX_MINUTES", 15.0)


# Live bankroll floor (USD).  Below this, flipping to live would either
# instantly trip the daily-loss gate or attempt to size into a stub.
def _bankroll_floor_usd() -> float:
    return _env_float("AXIOM_PREFLIGHT_BANKROLL_FLOOR_USD", 100.0)


# Daily-loss head-room: pre-flight fails if used_pct >= this.  90% by
# default — once you're 90% of the way to the cap, "going live" is a
# guaranteed halt within minutes.
def _daily_loss_max_used_pct() -> float:
    return _env_float("AXIOM_PREFLIGHT_DAILY_LOSS_MAX_USED_PCT", 90.0)


# Swarm-provider fallback rate ceiling.  Above this, the swarm is
# silently dominated by backups and the verdict distribution is biased.
def _swarm_provider_max_fallback_rate() -> float:
    return _env_float("AXIOM_PREFLIGHT_SWARM_FALLBACK_MAX_RATE", 0.30)


# Watchdog cooldown count ceiling.  More than N markets simultaneously
# in cooldown == bad market data, don't go live.
def _market_cooldown_max_active() -> int:
    return _env_int("AXIOM_PREFLIGHT_MARKET_COOLDOWN_MAX_ACTIVE", 25)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightCheck:
    """One independent predicate's verdict.

    ``id`` is the stable machine-readable name (used by ops dashboards
    and the 409 body).  ``label`` is the human-readable name surfaced
    in the alert.  ``hint`` is an actionable next step shown to the
    operator (None on pass).  ``details`` is a small dict of the
    measured values — present even on pass so the dashboard can render
    "calibrator: fit 3.2h ago" without re-querying.
    """
    id: str
    label: str
    passed: bool
    hint: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "passed": self.passed,
            "hint": self.hint,
            "details": self.details or {},
        }


@dataclass(frozen=True)
class PreflightResult:
    """Aggregate verdict for the whole 9-check run."""
    passed: bool
    checks: list[PreflightCheck]

    @property
    def first_failure(self) -> PreflightCheck | None:
        for c in self.checks:
            if not c.passed:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Individual predicates
# ---------------------------------------------------------------------------


async def check_calibrator_freshness() -> PreflightCheck:
    """At least one vertical has a calibrator fit within the freshness window."""
    max_age_hours = _calibrator_max_age_hours()
    try:
        row = await db.fetchrow(
            """
            SELECT vertical,
                   extract(epoch FROM (now() - created_at))::float / 3600.0 AS age_hours
              FROM calibration_snapshots
             WHERE COALESCE(drift_status->>'status', '') = 'retrained'
               AND calibrator_state IS NOT NULL
               AND calibrator_state::text <> '{}'
             ORDER BY created_at DESC
             LIMIT 1
            """
        )
    except Exception as e:
        log.warning("[preflight] calibrator_freshness query failed: %s", e)
        return PreflightCheck(
            id="calibrator_freshness",
            label="Calibrator freshness",
            passed=False,
            hint="DB query for calibrator freshness failed; check API logs.",
        )
    if row is None:
        return PreflightCheck(
            id="calibrator_freshness",
            label="Calibrator freshness",
            passed=False,
            hint=(
                "No retrained calibrator on file. Run the retrain loop "
                "(`POST /api/orchestrator/retrain`) before flipping live."
            ),
        )
    age = float(row["age_hours"] or 0.0)
    vertical = str(row["vertical"] or "?")
    if age > max_age_hours:
        return PreflightCheck(
            id="calibrator_freshness",
            label="Calibrator freshness",
            passed=False,
            hint=(
                f"Newest calibrator ({vertical}) is {age:.1f}h old; "
                f"max allowed is {max_age_hours:.0f}h. Trigger retrain."
            ),
            details={"vertical": vertical, "age_hours": round(age, 2)},
        )
    return PreflightCheck(
        id="calibrator_freshness",
        label="Calibrator freshness",
        passed=True,
        details={"vertical": vertical, "age_hours": round(age, 2)},
    )


async def check_pipeline_health() -> PreflightCheck:
    """Orchestrator has started a cycle in the recent past."""
    max_age_min = _cycle_max_age_minutes()
    try:
        age_seconds = await db.fetchval(
            """
            SELECT extract(epoch FROM (now() - max(started_at)))::float
              FROM trading_cycles
            """
        )
    except Exception as e:
        log.warning("[preflight] pipeline_health query failed: %s", e)
        return PreflightCheck(
            id="pipeline_health",
            label="Pipeline cycle heartbeat",
            passed=False,
            hint="DB query for trading_cycles failed; check API logs.",
        )
    if age_seconds is None:
        return PreflightCheck(
            id="pipeline_health",
            label="Pipeline cycle heartbeat",
            passed=False,
            hint=(
                "Orchestrator has never run a cycle. Start the scheduler "
                "before flipping live."
            ),
        )
    age_min = float(age_seconds) / 60.0
    if age_min > max_age_min:
        return PreflightCheck(
            id="pipeline_health",
            label="Pipeline cycle heartbeat",
            passed=False,
            hint=(
                f"Last cycle was {age_min:.1f} min ago; max allowed is "
                f"{max_age_min:.0f} min. Investigate scheduler health."
            ),
            details={"last_cycle_minutes_ago": round(age_min, 2)},
        )
    return PreflightCheck(
        id="pipeline_health",
        label="Pipeline cycle heartbeat",
        passed=True,
        details={"last_cycle_minutes_ago": round(age_min, 2)},
    )


async def check_bankroll_sanity() -> PreflightCheck:
    """Live ledger has a recent positive balance above the floor."""
    floor_usd = _bankroll_floor_usd()
    try:
        cents = await db.fetchval(
            """
            SELECT balance_cents
              FROM bankroll_events
             WHERE exchange = 'kalshi'
               AND trading_mode = 'live'
             ORDER BY occurred_at DESC, id DESC
             LIMIT 1
            """
        )
    except Exception as e:
        log.warning("[preflight] bankroll_sanity query failed: %s", e)
        return PreflightCheck(
            id="bankroll_sanity",
            label="Live bankroll sanity",
            passed=False,
            hint="DB query for live bankroll failed; check API logs.",
        )
    if cents is None:
        return PreflightCheck(
            id="bankroll_sanity",
            label="Live bankroll sanity",
            passed=False,
            hint=(
                "No live bankroll observation yet. Wait for "
                "`bankroll_sync` to record the Kalshi balance."
            ),
        )
    usd = float(cents) / 100.0
    if usd < floor_usd:
        return PreflightCheck(
            id="bankroll_sanity",
            label="Live bankroll sanity",
            passed=False,
            hint=(
                f"Live bankroll is ${usd:.2f}; floor is ${floor_usd:.2f}. "
                f"Top up the Kalshi account before flipping live."
            ),
            details={"balance_usd": round(usd, 2), "floor_usd": floor_usd},
        )
    return PreflightCheck(
        id="bankroll_sanity",
        label="Live bankroll sanity",
        passed=True,
        details={"balance_usd": round(usd, 2), "floor_usd": floor_usd},
    )


async def check_kill_switch() -> PreflightCheck:
    """Process-wide kill switch is not tripped."""
    try:
        from .. import exchange_kill_switch as _eks
        state = _eks.kill_switch_state()
    except Exception as e:
        log.warning("[preflight] kill_switch read failed: %s", e)
        return PreflightCheck(
            id="kill_switch",
            label="Kill switch",
            passed=False,
            hint="Cannot read kill-switch state; check API logs.",
        )
    if state.get("halted"):
        return PreflightCheck(
            id="kill_switch",
            label="Kill switch",
            passed=False,
            hint=(
                "Kill switch is TRIPPED — clear it via "
                "`POST /api/admin/kill-switch/reset` first."
            ),
            details=dict(state),
        )
    return PreflightCheck(
        id="kill_switch",
        label="Kill switch",
        passed=True,
        details=dict(state),
    )


async def check_trading_mode_target() -> PreflightCheck:
    """Honesty check: the operator is in fact requesting live mode.

    This is a no-op pass for the case where pre-flight is invoked
    against an enabled=True request — but having it as an explicit
    check keeps the response symmetric (always 9 entries) and gives
    the dashboard something to render in the row.
    """
    return PreflightCheck(
        id="trading_mode_target",
        label="Target mode = live",
        passed=True,
        details={"requested_mode": "live"},
    )


async def check_daily_loss_headroom() -> PreflightCheck:
    """Daily-loss budget has at least (100 - max_used_pct)% remaining."""
    max_used = _daily_loss_max_used_pct()
    try:
        from ..risk import daily_loss as _dl
        from .cycle import _load_operator_overrides
        try:
            overrides = await _load_operator_overrides()
        except Exception:
            overrides = {}
        snap = await _dl.compute_snapshot(overrides)
    except Exception as e:
        log.warning("[preflight] daily_loss_headroom failed: %s", e)
        return PreflightCheck(
            id="daily_loss_headroom",
            label="Daily-loss head-room",
            passed=False,
            hint="Cannot read daily-loss snapshot; check API logs.",
        )
    used_pcts: list[float] = []
    if snap.threshold_usd is not None and snap.threshold_usd > 0:
        used_pcts.append(100.0 * max(0.0, snap.loss_usd) / snap.threshold_usd)
    if snap.threshold_pct_usd is not None and snap.threshold_pct_usd > 0:
        used_pcts.append(100.0 * max(0.0, snap.loss_usd) / snap.threshold_pct_usd)
    if not used_pcts:
        # No threshold configured — head-room check is trivially satisfied.
        return PreflightCheck(
            id="daily_loss_headroom",
            label="Daily-loss head-room",
            passed=True,
            details={"used_pct": None, "max_used_pct": max_used},
        )
    used = round(max(used_pcts), 2)
    if used >= max_used:
        return PreflightCheck(
            id="daily_loss_headroom",
            label="Daily-loss head-room",
            passed=False,
            hint=(
                f"Daily-loss is {used:.1f}% of cap (>= {max_used:.0f}%). "
                f"Wait until tomorrow's reset before flipping live."
            ),
            details={"used_pct": used, "max_used_pct": max_used},
        )
    return PreflightCheck(
        id="daily_loss_headroom",
        label="Daily-loss head-room",
        passed=True,
        details={"used_pct": used, "max_used_pct": max_used},
    )


async def check_drift_status() -> PreflightCheck:
    """No vertical's most recent calibrator was rolled back recently."""
    try:
        row = await db.fetchrow(
            """
            SELECT vertical
              FROM calibration_snapshots
             WHERE COALESCE(drift_status->>'status', '') = 'rolled_back'
               AND created_at >= now() - interval '6 hours'
             ORDER BY created_at DESC
             LIMIT 1
            """
        )
    except Exception as e:
        log.warning("[preflight] drift_status query failed: %s", e)
        return PreflightCheck(
            id="drift_status",
            label="Calibrator drift status",
            passed=False,
            hint="DB query for drift status failed; check API logs.",
        )
    if row is not None:
        vertical = str(row["vertical"] or "?")
        return PreflightCheck(
            id="drift_status",
            label="Calibrator drift status",
            passed=False,
            hint=(
                f"Vertical '{vertical}' had a rolled-back calibrator in "
                f"the last 6h. Investigate drift before flipping live."
            ),
            details={"rolled_back_vertical": vertical},
        )
    return PreflightCheck(
        id="drift_status",
        label="Calibrator drift status",
        passed=True,
        details={"rolled_back_vertical": None},
    )


async def check_swarm_provider() -> PreflightCheck:
    """No primary LLM provider's fallback rate exceeds the ceiling."""
    max_rate = _swarm_provider_max_fallback_rate()
    try:
        from ..swarm.fallback_monitor import fetch_provider_stats
        stats = await fetch_provider_stats(window_minutes=30)
    except Exception as e:
        log.warning("[preflight] swarm_provider query failed: %s", e)
        return PreflightCheck(
            id="swarm_provider",
            label="Swarm provider health",
            passed=False,
            hint="Cannot read swarm fallback stats; check API logs.",
        )
    worst: dict[str, Any] | None = None
    for s in stats:
        rate = float(s.get("rate") or 0.0)
        if worst is None or rate > float(worst.get("rate") or 0.0):
            worst = s
    if worst is not None and float(worst.get("rate") or 0.0) >= max_rate:
        provider = str(worst.get("provider") or "?")
        rate = float(worst.get("rate") or 0.0)
        return PreflightCheck(
            id="swarm_provider",
            label="Swarm provider health",
            passed=False,
            hint=(
                f"Provider '{provider}' fallback rate is {rate:.1%} "
                f"(>= {max_rate:.1%}). Swarm is biased; resolve before live."
            ),
            details={"worst_provider": provider, "rate": round(rate, 4)},
        )
    return PreflightCheck(
        id="swarm_provider",
        label="Swarm provider health",
        passed=True,
        details={
            "worst_provider": (worst or {}).get("provider"),
            "rate": round(float((worst or {}).get("rate") or 0.0), 4),
        },
    )


async def check_market_cooldowns() -> PreflightCheck:
    """Watchdog has not put too many markets into cooldown."""
    ceiling = _market_cooldown_max_active()
    try:
        active = await db.fetchval(
            """
            SELECT count(*)::int
              FROM watchdog_market_cooldowns
             WHERE cooldown_until > now()
            """
        )
    except Exception as e:
        log.warning("[preflight] market_cooldowns query failed: %s", e)
        return PreflightCheck(
            id="market_cooldowns",
            label="Market cooldowns",
            passed=False,
            hint="DB query for market cooldowns failed; check API logs.",
        )
    n = int(active or 0)
    if n > ceiling:
        return PreflightCheck(
            id="market_cooldowns",
            label="Market cooldowns",
            passed=False,
            hint=(
                f"{n} markets are currently in watchdog cooldown "
                f"(ceiling {ceiling}). Investigate market-data health."
            ),
            details={"active": n, "ceiling": ceiling},
        )
    return PreflightCheck(
        id="market_cooldowns",
        label="Market cooldowns",
        passed=True,
        details={"active": n, "ceiling": ceiling},
    )


# ---------------------------------------------------------------------------
# Aggregate runner
# ---------------------------------------------------------------------------


# Order matters — we surface the first failure to the operator, so cheaper /
# more-actionable checks come first.  Kill-switch and bankroll-sanity are
# the most operator-friendly hints; drift / swarm health are more nuanced.
_CHECKS: list[Callable[[], Awaitable[PreflightCheck]]] = [
    check_trading_mode_target,
    check_kill_switch,
    check_bankroll_sanity,
    check_pipeline_health,
    check_calibrator_freshness,
    check_drift_status,
    check_daily_loss_headroom,
    check_swarm_provider,
    check_market_cooldowns,
]


async def run_preflight() -> PreflightResult:
    """Evaluate every check and return the aggregate verdict.

    Each predicate is awaited sequentially (not in a gather) so the
    DB doesn't see a thundering-herd of 9 concurrent queries on what
    is otherwise a low-traffic operator action.  The list is small and
    each query is cheap — total wall-clock should be well under a
    second even on a cold connection.
    """
    t0 = time.monotonic()
    results: list[PreflightCheck] = []
    for fn in _CHECKS:
        try:
            results.append(await fn())
        except Exception as e:  # pragma: no cover — defensive
            log.exception("[preflight] check %s raised", getattr(fn, "__name__", "?"))
            results.append(PreflightCheck(
                id=getattr(fn, "__name__", "?").replace("check_", ""),
                label="Pre-flight check",
                passed=False,
                hint=f"Check raised: {e}",
            ))
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "preflight ran %d checks in %dms; passed=%s",
        len(results),
        elapsed_ms,
        all(c.passed for c in results),
    )
    return PreflightResult(
        passed=all(c.passed for c in results),
        checks=results,
    )
