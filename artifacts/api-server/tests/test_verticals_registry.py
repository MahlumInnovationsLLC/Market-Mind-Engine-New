"""Regression tests for the pluggable verticals registry.

The registry replaces the hardcoded `VERTICAL_LEAGUES` dict that used
to live in `app.orchestrator.cycle`.  These tests pin the previously
hardcoded behaviour so future verticals can be added without breaking
the sports / weather / crypto / crypto-binary / politics paths.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace as _dc_replace
from typing import Any

import pytest

from app import dashboard, verticals
from app.config import get_settings
from app.orchestrator import cycle as cycle_mod
import importlib

scheduler_mod = importlib.import_module("app.orchestrator.scheduler")
OrchestratorScheduler = scheduler_mod._CycleScheduler
from app.risk.gates import RiskContext, gate_duplicate_trade, gate_per_vertical_cap


def _ctx(vertical: str, **overrides: Any) -> RiskContext:
    base = dict(
        market_id="KXTEST-1",
        side="YES",
        vertical=vertical,
        league=None,
        market_type="moneyline",
        matchup=None,
        current_price=0.5,
        model_probability=0.6,
        edge=0.1,
        confidence_band_low=0.45,
        confidence_band_high=0.55,
        agreement_rate=0.83,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=10,
        intended_notional_usd=5.0,
        bankroll_usd=1000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )
    base.update(overrides)
    return RiskContext(**base)


def test_builtins_match_legacy_vertical_leagues() -> None:
    """Every vertical that the old `VERTICAL_LEAGUES` dict mapped is
    registered with the same league filter."""
    expected = {
        "sports": [],
        "weather": ["WEATHER"],
        "crypto": ["CRYPTO"],
        "crypto-binary": ["CRYPTO"],
        "politics": ["POLITICS"],
    }
    for name, leagues in expected.items():
        assert verticals.has(name), f"{name} not registered"
        assert verticals.league_filter(name) == leagues
        # `_league_filter` in cycle.py must mirror the registry so any
        # call site that still imports it keeps the legacy behaviour.
        assert cycle_mod._league_filter(name) == leagues


def test_unknown_vertical_returns_empty_filter() -> None:
    """Mirrors the previous dict.get(..., []) behaviour."""
    assert verticals.league_filter("does-not-exist") == []
    assert cycle_mod._league_filter("does-not-exist") == []


def test_dashboard_strip_preserves_legacy_order() -> None:
    """`get_vertical_performance` used to iterate
    ``["sports", "weather", "crypto"]``.  The registry must still
    produce that exact ordering so the operator strip doesn't shuffle.
    """
    assert verticals.dashboard_strip_verticals() == ["sports", "weather", "crypto"]


def test_crypto_binary_routes_through_specialist_predictor_hook() -> None:
    spec = verticals.require_spec("crypto-binary")
    assert spec.predictor_hook == "crypto_binary"
    # Spot-crypto stays on the generic meta-model (no hook).
    assert verticals.require_spec("crypto").predictor_hook is None
    assert verticals.require_spec("sports").predictor_hook is None


def test_duplicate_trade_exempt_only_for_spot_crypto() -> None:
    """Pre-registry behaviour: `gate_duplicate_trade` short-circuited
    only for `vertical == 'crypto'`.  Verify the registry-driven path
    keeps that exact set."""
    crypto_result = asyncio.run(gate_duplicate_trade(_ctx("crypto")))
    assert crypto_result.passed is True
    assert "exempt" in crypto_result.reason

    # crypto-binary used to NOT be exempt because the production
    # `idx_trades_unique_daily` index was checked.  Preserve that.
    spec = verticals.require_spec("crypto-binary")
    assert spec.duplicate_trade_exempt is False


def test_per_vertical_cap_uses_spec_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no operator override is set, the gate uses the registered
    default allocation. Default for sports remains 0.30 (legacy)."""
    ctx = _ctx("sports", intended_notional_usd=400.0)
    result = asyncio.run(gate_per_vertical_cap(ctx))
    # sports default is 0.30 → cap_usd = $300; intended $400 → reject.
    assert result.passed is False
    assert pytest.approx(result.details["cap_pct"], 1e-6) == 0.30


def test_register_new_vertical_then_unregister() -> None:
    """A new vertical can be registered, queried, and removed without
    interfering with the built-ins."""
    spec = verticals.VerticalSpec(
        name="commodities-test",
        leagues=("OIL",),
        kelly_multiplier=0.75,
        default_allocation_pct=0.10,
        include_in_dashboard_strip=False,
    )
    verticals.REGISTRY.register(spec)
    try:
        assert verticals.has("commodities-test")
        assert verticals.league_filter("Commodities-Test") == ["OIL"]
        # Built-ins still intact.
        assert verticals.has("sports")
        assert verticals.dashboard_strip_verticals() == [
            "sports", "weather", "crypto"
        ]
    finally:
        verticals.REGISTRY.unregister("commodities-test")
    assert not verticals.has("commodities-test")


def test_double_register_raises_unless_replace() -> None:
    spec = verticals.VerticalSpec(name="dup-test")
    verticals.REGISTRY.register(spec)
    try:
        with pytest.raises(ValueError):
            verticals.REGISTRY.register(spec)
        # `replace=True` allows hot-swapping.
        spec2 = _dc_replace(spec, description="updated")
        verticals.REGISTRY.register(spec2, replace=True)
        assert verticals.require_spec("dup-test").description == "updated"
    finally:
        verticals.REGISTRY.unregister("dup-test")


def test_overview_includes_registered_verticals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/api/dashboard/overview` should expose the registry."""

    class _Stub:
        async def fetchval(self, *_a, **_kw):
            return None

        async def fetchrow(self, *_a, **_kw):
            return None

        async def fetch(self, *_a, **_kw):
            return []

    monkeypatch.setattr(dashboard, "db", _Stub())
    out = asyncio.run(dashboard.get_overview())
    names = [v["name"] for v in out["verticals"]]
    # Built-ins all present; arbitrary ordering tolerated, but the
    # legacy three must appear.
    for legacy in ("sports", "weather", "crypto", "crypto-binary", "politics"):
        assert legacy in names
    sports = next(v for v in out["verticals"] if v["name"] == "sports")
    assert "leagues" in sports
    assert "persona_profile" in sports
    assert "default_allocation_pct" in sports


def test_scheduler_skips_disabled_vertical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `AXIOM_ORCHESTRATOR_VERTICALS` lists a vertical whose
    registry spec has `enabled=False`, the scheduler must NOT spawn a
    loop task for it."""
    spec = verticals.VerticalSpec(
        name="sched-disabled-test",
        leagues=("X",),
        enabled=False,
    )
    verticals.REGISTRY.register(spec)
    try:
        # Bypass config.py validation by injecting a stub settings.
        class _S:
            orchestrator_enabled = True
            orchestrator_verticals = ["sports", "sched-disabled-test"]
            cycle_interval_seconds = 120
            trading_mode = "paper"

        monkeypatch.setattr(scheduler_mod, "get_settings", lambda: _S())

        sched = OrchestratorScheduler()

        async def go() -> None:
            await sched.start()
            try:
                assert "sports" in sched._tasks
                assert "sched-disabled-test" not in sched._tasks
            finally:
                await sched.stop()

        asyncio.run(go())
    finally:
        verticals.REGISTRY.unregister("sched-disabled-test")


def test_scheduler_status_exposes_registered_verticals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/api/orchestrator/status` consumers (web + mobile dashboards)
    rely on `registered_verticals` being part of the status payload."""

    class _S:
        orchestrator_enabled = False
        orchestrator_verticals: list[str] = []
        cycle_interval_seconds = 120
        trading_mode = "paper"

    monkeypatch.setattr(scheduler_mod, "get_settings", lambda: _S())
    sched = OrchestratorScheduler()
    status = sched.status()
    names = [v["name"] for v in status["registered_verticals"]]
    for legacy in ("sports", "weather", "crypto", "crypto-binary", "politics"):
        assert legacy in names
