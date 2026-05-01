"""Tests for the 9-point /api/orchestrator/live-execute pre-flight
(Task #752 step 4 — Round-11 audit Bug #7).

Coverage matrix:

* The 9 individual predicates each pass on a healthy stub and fail with
  the right ``id`` + ``hint`` on a degraded stub.
* The aggregate :func:`run_preflight` returns ``passed=True`` only when
  all 9 pass; otherwise ``first_failure`` points at the first failed
  predicate in execution order.
* The route returns 409 with ``{failed_check, hint, checks}`` on a
  failed pre-flight and persists nothing.
* The route returns 200 + persists when pre-flight passes.
* Paper-flips skip pre-flight entirely (you can always rollback).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import billing
from app.orchestrator import preflight


# ---------------------------------------------------------------------------
# Helpers — patch every DB / module hook the predicates touch with stubs
# that, by default, return "everything healthy".
# ---------------------------------------------------------------------------


class _HealthyDB:
    """Stub DB that returns the rosiest plausible answer to every
    pre-flight query.  Tests then override individual responses to
    simulate one degraded signal at a time."""

    def __init__(self) -> None:
        # Per-test overrides — keys map onto the predicate id.
        self.calibrator_age_hours: float | None = 2.5
        self.calibrator_vertical: str = "sports"
        self.cycle_age_seconds: float | None = 60.0  # 1 minute ago
        self.bankroll_cents: int | None = 50_000     # $500
        self.rolled_back_vertical: str | None = None
        self.cooldown_count: int = 0

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM calibration_snapshots" in q and "rolled_back" in q:
            if self.rolled_back_vertical is None:
                return None
            return {"vertical": self.rolled_back_vertical}
        if "FROM calibration_snapshots" in q and "retrained" in q:
            if self.calibrator_age_hours is None:
                return None
            return {
                "vertical": self.calibrator_vertical,
                "age_hours": self.calibrator_age_hours,
            }
        return None

    async def fetchval(self, query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM trading_cycles" in q:
            return self.cycle_age_seconds
        if "FROM bankroll_events" in q:
            return self.bankroll_cents
        if "FROM watchdog_market_cooldowns" in q:
            return self.cooldown_count
        return None


@pytest.fixture
def healthy_stubs(monkeypatch):
    """Patch every external dependency the predicates reach for to a
    healthy default.  Returns the mutable :class:`_HealthyDB` so the
    test can flip one signal degraded.
    """
    fake = _HealthyDB()
    monkeypatch.setattr(preflight.db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(preflight.db, "fetchval", fake.fetchval)

    # Kill switch — healthy by default.  Patch at the module level so
    # ``preflight.check_kill_switch`` (which does a fresh ``from .. import
    # exchange_kill_switch as _eks`` inside the function) sees the stub.
    from app import exchange_kill_switch as _eks
    fake._kill_switch_state: dict[str, Any] = {  # type: ignore[attr-defined]
        "halted": False, "reason": None, "since": None,
    }

    def _fake_state() -> dict[str, Any]:
        return dict(fake._kill_switch_state)  # type: ignore[attr-defined]

    monkeypatch.setattr(_eks, "kill_switch_state", _fake_state)

    # Daily-loss snapshot — under any threshold.
    from app.risk import daily_loss as _dl

    class _FakeSnapshot:
        loss_usd = 5.0
        threshold_usd = 100.0
        threshold_pct_usd = 100.0

    async def _fake_compute_snapshot(_overrides):
        return _FakeSnapshot()

    async def _fake_load_overrides():
        return {}

    monkeypatch.setattr(_dl, "compute_snapshot", _fake_compute_snapshot)
    from app.orchestrator import cycle as _cycle
    monkeypatch.setattr(_cycle, "_load_operator_overrides", _fake_load_overrides)

    # Swarm provider stats — empty list == no inflight problem.
    from app.swarm import fallback_monitor as _fm

    async def _fake_provider_stats(window_minutes: int):
        return []

    monkeypatch.setattr(_fm, "fetch_provider_stats", _fake_provider_stats)

    return fake


# ---------------------------------------------------------------------------
# Aggregate run — every predicate green => passed=True
# ---------------------------------------------------------------------------


def test_run_preflight_all_green(healthy_stubs):
    result = asyncio.run(preflight.run_preflight())
    assert result.passed is True
    assert result.first_failure is None
    assert len(result.checks) == 9
    ids = [c.id for c in result.checks]
    # Order is the operator-friendly order defined in _CHECKS.
    assert ids[:2] == ["trading_mode_target", "kill_switch"]
    assert "calibrator_freshness" in ids
    assert "drift_status" in ids
    assert "swarm_provider" in ids
    assert "market_cooldowns" in ids


# ---------------------------------------------------------------------------
# Per-predicate failures — each test flips exactly one signal.
# ---------------------------------------------------------------------------


def test_calibrator_freshness_fails_when_no_row(healthy_stubs):
    healthy_stubs.calibrator_age_hours = None
    check = asyncio.run(preflight.check_calibrator_freshness())
    assert check.passed is False
    assert check.id == "calibrator_freshness"
    assert "retrain" in check.hint.lower()


def test_calibrator_freshness_fails_when_stale(healthy_stubs, monkeypatch):
    monkeypatch.setenv("AXIOM_PREFLIGHT_CALIBRATOR_MAX_HOURS", "6")
    healthy_stubs.calibrator_age_hours = 24.0
    check = asyncio.run(preflight.check_calibrator_freshness())
    assert check.passed is False
    assert "stale" not in (check.hint or "")  # we say "old", not "stale"
    assert "24" in (check.hint or "")


def test_calibrator_freshness_passes_when_fresh(healthy_stubs):
    healthy_stubs.calibrator_age_hours = 0.5
    check = asyncio.run(preflight.check_calibrator_freshness())
    assert check.passed is True


def test_pipeline_health_fails_when_no_cycles(healthy_stubs):
    healthy_stubs.cycle_age_seconds = None
    check = asyncio.run(preflight.check_pipeline_health())
    assert check.passed is False
    assert check.id == "pipeline_health"
    assert "scheduler" in (check.hint or "").lower()


def test_pipeline_health_fails_when_stale(healthy_stubs, monkeypatch):
    monkeypatch.setenv("AXIOM_PREFLIGHT_CYCLE_MAX_MINUTES", "5")
    healthy_stubs.cycle_age_seconds = 60.0 * 30  # 30 minutes ago
    check = asyncio.run(preflight.check_pipeline_health())
    assert check.passed is False


def test_pipeline_health_passes_when_recent(healthy_stubs):
    healthy_stubs.cycle_age_seconds = 30.0
    check = asyncio.run(preflight.check_pipeline_health())
    assert check.passed is True


def test_bankroll_sanity_fails_when_no_observation(healthy_stubs):
    healthy_stubs.bankroll_cents = None
    check = asyncio.run(preflight.check_bankroll_sanity())
    assert check.passed is False
    assert check.id == "bankroll_sanity"
    assert "bankroll_sync" in (check.hint or "")


def test_bankroll_sanity_fails_below_floor(healthy_stubs, monkeypatch):
    monkeypatch.setenv("AXIOM_PREFLIGHT_BANKROLL_FLOOR_USD", "100")
    healthy_stubs.bankroll_cents = 5_000  # $50
    check = asyncio.run(preflight.check_bankroll_sanity())
    assert check.passed is False
    assert "$50" in (check.hint or "") or "50.00" in (check.hint or "")


def test_bankroll_sanity_passes_above_floor(healthy_stubs):
    healthy_stubs.bankroll_cents = 100_000  # $1,000
    check = asyncio.run(preflight.check_bankroll_sanity())
    assert check.passed is True


def test_kill_switch_fails_when_tripped(healthy_stubs):
    healthy_stubs._kill_switch_state = {
        "halted": True, "reason": "operator", "since": "2026-05-01T00:00:00Z",
    }
    check = asyncio.run(preflight.check_kill_switch())
    assert check.passed is False
    assert check.id == "kill_switch"
    assert "TRIPPED" in (check.hint or "")


def test_kill_switch_passes_when_clear(healthy_stubs):
    check = asyncio.run(preflight.check_kill_switch())
    assert check.passed is True


def test_trading_mode_target_always_passes():
    """It's a marker check that documents intent — once we hit
    ``run_preflight`` we *are* asking for live."""
    check = asyncio.run(preflight.check_trading_mode_target())
    assert check.passed is True
    assert check.details["requested_mode"] == "live"


def test_daily_loss_headroom_fails_when_near_cap(healthy_stubs, monkeypatch):
    monkeypatch.setenv("AXIOM_PREFLIGHT_DAILY_LOSS_MAX_USED_PCT", "90")
    from app.risk import daily_loss as _dl

    class _NearCap:
        loss_usd = 95.0
        threshold_usd = 100.0
        threshold_pct_usd = 100.0

    async def _stub(_overrides):
        return _NearCap()

    monkeypatch.setattr(_dl, "compute_snapshot", _stub)
    check = asyncio.run(preflight.check_daily_loss_headroom())
    assert check.passed is False
    assert check.id == "daily_loss_headroom"


def test_daily_loss_headroom_passes_with_room(healthy_stubs):
    check = asyncio.run(preflight.check_daily_loss_headroom())
    assert check.passed is True


def test_daily_loss_headroom_passes_when_no_threshold(healthy_stubs, monkeypatch):
    """No configured threshold == operator-acknowledged risk; the
    head-room check is trivially satisfied (we still fail fast on a
    real budget breach via the daily-loss gate at runtime)."""
    from app.risk import daily_loss as _dl

    class _NoThresh:
        loss_usd = 50.0
        threshold_usd = None
        threshold_pct_usd = None

    async def _stub(_overrides):
        return _NoThresh()

    monkeypatch.setattr(_dl, "compute_snapshot", _stub)
    check = asyncio.run(preflight.check_daily_loss_headroom())
    assert check.passed is True


def test_drift_status_fails_when_recent_rollback(healthy_stubs):
    healthy_stubs.rolled_back_vertical = "sports"
    check = asyncio.run(preflight.check_drift_status())
    assert check.passed is False
    assert check.id == "drift_status"
    assert "sports" in (check.hint or "")


def test_drift_status_passes_when_clean(healthy_stubs):
    check = asyncio.run(preflight.check_drift_status())
    assert check.passed is True


def test_swarm_provider_fails_above_ceiling(healthy_stubs, monkeypatch):
    monkeypatch.setenv("AXIOM_PREFLIGHT_SWARM_FALLBACK_MAX_RATE", "0.30")
    from app.swarm import fallback_monitor as _fm

    async def _stub(window_minutes: int):
        return [
            {"provider": "openai", "total": 100, "fallback": 50, "rate": 0.5},
            {"provider": "anthropic", "total": 100, "fallback": 5, "rate": 0.05},
        ]

    monkeypatch.setattr(_fm, "fetch_provider_stats", _stub)
    check = asyncio.run(preflight.check_swarm_provider())
    assert check.passed is False
    assert check.id == "swarm_provider"
    assert "openai" in (check.hint or "")


def test_swarm_provider_passes_below_ceiling(healthy_stubs, monkeypatch):
    from app.swarm import fallback_monitor as _fm

    async def _stub(window_minutes: int):
        return [
            {"provider": "openai", "total": 100, "fallback": 5, "rate": 0.05},
        ]

    monkeypatch.setattr(_fm, "fetch_provider_stats", _stub)
    check = asyncio.run(preflight.check_swarm_provider())
    assert check.passed is True


def test_market_cooldowns_fails_when_too_many(healthy_stubs, monkeypatch):
    monkeypatch.setenv("AXIOM_PREFLIGHT_MARKET_COOLDOWN_MAX_ACTIVE", "10")
    healthy_stubs.cooldown_count = 50
    check = asyncio.run(preflight.check_market_cooldowns())
    assert check.passed is False
    assert check.id == "market_cooldowns"


def test_market_cooldowns_passes_when_calm(healthy_stubs):
    check = asyncio.run(preflight.check_market_cooldowns())
    assert check.passed is True


# ---------------------------------------------------------------------------
# Aggregate — first_failure points at the first failed predicate in order.
# ---------------------------------------------------------------------------


def test_run_preflight_first_failure_is_kill_switch(healthy_stubs):
    """Kill switch is checked early, so when it AND something else
    are both failed, the kill switch is what surfaces to the operator."""
    healthy_stubs._kill_switch_state = {
        "halted": True, "reason": "operator", "since": None,
    }
    healthy_stubs.calibrator_age_hours = None  # also degraded
    result = asyncio.run(preflight.run_preflight())
    assert result.passed is False
    assert result.first_failure is not None
    assert result.first_failure.id == "kill_switch"


def test_run_preflight_serializes_to_dict(healthy_stubs):
    result = asyncio.run(preflight.run_preflight())
    d = result.to_dict()
    assert d["passed"] is True
    assert isinstance(d["checks"], list)
    assert all(set(c.keys()) == {"id", "label", "passed", "hint", "details"}
               for c in d["checks"])


# ---------------------------------------------------------------------------
# Env-var parsing edge cases
# ---------------------------------------------------------------------------


def test_env_float_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("AXIOM_PREFLIGHT_CALIBRATOR_MAX_HOURS", "garbage")
    assert preflight._calibrator_max_age_hours() == 24.0


def test_env_float_falls_back_on_negative(monkeypatch):
    monkeypatch.setenv("AXIOM_PREFLIGHT_CALIBRATOR_MAX_HOURS", "-5")
    assert preflight._calibrator_max_age_hours() == 24.0


def test_env_int_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("AXIOM_PREFLIGHT_MARKET_COOLDOWN_MAX_ACTIVE", "abc")
    assert preflight._market_cooldown_max_active() == 25


# ---------------------------------------------------------------------------
# Route-level integration: 409 on failure, 200 on success, paper unrestricted
# ---------------------------------------------------------------------------


# Re-use the helper from the gating tests so the UserSubscription
# signature stays consistent.
from tests.test_pro_elite_gating import _sub  # type: ignore  # noqa: E402


@pytest.fixture
def route_stubs(monkeypatch):
    """Patch the system_settings reads/writes the route does on success.

    ``executes`` records only the trading_mode flips; canary seed
    writes go to ``canary_executes`` so the live-execute assertions
    aren't polluted by step-5 wiring.
    """
    state: dict[str, Any] = {
        "executes": [],
        "value": None,
        "canary_executes": [],
        "canary_remaining": None,
    }

    async def _fake_execute(query: str, *args: Any):
        if "system_settings" not in query:
            raise AssertionError(f"unexpected execute: {query!r}")
        if args and args[0] in ("paper", "live", "halt"):
            state["value"] = args[0]
            state["executes"].append(args[0])
        else:
            state["canary_executes"].append(args)
            if args and args[0] == "risk.canary_remaining" and len(args) >= 2:
                state["canary_remaining"] = args[1]
        return "OK"

    async def _fake_fetchval(query: str, *args: Any):
        if "system_settings" in query and "risk.trading_mode" in query:
            return state["value"]
        if (
            "system_settings" in query
            and "WHERE key" in query
            and args
            and args[0] == "risk.canary_remaining"
        ):
            return state["canary_remaining"]
        return None

    from app import db as db_module
    monkeypatch.setattr(db_module, "fetchval", _fake_fetchval)
    monkeypatch.setattr(db_module, "execute", _fake_execute)
    return state


@pytest.fixture
def elite_client(monkeypatch, override_auth, patch_subscription, route_stubs):
    from fastapi.testclient import TestClient
    from app.main import app
    patch_subscription["value"] = _sub(billing.TIER_ELITE)
    return TestClient(app), route_stubs


# Helpful imports already used by the gating tests — re-imported for the
# fixtures we depend on.
from tests.test_pro_elite_gating import (  # type: ignore  # noqa: E402
    override_auth,  # noqa: F401
    patch_subscription,  # noqa: F401
)


def test_route_returns_409_when_preflight_fails(elite_client, monkeypatch):
    client, state = elite_client

    from app.orchestrator import preflight as _preflight

    async def _failing():
        return _preflight.PreflightResult(
            passed=False,
            checks=[
                _preflight.PreflightCheck(
                    id="kill_switch",
                    label="Kill switch",
                    passed=False,
                    hint="Kill switch is TRIPPED — clear it first.",
                ),
                _preflight.PreflightCheck(
                    id="bankroll_sanity",
                    label="Live bankroll sanity",
                    passed=True,
                ),
            ],
        )

    monkeypatch.setattr(_preflight, "run_preflight", _failing)

    resp = client.post(
        "/api/orchestrator/live-execute", json={"enabled": True}
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"]["code"] == "preflight_failed"
    assert body["detail"]["failed_check"] == "kill_switch"
    assert "TRIPPED" in body["detail"]["hint"]
    assert isinstance(body["detail"]["checks"], list)
    assert len(body["detail"]["checks"]) == 2
    # No DB write happened.
    assert state["executes"] == []


def test_route_returns_200_when_preflight_passes(elite_client, monkeypatch):
    client, state = elite_client

    from app.orchestrator import preflight as _preflight

    async def _passing():
        return _preflight.PreflightResult(passed=True, checks=[])

    monkeypatch.setattr(_preflight, "run_preflight", _passing)

    resp = client.post(
        "/api/orchestrator/live-execute", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text
    assert state["executes"] == ["live"]


def test_route_skips_preflight_when_flipping_back_to_paper(elite_client, monkeypatch):
    """Going back to paper is unconditional — never gated by pre-flight.
    This is critical: the pre-flight is the safety belt for *going live*,
    not for going home."""
    client, state = elite_client

    called = {"n": 0}

    from app.orchestrator import preflight as _preflight

    async def _should_not_be_called():
        called["n"] += 1
        return _preflight.PreflightResult(passed=False, checks=[])

    monkeypatch.setattr(_preflight, "run_preflight", _should_not_be_called)

    resp = client.post(
        "/api/orchestrator/live-execute", json={"enabled": False}
    )
    assert resp.status_code == 200, resp.text
    assert called["n"] == 0
    assert state["executes"] == ["paper"]
