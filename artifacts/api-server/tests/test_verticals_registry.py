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


# ---------------------------------------------------------------------------
# Frozen baseline of every built-in vertical's full spec serialization.
#
# This is the "byte-identical regression lock" for the registry foundation:
# any change to a built-in spec — kelly_multiplier, default_allocation_pct,
# leagues, predictor_hook, duplicate_trade_exempt, persona_profile,
# include_in_dashboard_strip, etc. — must update this fixture *and* be
# justified in code review.  This guards the legacy production behaviour
# of the sports / weather / crypto / crypto-binary / politics paths.
# ---------------------------------------------------------------------------
_BASELINE_BUILTINS: dict[str, dict[str, Any]] = {
    "sports": {
        "name": "sports",
        "leagues": [],
        "ticker_prefixes": [
            "KXNFL", "KXNBA", "KXMLB", "KXNHL",
            "KXMLS", "KXEPL", "KXNCAAF", "KXNCAAB",
        ],
        "persona_profile": "sports",
        "kelly_multiplier": 1.0,
        "default_allocation_pct": 0.30,
        "blackout_windows": [],
        "predictor_hook": None,
        "feature_builder_hook": None,
        "duplicate_trade_exempt": False,
        "include_in_dashboard_strip": True,
        "enabled": True,
    },
    "weather": {
        "name": "weather",
        "leagues": ["WEATHER"],
        "ticker_prefixes": ["KXHIGH", "KXTEMP", "KXLOW", "KXRAIN"],
        "persona_profile": "weather",
        "kelly_multiplier": 1.0,
        "default_allocation_pct": 0.30,
        # Temperature contracts route through the GRU + EMOS hook
        # registered in task #246.  Pinned here so any future change
        # to the wiring trips the baseline-drift guard.
        "predictor_hook": "weather_temp_gru_emos",
        "feature_builder_hook": None,
        "duplicate_trade_exempt": False,
        "include_in_dashboard_strip": True,
        "enabled": True,
    },
    "crypto": {
        "name": "crypto",
        "leagues": ["CRYPTO"],
        "ticker_prefixes": ["KXBTC", "KXETH"],
        "persona_profile": "crypto",
        "kelly_multiplier": 1.0,
        "default_allocation_pct": 0.30,
        "predictor_hook": None,
        "feature_builder_hook": None,
        "duplicate_trade_exempt": True,
        "include_in_dashboard_strip": True,
        "enabled": True,
    },
    "crypto-binary": {
        "name": "crypto-binary",
        "leagues": ["CRYPTO"],
        "ticker_prefixes": ["KXBTC", "KXETH"],
        "persona_profile": "crypto",
        "kelly_multiplier": 1.0,
        "default_allocation_pct": 0.30,
        "predictor_hook": "crypto_binary",
        "feature_builder_hook": None,
        "duplicate_trade_exempt": False,
        "enabled": True,
    },
    "politics": {
        "name": "politics",
        "leagues": ["POLITICS"],
        "persona_profile": "politics",
        "kelly_multiplier": 1.0,
        "default_allocation_pct": 0.30,
        "predictor_hook": None,
        "feature_builder_hook": None,
        "duplicate_trade_exempt": False,
        "enabled": True,
    },
}


def test_builtin_spec_baseline_frozen() -> None:
    """Every key listed in `_BASELINE_BUILTINS` must equal the live
    spec's `to_dict()` output for that field. New fields can be added
    to the spec without updating this fixture, but no listed value may
    drift silently."""
    for name, expected in _BASELINE_BUILTINS.items():
        live = verticals.require_spec(name).to_dict()
        for key, want in expected.items():
            assert live[key] == want, (
                f"baseline drift on built-in {name!r}.{key}: "
                f"expected {want!r}, got {live[key]!r}"
            )


def test_builtin_registration_order_frozen() -> None:
    """The registration order of the built-ins is observable through
    `dashboard_strip_verticals()` and `_registered_verticals_snapshot()`;
    pin it so dashboard ordering can never silently rearrange."""
    names = [s.name for s in verticals.all_specs()]
    # Built-ins must appear in this exact relative order; later
    # registrations from #194+ may append after `politics`.
    expected_prefix = ["sports", "weather", "crypto", "crypto-binary", "politics"]
    assert names[: len(expected_prefix)] == expected_prefix


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
    ``["sports", "weather", "crypto"]``.  The legacy three must keep
    that exact ordering so the operator strip doesn't shuffle.  Task
    #196 appended ``soccer`` after the original three (registered in
    insertion order in `app/verticals/builtins.py`).
    """
    strip = verticals.dashboard_strip_verticals()
    assert strip[:3] == ["sports", "weather", "crypto"], (
        "legacy three must remain in original order"
    )
    assert "soccer" in strip, "soccer vertical should appear on the strip"
    assert strip.index("soccer") > strip.index("crypto"), (
        "soccer must come after the legacy three (insertion order)"
    )
    # Task #215 promoted combat-individual onto the strip alongside
    # soccer; both must appear after the legacy three.
    assert "combat-individual" in strip, (
        "combat-individual vertical should appear on the strip"
    )
    assert strip.index("combat-individual") > strip.index("crypto"), (
        "combat-individual must come after the legacy three (insertion order)"
    )
    # Task #218 promoted the `commodities` vertical onto the strip so
    # the operator dashboard shows live commodities trade count + P&L
    # alongside the other front-of-book verticals.  In
    # `app/verticals/builtins.py` commodities is registered after
    # the legacy three but before the later combat-individual / soccer
    # block, so its position on the strip falls between them.
    assert "commodities" in strip, (
        "commodities vertical should appear on the strip after task #218"
    )
    assert strip.index("commodities") > strip.index("crypto"), (
        "commodities must come after the legacy three (insertion order)"
    )


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
        # Legacy strip is now
        # ["sports","weather","crypto","soccer","combat-individual"]
        # after tasks #196 / #215 promoted the soccer and
        # combat-individual verticals.  The commodities-test fixture
        # above explicitly opts out of the strip, so it must NOT
        # appear here.
        strip = verticals.dashboard_strip_verticals()
        assert strip[:3] == ["sports", "weather", "crypto"]
        assert "soccer" in strip
        assert "combat-individual" in strip
        assert "commodities-test" not in strip
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


def test_candidate_predictions_sql_parity_per_vertical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end regression lock: `_candidate_predictions` must use
    the exact league filter the legacy hardcoded `VERTICAL_LEAGUES`
    dict produced for each live vertical. We exercise the cycle-level
    code path with a stubbed db.fetch and capture the SQL parameters
    so any future drift fails this test."""
    captured: list[tuple[str, tuple[Any, ...]]] = []

    class _StubDB:
        async def fetch(self, sql: str, *args: Any) -> list[Any]:
            captured.append((sql, args))
            return []

    monkeypatch.setattr(cycle_mod, "db", _StubDB())

    expected = {
        "sports": None,            # legacy: no league filter parameter
        "weather": ["WEATHER"],
        "crypto": ["CRYPTO"],
        "crypto-binary": ["CRYPTO"],
        "politics": ["POLITICS"],
    }
    for vertical, want_leagues in expected.items():
        captured.clear()
        asyncio.run(cycle_mod._candidate_predictions(vertical, limit=5))
        assert captured, f"{vertical}: db.fetch was never called"
        sql, args = captured[0]
        if want_leagues is None:
            # Sports: no `league = ANY($2)` clause; only $1 = limit.
            assert "league = ANY" not in sql, f"{vertical}: unexpected league filter"
            assert args == (5,), (
                f"{vertical}: expected (limit,) got {args!r}"
            )
        else:
            assert "league = ANY($2)" in sql, (
                f"{vertical}: expected league filter clause"
            )
            assert args == (5, want_leagues), (
                f"{vertical}: expected (limit, {want_leagues!r}) got {args!r}"
            )


def test_predictor_routing_per_vertical_baseline() -> None:
    """`crypto-binary` must route through the specialist
    `crypto_binary` predictor hook; the `weather` vertical routes
    through the GRU + EMOS temperature pipeline (task #246).  Every
    other built-in vertical must use the generic meta-model
    (predictor_hook=None)."""
    routing = {
        "sports": None,
        "weather": "weather_temp_gru_emos",
        "crypto": None,
        "crypto-binary": "crypto_binary",
        "politics": None,
    }
    for vertical, want_hook in routing.items():
        assert verticals.require_spec(vertical).predictor_hook == want_hook


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
