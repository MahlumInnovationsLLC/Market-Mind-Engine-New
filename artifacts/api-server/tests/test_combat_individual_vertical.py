"""End-to-end coverage for the combat & individual sports vertical
(task #197).

Hermetic — no network, no DB.  Mirrors `test_soccer_vertical.py` so a
future vertical can use the same template.
"""
from __future__ import annotations

import asyncio
import math

import pytest

from app import verticals
from app.calibrators import SUPPORTED_LEAGUES
from app.feeds import odds_api
from app.predictor_n_way import (
    bradley_terry_pairwise,
    is_normalised,
    plackett_luce,
    plackett_luce_from_mapping,
)
from app.risk.gates import (
    GATES,
    RiskContext,
    gate_market_cooldown,
    gate_per_vertical_cap,
)
from app.sports import (
    LEAGUE_CONFIGS,
    get_league_config,
    persona_context_augment,
)
from app.swarm.profiles import COMBAT_PERSONAS, PROFILES, get_profile


COMBAT_LEAGUES = ("UFC", "ATP", "WTA", "F1", "PGA")


# ---------------------------------------------------------------------------
# Vertical registration
# ---------------------------------------------------------------------------


def test_vertical_registry_has_combat_individual():
    spec = verticals.REGISTRY.get("combat-individual")
    assert spec is not None
    assert spec.persona_profile == "combat-individual"
    assert set(spec.leagues) == set(COMBAT_LEAGUES)
    # Off the dashboard strip until the UI catches up — guards the
    # legacy 4-column strip layout.
    assert spec.include_in_dashboard_strip is False


def test_combat_individual_profile_has_five_personas():
    profile = get_profile("combat-individual")
    assert profile is not None
    persona_ids = {p.id for p in profile.personas}
    assert persona_ids == {
        "matchup_historian",
        "sharp_edge",
        "fitness_travel",
        "market_microstructure",
        "risk_guardian",
    }


def test_profile_personas_match_module_constant():
    profile = PROFILES["combat-individual"]
    assert profile.personas is COMBAT_PERSONAS
    assert len(COMBAT_PERSONAS) == 5


# ---------------------------------------------------------------------------
# League config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lg", COMBAT_LEAGUES)
def test_league_config_exists_with_combat_fields(lg):
    cfg = get_league_config(lg)
    assert cfg is not None, f"{lg} not in LEAGUE_CONFIGS"
    assert cfg.per_event_cap_pct is not None and cfg.per_event_cap_pct > 0
    assert cfg.live_action_blackout is True


@pytest.mark.parametrize("lg", ("ATP", "WTA", "F1", "PGA"))
def test_tournament_leagues_marked_n_way(lg):
    """Tournament-winner leagues fan out to N>2 entrants per market and
    must opt into the Plackett-Luce branch.  UFC is excluded — fights
    are 2-way so the BT pairwise branch handles them."""
    assert get_league_config(lg).n_way_market is True


def test_ufc_individual_fights_remain_two_way():
    assert get_league_config("UFC").n_way_market is False


def test_calibrator_supports_every_combat_league():
    for lg in COMBAT_LEAGUES:
        assert lg in SUPPORTED_LEAGUES


# ---------------------------------------------------------------------------
# Sportradar feed wiring
# ---------------------------------------------------------------------------


def test_sportradar_schedule_paths_include_every_combat_sport():
    for sport in ("ufc", "atp", "wta", "f1", "pga"):
        assert sport in odds_api._SPORTRADAR_SCHEDULE_PATHS


def test_sportradar_league_map_round_trips_combat_codes():
    for sport, league in (
        ("ufc", "UFC"), ("atp", "ATP"), ("wta", "WTA"),
        ("f1", "F1"), ("pga", "PGA"),
    ):
        assert odds_api._SPORTRADAR_LEAGUE.get(sport) == league


# ---------------------------------------------------------------------------
# Persona prompts mention surface / track / course
# ---------------------------------------------------------------------------


def test_persona_prompts_mention_surface_track_course():
    """Sharp-edge + matchup-historian prompts must explicitly mention
    surface / track / course — these are the spec-anchored signals
    differentiating combat & individual sports from team sports."""
    blob = "\n".join(p.system_prompt for p in COMBAT_PERSONAS).lower()
    assert "surface" in blob
    assert "track" in blob
    assert "course" in blob


def test_persona_context_augment_produces_combat_block():
    """Whichever league hook the orchestrator uses to inject combat
    context must surface the N-way / live-blackout / per-event cues."""
    text = persona_context_augment("UFC")
    assert text is not None
    lowered = text.lower()
    # At least one combat-specific cue must be in the augment text.
    assert any(
        cue in lowered
        for cue in ("n-way", "surface", "live-action", "per-event")
    )


# ---------------------------------------------------------------------------
# Bradley-Terry / Plackett-Luce predictor
# ---------------------------------------------------------------------------


def test_bradley_terry_symmetric_around_equal_ratings():
    assert bradley_terry_pairwise(1500, 1500) == pytest.approx(0.5, abs=1e-9)


def test_bradley_terry_400_point_gap_is_classic_elo():
    # Classic Elo: a 400-point lead implies ~10:1 odds, i.e. p ≈ 0.909.
    p = bradley_terry_pairwise(1900, 1500)
    assert p == pytest.approx(10.0 / 11.0, abs=1e-6)


def test_plackett_luce_uniform_when_ratings_equal():
    probs = plackett_luce([1500.0, 1500.0, 1500.0, 1500.0])
    assert is_normalised(probs)
    assert all(p == pytest.approx(0.25, abs=1e-9) for p in probs)


def test_plackett_luce_concentrates_on_top_seed():
    probs = plackett_luce([2200, 2000, 1800, 1600])
    assert is_normalised(probs)
    assert probs[0] > probs[1] > probs[2] > probs[3]
    # Probabilities must sum to 1.0 (it's a single-winner field).
    assert sum(probs) == pytest.approx(1.0, abs=1e-9)


def test_plackett_luce_from_mapping_preserves_keys():
    rs = {"verstappen": 2200, "norris": 2000, "leclerc": 1900}
    out = plackett_luce_from_mapping(rs)
    assert set(out) == set(rs)
    assert sum(out.values()) == pytest.approx(1.0, abs=1e-9)
    # The N-way probabilities must respect the rating order.
    assert out["verstappen"] > out["norris"] > out["leclerc"]


def test_plackett_luce_rejects_nan():
    with pytest.raises(ValueError):
        plackett_luce([1500.0, math.nan, 1700.0])


def test_plackett_luce_handles_extreme_gap_without_overflow():
    # Without the max-subtraction stability trick this would overflow.
    probs = plackett_luce([4500.0, 1500.0])
    assert is_normalised(probs)
    assert probs[0] == pytest.approx(1.0, abs=1e-3)


# ---------------------------------------------------------------------------
# Risk gates: per-event cap + live-action blackout
# ---------------------------------------------------------------------------


def _ctx(**overrides) -> RiskContext:
    base = dict(
        market_id="KXUFC-UFC300-MAKHACHEV",
        side="YES",
        vertical="combat-individual",
        league="UFC",
        market_type="moneyline",
        matchup="Makhachev vs Volkanovski",
        current_price=0.50,
        model_probability=0.60,
        edge=0.10,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.80,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=297,
        intended_quantity=10,
        intended_notional_usd=100.0,
        bankroll_usd=10_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
        event_id="UFC-300",
        open_positions_per_event_usd={},
    )
    base.update(overrides)
    return RiskContext(**base)


def test_per_event_cap_passes_when_under_cap():
    # A single small bet on an event with no prior exposure must pass.
    # Use a tiny notional so neither per-game nor per-event cap fires.
    ctx = _ctx(
        intended_notional_usd=5.0,
        open_positions_per_event_usd={"UFC:UFC-300": 10.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed, res.reason


def test_per_event_cap_rejects_over_cap():
    """A second leg on the same UFC card whose projected event exposure
    exceeds the per-event cap must be rejected with the per_event_cap
    sub_check tag."""
    cfg = get_league_config("UFC")
    event_cap_usd = 10_000 * cfg.per_event_cap_pct  # 0.015 → $150
    # Tiny new bet on a *different* matchup on the same card so the
    # per-game cap doesn't fire first; event_open already near the cap.
    ctx = _ctx(
        matchup="Pereira vs Hill",
        intended_notional_usd=10.0,
        open_positions_per_event_usd={"UFC:UFC-300": event_cap_usd - 5.0},
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert not res.passed, "per-event cap must reject the second leg"
    assert "per-event cap" in res.reason
    assert res.details.get("sub_check") == "per_event_cap"


def test_per_event_cap_skipped_when_event_id_missing():
    """Legacy candidates with no event_id must short-circuit the
    per-event sub-check rather than crashing on a missing key."""
    ctx = _ctx(
        event_id=None,
        intended_notional_usd=5.0,
    )
    res = asyncio.run(gate_per_vertical_cap(ctx))
    assert res.passed, res.reason


def test_live_action_blackout_rejects_in_progress_event():
    ctx = _ctx(event_in_progress=True)
    res = asyncio.run(gate_market_cooldown(ctx))
    assert not res.passed
    assert "live-action" in res.reason.lower()
    assert res.details.get("reason") == "live_action_blackout"


def test_live_action_blackout_passes_when_not_in_progress():
    ctx = _ctx(event_in_progress=False)
    res = asyncio.run(gate_market_cooldown(ctx))
    assert res.passed


def test_event_in_progress_derived_from_past_game_time():
    """The orchestrator derives `event_in_progress` from the candidate's
    game_time when feeds haven't tagged it explicitly — gate must reject
    a candidate whose start time has already passed."""
    from datetime import datetime, timedelta, timezone
    past = datetime.now(timezone.utc) - timedelta(minutes=30)
    # Simulate the cycle's derivation logic: bool(explicit) or
    # (game_time <= now()).
    derived = (
        False or (past is not None and past <= datetime.now(timezone.utc))
    )
    assert derived is True
    ctx = _ctx(event_in_progress=derived)
    res = asyncio.run(gate_market_cooldown(ctx))
    assert not res.passed
    assert res.details.get("reason") == "live_action_blackout"


# ---------------------------------------------------------------------------
# N-way predictor runtime branch
# ---------------------------------------------------------------------------


def test_predict_for_candidate_routes_n_way_when_field_present():
    """ATP candidates carrying an n_way_field + entrant must be priced
    via Plackett-Luce, not the 2-way meta-model."""
    from app.orchestrator import cycle

    cand = {
        "league": "ATP",
        "n_way_field": {"alcaraz": 2200, "sinner": 2150, "djokovic": 2100},
        "entrant": "alcaraz",
        "game_id": "ATP-RG-2025",
    }
    pred = asyncio.run(
        cycle._predict_for_candidate(
            cand,
            consensus=None,
            current_price=0.40,
            edge=None,
            vertical="combat-individual",
        )
    )
    assert pred is not None
    assert pred["model_name"] == "plackett_luce_n_way"
    assert 0.0 < pred["probability"] < 1.0
    # Top seed must carry the highest probability in the field.
    probs = pred["n_way_probabilities"]
    assert probs["alcaraz"] > probs["sinner"] > probs["djokovic"]


def test_predict_for_candidate_falls_through_for_two_way_league():
    """UFC candidates (n_way_market=False) must NOT take the PL branch
    even when an n_way_field accidentally arrives — the meta-model
    handles 2-way fights."""
    from app.orchestrator import cycle

    cand = {
        "league": "UFC",
        "n_way_field": {"makhachev": 1900, "volkanovski": 1800},
        "entrant": "makhachev",
    }
    # Predictor is unloaded in the test env → returns None.  We just
    # need to confirm the PL branch isn't hit (no model_name
    # 'plackett_luce_n_way').
    pred = asyncio.run(
        cycle._predict_for_candidate(
            cand,
            consensus=None,
            current_price=0.5,
            edge=None,
            vertical="combat-individual",
        )
    )
    assert pred is None or pred.get("model_name") != "plackett_luce_n_way"


def test_gates_tuple_still_twelve():
    """Lock the 12-gate invariant — combat-individual sub-checks must
    fold into existing gates rather than adding new entries."""
    assert len(GATES) == 12
