"""Unit tests for `risk/gates.py::_mode_default` (Task #137).

The mode defaulter pulls the active aggression mode out of
``operator_overrides['aggression_mode']`` and returns the corresponding
profile value.  These tests pin two pieces of behaviour:

* Conservative > Balanced > Aggressive ordering for ``min_edge_pp`` —
  the Conservative profile must require the highest min-edge, and
  Aggressive the lowest.  Same ordering for ``min_agreement_rate`` and
  ``max_confidence_band`` (lower = more permissive).
* An explicit ``operator_overrides`` entry for a tunable knob (e.g.
  ``min_edge_pp``) still wins over the mode default, exercised through
  ``gate_min_edge`` so we cover the public contract not just the
  internal helper.
"""
from __future__ import annotations

import asyncio

import pytest

from app.risk import gates as _gates


def _ctx(overrides: dict | None = None, *, edge: float = 0.05):
    return _gates.RiskContext(
        market_id="TEST-T1",
        side="YES",
        vertical="sports",
        league=None,
        market_type=None,
        matchup=None,
        current_price=0.5,
        model_probability=0.6,
        edge=edge,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=10,
        intended_notional_usd=5.0,
        bankroll_usd=1000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides or {},
        market_cooldown_until=None,
    )


# ---------------------------------------------------------------------------
# Mode ordering
# ---------------------------------------------------------------------------


def test_mode_default_falls_back_when_no_mode_set():
    # No `aggression_mode` on the overrides -> caller's fallback wins.
    ctx = _ctx({})
    assert _gates._mode_default(ctx, "min_edge_pp", 3.0) == 3.0


def test_mode_default_unknown_key_returns_fallback():
    ctx = _ctx({"aggression_mode": "balanced"})
    # The profile doesn't carry "made_up_knob" — fallback wins.
    assert _gates._mode_default(ctx, "made_up_knob", 1.5) == 1.5


def test_min_edge_pp_strict_ordering_across_modes():
    cons = _gates._mode_default(_ctx({"aggression_mode": "conservative"}),
                                "min_edge_pp", 0.0)
    bal = _gates._mode_default(_ctx({"aggression_mode": "balanced"}),
                               "min_edge_pp", 0.0)
    aggr = _gates._mode_default(_ctx({"aggression_mode": "aggressive"}),
                                "min_edge_pp", 0.0)
    assert cons > bal > aggr, (
        f"min_edge_pp ordering broke: conservative={cons}, "
        f"balanced={bal}, aggressive={aggr}"
    )


def test_min_agreement_rate_strict_ordering_across_modes():
    cons = _gates._mode_default(_ctx({"aggression_mode": "conservative"}),
                                "min_agreement_rate", 0.0)
    bal = _gates._mode_default(_ctx({"aggression_mode": "balanced"}),
                               "min_agreement_rate", 0.0)
    aggr = _gates._mode_default(_ctx({"aggression_mode": "aggressive"}),
                                "min_agreement_rate", 0.0)
    assert cons > bal > aggr


def test_max_confidence_band_loosens_with_aggression():
    cons = _gates._mode_default(_ctx({"aggression_mode": "conservative"}),
                                "max_confidence_band", 0.0)
    bal = _gates._mode_default(_ctx({"aggression_mode": "balanced"}),
                               "max_confidence_band", 0.0)
    aggr = _gates._mode_default(_ctx({"aggression_mode": "aggressive"}),
                                "max_confidence_band", 0.0)
    # Higher aggression tolerates wider consensus bands.
    assert cons < bal < aggr


def test_kelly_fraction_cap_loosens_with_aggression():
    cons = _gates._mode_default(_ctx({"aggression_mode": "conservative"}),
                                "kelly_fraction_cap", 0.0)
    bal = _gates._mode_default(_ctx({"aggression_mode": "balanced"}),
                               "kelly_fraction_cap", 0.0)
    aggr = _gates._mode_default(_ctx({"aggression_mode": "aggressive"}),
                                "kelly_fraction_cap", 0.0)
    assert cons < bal < aggr


def test_unknown_mode_falls_back_to_balanced_profile():
    # `_aggression_profile` normalizes to DEFAULT_MODE on garbage —
    # the mode defaulter inherits that behaviour.
    bad = _gates._mode_default(_ctx({"aggression_mode": "yolo"}),
                               "min_edge_pp", 0.0)
    bal = _gates._mode_default(_ctx({"aggression_mode": "balanced"}),
                               "min_edge_pp", 0.0)
    assert bad == bal


# ---------------------------------------------------------------------------
# Explicit operator override still wins
# ---------------------------------------------------------------------------


def test_gate_min_edge_uses_conservative_default_when_no_override():
    # Conservative requires min_edge_pp = 7.0 -> 0.07 edge floor.
    # Candidate has edge=0.05 — must fail.
    ctx = _ctx({"aggression_mode": "conservative"}, edge=0.05)
    res = asyncio.run(_gates.gate_min_edge(ctx))
    assert not res.passed
    assert res.details["min_edge"] == pytest.approx(0.07)


def test_gate_min_edge_explicit_override_beats_mode_default():
    # Conservative would normally reject edge=0.05 (default 7.0pp), but
    # an explicit override of 4.0pp must let it through.
    ctx = _ctx(
        {"aggression_mode": "conservative", "min_edge_pp": 4.0},
        edge=0.05,
    )
    res = asyncio.run(_gates.gate_min_edge(ctx))
    assert res.passed, f"explicit override ignored: {res.reason}"
    assert res.details["min_edge"] == pytest.approx(0.04)


def test_gate_min_edge_aggressive_default_lets_thin_edge_through():
    # Aggressive default = 2.0pp -> 0.02 floor; edge=0.025 must pass.
    ctx = _ctx({"aggression_mode": "aggressive"}, edge=0.025)
    res = asyncio.run(_gates.gate_min_edge(ctx))
    assert res.passed
    assert res.details["min_edge"] == pytest.approx(0.02)
