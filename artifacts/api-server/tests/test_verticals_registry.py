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
from app.risk.gates import (
    RiskContext,
    gate_duplicate_trade,
    gate_kelly_fraction,
    gate_per_vertical_cap,
)


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


def test_kelly_multiplier_default_preserves_legacy_behaviour() -> None:
    """All built-in verticals use kelly_multiplier=1.0, so the legacy
    notional cap (`bankroll * kelly * fraction_cap`) must be unchanged."""
    for v in ("sports", "weather", "crypto", "crypto-binary", "politics"):
        assert verticals.require_spec(v).kelly_multiplier == 1.0
    ctx = _ctx("sports", model_probability=0.7, current_price=0.5,
               intended_notional_usd=1.0, bankroll_usd=1000.0)
    res = asyncio.run(gate_kelly_fraction(ctx))
    assert res.passed is True
    # multiplier reported as 1.0 (legacy parity).
    assert res.details["kelly_multiplier"] == pytest.approx(1.0)


def test_kelly_multiplier_from_spec_tightens_cap() -> None:
    """A vertical with `kelly_multiplier=0.5` must produce a notional
    cap that is exactly half of the equivalent kelly_multiplier=1.0
    vertical. Operator override is left unset."""
    spec = verticals.VerticalSpec(
        name="kelly-half-test",
        kelly_multiplier=0.5,
    )
    verticals.REGISTRY.register(spec)
    try:
        ctx_full = _ctx("sports", model_probability=0.7, current_price=0.5,
                        intended_notional_usd=1.0, bankroll_usd=1000.0)
        ctx_half = _ctx("kelly-half-test", model_probability=0.7,
                        current_price=0.5, intended_notional_usd=1.0,
                        bankroll_usd=1000.0)
        res_full = asyncio.run(gate_kelly_fraction(ctx_full))
        res_half = asyncio.run(gate_kelly_fraction(ctx_half))
        assert res_half.details["max_notional"] == pytest.approx(
            res_full.details["max_notional"] * 0.5
        )
        assert res_half.details["kelly_multiplier"] == pytest.approx(0.5)
    finally:
        verticals.REGISTRY.unregister("kelly-half-test")


def test_kelly_multiplier_operator_override_wins() -> None:
    """Per the existing override convention, an operator-set
    `kelly_multiplier` must beat the spec value."""
    spec = verticals.VerticalSpec(name="kelly-ov-test", kelly_multiplier=0.5)
    verticals.REGISTRY.register(spec)
    try:
        ctx = _ctx(
            "kelly-ov-test",
            model_probability=0.7,
            current_price=0.5,
            intended_notional_usd=1.0,
            bankroll_usd=1000.0,
            operator_overrides={"kelly_multiplier": 2.0},
        )
        res = asyncio.run(gate_kelly_fraction(ctx))
        assert res.details["kelly_multiplier"] == pytest.approx(2.0)
    finally:
        verticals.REGISTRY.unregister("kelly-ov-test")


def test_feature_builder_hook_field_present() -> None:
    """`feature_builder_hook` is part of the spec contract so future
    cycle feature-construction code can dispatch on it."""
    for v in verticals.all_specs():
        # Default for built-ins is None (generic builder).
        assert hasattr(v, "feature_builder_hook")
    spec = verticals.VerticalSpec(
        name="fb-hook-test",
        feature_builder_hook="weather_grid_v1",
    )
    verticals.REGISTRY.register(spec)
    try:
        assert verticals.require_spec("fb-hook-test").feature_builder_hook == (
            "weather_grid_v1"
        )
        # to_dict must round-trip the new field for /api/health consumers.
        assert verticals.require_spec("fb-hook-test").to_dict()[
            "feature_builder_hook"
        ] == "weather_grid_v1"
    finally:
        verticals.REGISTRY.unregister("fb-hook-test")


def test_swarm_runner_resolves_persona_profile_from_registry() -> None:
    """`_run_debate_inner` reads `candidate['vertical']` and looks up
    the persona profile via the registry; we verify the registry-side
    half here without spinning up the full LLM stack."""
    spec = verticals.VerticalSpec(
        name="profile-test", persona_profile="commodities_v1"
    )
    verticals.REGISTRY.register(spec)
    try:
        assert verticals.persona_profile_for("profile-test") == "commodities_v1"
        # Unknown vertical falls back to the legacy "default" profile.
        assert verticals.persona_profile_for("totally-unknown") == "default"
    finally:
        verticals.REGISTRY.unregister("profile-test")


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
