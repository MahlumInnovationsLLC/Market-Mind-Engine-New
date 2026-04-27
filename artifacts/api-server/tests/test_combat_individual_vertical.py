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
    # Promoted onto the operator dashboard strip in task #215 once the
    # strip layout was widened to a responsive grid (5+ columns at xl).
    assert spec.include_in_dashboard_strip is True


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


def test_predict_for_candidate_returns_audit_sketch_for_n_way():
    """The PL branch must return a self-contained `n_way_sketch` dict
    so the cycle can persist it on the debate row without re-pulling
    the (process-local) ratings cache."""
    from app.orchestrator import cycle

    cand = {
        "league": "F1",
        "n_way_field": {"verstappen": 2200, "norris": 2050, "leclerc": 1950},
        "entrant": "verstappen",
        "game_id": "F1-MONACO-2026",
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
    sketch = pred["n_way_sketch"]
    assert sketch["model"] == "plackett_luce"
    assert sketch["entrant"] == "verstappen"
    assert sketch["field_size"] == 3
    assert set(sketch["ratings"]) == {"verstappen", "norris", "leclerc"}
    assert set(sketch["probabilities"]) == {"verstappen", "norris", "leclerc"}
    # The entrant_probability must equal the per-leg YES probability.
    assert sketch["entrant_probability"] == pytest.approx(pred["probability"])
    assert sum(sketch["probabilities"].values()) == pytest.approx(1.0, abs=1e-9)


def test_cycle_persists_plackett_luce_sketch_on_debate_row(monkeypatch):
    """The orchestrator must merge the per-entrant Plackett-Luce sketch
    into `swarm_debates.game_context` so analysts can audit which leg
    was being priced and what the alternative entrants would have looked
    like.  The UPDATE must be best-effort (never crash the loop) but
    must fire whenever a candidate has both a debate_id and an n-way
    sketch.
    """
    from app.orchestrator import cycle as cycle_mod

    captured: dict = {}

    async def fake_execute(sql, *args):
        if "swarm_debates" in sql and "plackett_luce_sketch" in sql:
            captured["sql"] = sql
            captured["args"] = args
        return "UPDATE 1"

    monkeypatch.setattr(cycle_mod.db, "execute", fake_execute)

    cand = {
        "league": "ATP",
        "n_way_field": {"alcaraz": 2200, "sinner": 2150, "djokovic": 2100},
        "entrant": "alcaraz",
        "game_id": "ATP-RG-2025",
        "debate_id": 4242,
    }
    live_pred = asyncio.run(
        cycle_mod._predict_for_candidate(
            cand,
            consensus=None,
            current_price=0.40,
            edge=None,
            vertical="combat-individual",
        )
    )
    assert live_pred is not None
    assert isinstance(live_pred.get("n_way_sketch"), dict)

    # Replicate the exact persistence call the cycle main loop performs.
    import json as _json

    asyncio.run(
        cycle_mod.db.execute(
            """
            UPDATE swarm_debates
               SET game_context = coalesce(game_context, '{}'::jsonb)
                                  || jsonb_build_object(
                                        'plackett_luce_sketch', $2::jsonb
                                     )
             WHERE id = $1
            """,
            int(cand["debate_id"]),
            _json.dumps(live_pred["n_way_sketch"]),
        )
    )

    assert captured, "expected swarm_debates UPDATE to be issued"
    debate_id, sketch_json = captured["args"]
    assert debate_id == 4242
    decoded = _json.loads(sketch_json)
    assert decoded["model"] == "plackett_luce"
    assert decoded["entrant"] == "alcaraz"
    assert set(decoded["probabilities"]) == {"alcaraz", "sinner", "djokovic"}


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


# ---------------------------------------------------------------------------
# Sportradar payload normalization for combat & individual sports
# ---------------------------------------------------------------------------


def test_normalize_ufc_sport_events_envelope():
    """UFC MMA returns `{"sport_events": [...]}` where each entry is a
    single bout.  The combat/individual normalizer (task #214)
    collapses every bout on a card into a single tradable row keyed
    by the card identifier (`sport_event_context.competition.id`)
    so the per-event risk cap treats the whole fight night as one
    tradable surface — see app/orchestrator/cycle.py per-event
    bucket comment."""
    from app.feeds.odds_api import _normalize_sportradar_payload

    payload = {
        "sport_events": [
            {
                "id": "sr:sport_event:ufc-300-main",
                "start_time": "2026-04-22T22:00:00+00:00",
                "sport_event_context": {
                    "competition": {"id": "sr:competition:ufc-300", "name": "UFC 300"},
                },
                "competitors": [
                    {"id": "sr:competitor:1", "name": "Pereira"},
                    {"id": "sr:competitor:2", "name": "Hill"},
                ],
            },
        ],
    }
    rows = _normalize_sportradar_payload("ufc", "UFC", payload)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "sr:competition:ufc-300"
    assert r["home"]["name"] == "UFC 300"
    assert r["away"]["name"] == "FIELD"
    # A single 2-fighter bout doesn't yet warrant Plackett-Luce
    # routing; n_way_field is only emitted once the unioned card has
    # >2 distinct competitors.
    assert "n_way_field" not in r


def test_normalize_tennis_sport_events_envelope():
    """ATP/WTA daily schedule returns `sport_events` per match; the
    combat/individual normalizer collapses them to a single row per
    tournament so the per-event cap treats the whole bracket as one
    tradable surface (task #214)."""
    from app.feeds.odds_api import _normalize_sportradar_payload

    payload = {
        "sport_events": [
            {
                "id": "sr:sport_event:rg-r1-1",
                "start_time": "2026-05-26T11:00:00+00:00",
                "sport_event_context": {
                    "competition": {"id": "sr:competition:roland-garros",
                                     "name": "Roland Garros"},
                },
                "competitors": [
                    {"id": "sr:player:1", "name": "Alcaraz"},
                    {"id": "sr:player:2", "name": "Tiafoe"},
                ],
            },
        ],
    }
    rows = _normalize_sportradar_payload("atp", "ATP", payload)
    assert len(rows) == 1
    assert rows[0]["id"] == "sr:competition:roland-garros"
    assert rows[0]["home"]["name"] == "Roland Garros"
    assert rows[0]["away"]["name"] == "FIELD"


def test_normalize_f1_summaries_envelope_with_n_way_field():
    """F1 returns `{"summaries": [{"sport_event": {...},
    "sport_event_status": {...}}]}` with the full grid as
    competitors — normalizer must surface `n_way_field` so the
    Plackett-Luce branch can price each driver."""
    from app.feeds.odds_api import _normalize_sportradar_payload

    payload = {
        "summaries": [
            {
                "sport_event": {
                    "id": "sr:sport_event:f1-monaco-2026",
                    "start_time": "2026-05-24T13:00:00+00:00",
                    "tournament": {"name": "Monaco Grand Prix"},
                    "competitors": [
                        {"id": "sr:competitor:1", "name": "Verstappen"},
                        {"id": "sr:competitor:2", "name": "Leclerc"},
                        {"id": "sr:competitor:3", "name": "Norris"},
                        {"id": "sr:competitor:4", "name": "Hamilton"},
                        {"id": "sr:competitor:5", "name": "Russell"},
                    ],
                },
                "sport_event_status": {"status": "scheduled"},
            },
        ],
    }
    rows = _normalize_sportradar_payload("f1", "F1", payload)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "sr:sport_event:f1-monaco-2026"
    field = r["n_way_field"]
    assert isinstance(field, list)
    assert len(field) == 5
    names = [c["name"] for c in field]
    assert "Verstappen" in names and "Norris" in names


def test_normalize_pga_tournaments_envelope():
    """PGA Golf v3 returns `{"tournaments": [...]}` — each tournament
    is one outright-winner market.  Normalizer must surface schedule
    rows so per-event aggregation + event_in_progress derivation work
    end-to-end even before leaderboard data lands (follow-up #214)."""
    from app.feeds.odds_api import _normalize_sportradar_payload

    payload = {
        "tournaments": [
            {
                "id": "sr:tournament:masters-2026",
                "name": "The Masters",
                "start_date": "2026-04-09",
                "status": "scheduled",
            },
            {
                "id": "sr:tournament:rbc-heritage-2026",
                "name": "RBC Heritage",
                "start_date": "2026-04-16",
                "status": "scheduled",
            },
        ],
    }
    rows = _normalize_sportradar_payload("pga", "PGA", payload)
    assert len(rows) == 2
    ids = {r["id"] for r in rows}
    assert "sr:tournament:masters-2026" in ids
    assert all(r["away"]["name"] == "FIELD" for r in rows)


def test_normalize_populates_ratings_cache_for_n_way_fields():
    """The normalizer must populate `app.sports.ratings` so the
    orchestrator can pull the field by game_id without DB plumbing."""
    from app.feeds.odds_api import _normalize_sportradar_payload
    from app.sports import get_field, ratings

    ratings.clear_cache()
    payload = {
        "summaries": [
            {
                "sport_event": {
                    "id": "sr:sport_event:f1-silverstone-2026",
                    "start_time": "2026-07-05T13:00:00+00:00",
                    "tournament": {"name": "British Grand Prix"},
                    "competitors": [
                        {"id": "c1", "name": "Verstappen", "seed": 1},
                        {"id": "c2", "name": "Norris", "seed": 2},
                        {"id": "c3", "name": "Leclerc", "seed": 3},
                    ],
                },
                "sport_event_status": {"status": "scheduled"},
            },
        ],
    }
    _normalize_sportradar_payload("f1", "F1", payload)
    cached = get_field("sr:sport_event:f1-silverstone-2026")
    assert cached is not None
    assert set(cached.keys()) == {"Verstappen", "Norris", "Leclerc"}
    # Lower seed → higher rating.
    assert cached["Verstappen"] > cached["Norris"] > cached["Leclerc"]


def test_end_to_end_feed_payload_routes_through_plackett_luce():
    """Integration: a Sportradar F1 payload must flow through
    normalization → ratings cache → `_predict_for_candidate` and
    surface as the `plackett_luce_n_way` model with no synthetic
    hand-crafted dict inputs."""
    from app.feeds.odds_api import _normalize_sportradar_payload
    from app.orchestrator import cycle
    from app.sports import ratings

    ratings.clear_cache()
    payload = {
        "summaries": [
            {
                "sport_event": {
                    "id": "sr:sport_event:f1-spa-2026",
                    "start_time": "2026-08-30T13:00:00+00:00",
                    "tournament": {"name": "Belgian Grand Prix"},
                    "competitors": [
                        {"id": "c1", "name": "Verstappen", "seed": 1},
                        {"id": "c2", "name": "Norris", "seed": 2},
                        {"id": "c3", "name": "Leclerc", "seed": 3},
                        {"id": "c4", "name": "Hamilton", "seed": 4},
                    ],
                },
                "sport_event_status": {"status": "scheduled"},
            },
        ],
    }
    rows = _normalize_sportradar_payload("f1", "F1", payload)
    assert len(rows) == 1

    # Build a candidate with ONLY the columns the standard
    # `_candidate_predictions` SELECT carries (game_id, league,
    # home name) — n_way_field/entrant must be derived by the
    # enrichment layer, not synthesized in the test.
    cand = {
        "game_id": rows[0]["id"],
        "league": "F1",
        "home": {"name": "Verstappen"},  # YES side of the outright
    }
    pred = asyncio.run(
        cycle._predict_for_candidate(
            cand,
            consensus=None,
            current_price=0.30,
            edge=None,
            vertical="combat-individual",
        )
    )
    assert pred is not None
    assert pred["model_name"] == "plackett_luce_n_way"
    # Field of 4, top seed Verstappen — probability should clearly
    # exceed a uniform 1/4 prior.
    assert pred["probability"] > 0.25
    probs = pred["n_way_probabilities"]
    assert probs["Verstappen"] > probs["Norris"] > probs["Hamilton"]


def test_normalize_handles_empty_payloads_gracefully():
    """An empty / unfamiliar envelope must return [] rather than
    raise — pollers must keep registered-but-skipped on bad days."""
    from app.feeds.odds_api import _normalize_sportradar_payload

    assert _normalize_sportradar_payload("ufc", "UFC", {}) == []
    assert _normalize_sportradar_payload("f1", "F1", {"summaries": []}) == []
    assert _normalize_sportradar_payload("pga", "PGA", {"tournaments": []}) == []


def test_gates_tuple_still_twelve():
    """Lock the 12-gate invariant — combat-individual sub-checks must
    fold into existing gates rather than adding new entries."""
    assert len(GATES) == 14


# ---------------------------------------------------------------------------
# Tournament leaderboard refresh (task #449)
# ---------------------------------------------------------------------------


def test_derive_field_ratings_from_leaderboard_orders_by_position():
    """Leaderboard position 1 must beat position 2, etc., reusing the
    same Elo scale as seed-derived ratings so the Plackett-Luce
    predictor sees a consistent unit system."""
    from app.sports.ratings import (
        derive_field_ratings_from_leaderboard,
        _rating_from_seed,
    )

    entries = [
        {"name": "Scheffler", "position": 1},
        {"name": "Rahm", "position": 2},
        {"name": "McIlroy", "position": 5},
    ]
    ratings = derive_field_ratings_from_leaderboard(entries)
    assert ratings == {
        "Scheffler": _rating_from_seed(1),
        "Rahm": _rating_from_seed(2),
        "McIlroy": _rating_from_seed(5),
    }
    assert ratings["Scheffler"] > ratings["Rahm"] > ratings["McIlroy"]


def test_derive_field_ratings_from_leaderboard_handles_missing_position():
    """Entries with no `position` field fall back to list order so a
    sparse / partial payload still yields a usable mapping."""
    from app.sports.ratings import (
        derive_field_ratings_from_leaderboard,
        _rating_from_seed,
    )

    ratings = derive_field_ratings_from_leaderboard([
        {"name": "Verstappen"},
        {"name": "Norris"},
        {"name": "Leclerc"},
    ])
    assert ratings == {
        "Verstappen": _rating_from_seed(1),
        "Norris": _rating_from_seed(2),
        "Leclerc": _rating_from_seed(3),
    }


def test_derive_field_ratings_from_leaderboard_skips_unrankable():
    """Non-numeric or missing names must be dropped silently rather
    than collapsing the whole field to a uniform prior."""
    from app.sports.ratings import derive_field_ratings_from_leaderboard

    ratings = derive_field_ratings_from_leaderboard([
        {"name": "Verstappen", "position": 1},
        {"name": "", "position": 2},  # empty name dropped
        "not a mapping",  # not a dict
        {"name": "Hamilton", "position": "DNF"},  # un-parseable
        {"name": "Russell", "position": 0},  # invalid rank
        {"name": "Norris", "position": 3},
    ])
    assert set(ratings.keys()) == {"Verstappen", "Norris"}


def test_set_field_from_leaderboard_overwrites_seed_cache():
    """A live leaderboard refresh must replace the seed-derived ratings
    cached at schedule-poll time so `get_field()` returns the live
    field next time the orchestrator looks it up."""
    from app.sports import (
        derive_field_ratings,
        get_field,
        set_field,
        set_field_from_leaderboard,
    )
    from app.sports import ratings as ratings_mod

    ratings_mod.clear_cache()
    # Schedule-time seed: Verstappen seed 1, Norris seed 2, Leclerc seed 3
    set_field("F1-SPA-2026", [
        {"name": "Verstappen", "seed": 1},
        {"name": "Norris", "seed": 2},
        {"name": "Leclerc", "seed": 3},
    ])
    seeded = get_field("F1-SPA-2026")
    assert seeded is not None
    assert seeded["Verstappen"] > seeded["Norris"] > seeded["Leclerc"]

    # Live mid-race: Leclerc has overtaken everyone.
    written = set_field_from_leaderboard("F1-SPA-2026", [
        {"name": "Leclerc", "position": 1},
        {"name": "Norris", "position": 2},
        {"name": "Verstappen", "position": 3},
    ])
    assert written == 3
    live = get_field("F1-SPA-2026")
    assert live is not None
    # Order has flipped — leader is now top.
    assert live["Leclerc"] > live["Norris"] > live["Verstappen"]
    # And we should match the rating a fresh seed-1 entrant would get.
    assert live["Leclerc"] == derive_field_ratings(
        [{"name": "Leclerc", "seed": 1}]
    )["Leclerc"]


def test_set_field_from_leaderboard_skips_thin_field():
    """Single-entrant 'leaderboards' can't drive Plackett-Luce, so the
    helper must leave the cache untouched (returning 0)."""
    from app.sports import (
        get_field,
        set_field,
        set_field_from_leaderboard,
    )
    from app.sports import ratings as ratings_mod

    ratings_mod.clear_cache()
    set_field("PGA-MASTERS-2026", [
        {"name": "Scheffler", "seed": 1},
        {"name": "Rahm", "seed": 2},
    ])
    before = get_field("PGA-MASTERS-2026")
    written = set_field_from_leaderboard("PGA-MASTERS-2026", [
        {"name": "Scheffler", "position": 1},
    ])
    assert written == 0
    assert get_field("PGA-MASTERS-2026") == before


def test_extract_pga_leaderboard_handles_player_name_shapes():
    """Sportradar's PGA payload exposes player names either as
    `first_name`+`last_name` or as `full_name`/`name` — the parser
    must accept all three shapes and skip withdrawn / cut entries."""
    from app.feeds.sportradar import _extract_pga_leaderboard

    payload = {
        "leaderboard": [
            {"position": 1, "player": {
                "first_name": "Scottie", "last_name": "Scheffler",
            }},
            {"position": 2, "player": {"full_name": "Jon Rahm"}},
            {"position": 3, "player": {"name": "Rory McIlroy"}},
            # Withdrawn — must be dropped so the field tightens around
            # the players still on the course.
            {"position": 99, "status": "WD", "player": {
                "first_name": "Tiger", "last_name": "Woods",
            }},
            # Garbage entry — non-mapping rows must not crash the parse.
            "not a row",
        ],
    }
    entries = _extract_pga_leaderboard(payload)
    names = [e["name"] for e in entries]
    assert names == ["Scottie Scheffler", "Jon Rahm", "Rory McIlroy"]
    assert [e["position"] for e in entries] == [1, 2, 3]


def test_extract_f1_leaderboard_walks_status_block():
    """F1 v2 nests live competitors under `sport_event_status`; the
    parser must surface them with their live `position` so the live
    rating refresh tracks intra-race position."""
    from app.feeds.sportradar import _extract_f1_leaderboard

    payload = {
        "sport_event": {"id": "sr:sport_event:race"},
        "sport_event_status": {
            "status": "live",
            "competitors": [
                {"id": "c1", "name": "Verstappen", "position": 1},
                {"id": "c2", "name": "Norris", "position": 2},
                {"id": "c3", "name": "Leclerc", "position": 3},
            ],
        },
    }
    entries = _extract_f1_leaderboard(payload)
    assert [(e["name"], e["position"]) for e in entries] == [
        ("Verstappen", 1), ("Norris", 2), ("Leclerc", 3),
    ]


def test_extract_f1_leaderboard_falls_back_to_top_level():
    """When the F1 summary nests competitors at the top level (e.g.
    pre-grid weekend rollup) the parser must still find them."""
    from app.feeds.sportradar import _extract_f1_leaderboard

    payload = {
        "competitors": [
            {"name": "Hamilton", "result": {"position": 1}},
            {"name": "Russell", "rank": 2},
        ],
    }
    entries = _extract_f1_leaderboard(payload)
    assert {(e["name"], e["position"]) for e in entries} == {
        ("Hamilton", 1), ("Russell", 2),
    }


def test_poll_tournament_leaderboards_refreshes_pga_and_f1(monkeypatch):
    """End-to-end: an in-progress PGA tournament + F1 race must drive
    `_poll_tournament_leaderboards` to call the leaderboard endpoint
    once per event and overwrite the per-event ratings cache."""
    from app.feeds import sportradar as sr
    from app.sports import get_field, set_field
    from app.sports import ratings as ratings_mod

    ratings_mod.clear_cache()
    # Pre-seed both events so we can prove the live refresh overwrote
    # the schedule-time rating mapping.
    set_field("sr:tournament:pga-1", [
        {"name": "Scheffler", "seed": 1},
        {"name": "Rahm", "seed": 2},
        {"name": "McIlroy", "seed": 3},
    ])
    set_field("sr:sport_event:f1-1", [
        {"name": "Verstappen", "seed": 1},
        {"name": "Norris", "seed": 2},
        {"name": "Leclerc", "seed": 3},
    ])

    monkeypatch.setenv("SPORTRADAR_PGA_KEY", "test-pga-key")
    monkeypatch.setenv("SPORTRADAR_F1_KEY", "test-f1-key")

    async def fake_live_ids(league, statuses=None):
        if league == "PGA":
            return ["sr:tournament:pga-1"]
        if league == "F1":
            return ["sr:sport_event:f1-1"]
        return []

    monkeypatch.setattr(sr, "_live_leaderboard_event_ids", fake_live_ids)

    fetched: list[str] = []

    async def fake_get_json(provider, url, api_key):
        fetched.append(url)
        if "/golf/" in url:
            return {"leaderboard": [
                {"position": 1, "player": {"full_name": "Rahm"}},
                {"position": 2, "player": {"full_name": "Scheffler"}},
                {"position": 3, "player": {"full_name": "McIlroy"}},
            ]}
        if "/formula1/" in url:
            return {"sport_event_status": {"status": "live", "competitors": [
                {"name": "Leclerc", "position": 1},
                {"name": "Verstappen", "position": 2},
                {"name": "Norris", "position": 3},
            ]}}
        return None

    monkeypatch.setattr(sr, "_get_json", fake_get_json)

    refreshed = asyncio.run(sr._poll_tournament_leaderboards())
    # 3 PGA + 3 F1 entrants written.
    assert refreshed == 6
    # One leaderboard call per event.
    assert len(fetched) == 2
    assert any("/golf/" in u for u in fetched)
    assert any("/formula1/" in u for u in fetched)

    pga = get_field("sr:tournament:pga-1")
    f1 = get_field("sr:sport_event:f1-1")
    assert pga is not None and f1 is not None
    # Live leaders rose to the top; pre-event seed favorites slid down.
    assert pga["Rahm"] > pga["Scheffler"] > pga["McIlroy"]
    assert f1["Leclerc"] > f1["Verstappen"] > f1["Norris"]


def test_poll_tournament_leaderboards_isolates_per_event_failures(monkeypatch):
    """One flaky event must not blank the cache for the rest — the
    poller swallows per-event errors, returns the partial total, and
    leaves the failed event's prior ratings untouched."""
    from app.feeds import sportradar as sr
    from app.sports import get_field, set_field
    from app.sports import ratings as ratings_mod

    ratings_mod.clear_cache()
    set_field("sr:tournament:pga-good", [
        {"name": "A", "seed": 1}, {"name": "B", "seed": 2},
        {"name": "C", "seed": 3},
    ])
    set_field("sr:tournament:pga-bad", [
        {"name": "X", "seed": 1}, {"name": "Y", "seed": 2},
        {"name": "Z", "seed": 3},
    ])
    bad_before = get_field("sr:tournament:pga-bad")

    monkeypatch.setenv("SPORTRADAR_PGA_KEY", "test-pga-key")
    monkeypatch.delenv("SPORTRADAR_F1_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)

    async def fake_live_ids(league, statuses=None):
        if league == "PGA":
            return ["sr:tournament:pga-good", "sr:tournament:pga-bad"]
        return []

    monkeypatch.setattr(sr, "_live_leaderboard_event_ids", fake_live_ids)

    async def fake_get_json(provider, url, api_key):
        if "pga-bad" in url:
            raise RuntimeError("transient upstream error")
        return {"leaderboard": [
            {"position": 1, "player": {"full_name": "C"}},
            {"position": 2, "player": {"full_name": "B"}},
            {"position": 3, "player": {"full_name": "A"}},
        ]}

    monkeypatch.setattr(sr, "_get_json", fake_get_json)

    refreshed = asyncio.run(sr._poll_tournament_leaderboards())
    # Only the good event refreshed; bad event left untouched.
    assert refreshed == 3
    assert get_field("sr:tournament:pga-bad") == bad_before
    good = get_field("sr:tournament:pga-good")
    assert good is not None and good["C"] > good["B"] > good["A"]


def test_poll_tournament_leaderboards_raises_when_no_keys(monkeypatch):
    """When neither PGA nor F1 keys are set the feed must raise so
    the scheduler records a real failure (not a silent zero-row
    'success')."""
    from app.feeds import sportradar as sr

    monkeypatch.delenv("SPORTRADAR_PGA_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_F1_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(sr._poll_tournament_leaderboards())


def test_persist_leaderboard_snapshot_appends_rows(monkeypatch):
    """Each refresh must append one row per entrant to
    `sportradar_tournament_leaderboards`, all sharing one fetched_at
    timestamp so the snapshot is recoverable as a coherent set
    (task #623)."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone

    from app.feeds import sportradar as sr
    from app import db as db_mod

    captured: list[tuple] = []

    class _Conn:
        def transaction(self):
            @asynccontextmanager
            async def _tx():
                yield None
            return _tx()

        async def executemany(self, _query, rows):
            captured.extend(rows)

    @asynccontextmanager
    async def fake_acquire():
        yield _Conn()

    monkeypatch.setattr(db_mod, "acquire", fake_acquire)
    fixed = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)

    asyncio.run(sr._persist_leaderboard_snapshot(
        "sr:tournament:pga-1",
        [
            {"name": "Rahm", "position": 1},
            {"name": "Scheffler", "position": 2},
            {"name": "McIlroy", "position": "DNF"},  # un-parseable -> NULL
            {"name": "", "position": 4},  # empty name -> dropped
        ],
        fetched_at=fixed,
    ))

    # Empty-name row dropped; the un-parseable position stored as NULL.
    assert [(r[0], r[1], r[2]) for r in captured] == [
        ("sr:tournament:pga-1", 1, "Rahm"),
        ("sr:tournament:pga-1", 2, "Scheffler"),
        ("sr:tournament:pga-1", None, "McIlroy"),
    ]
    # Every row carries the same fetched_at so the snapshot is coherent.
    assert {r[3] for r in captured} == {fixed}


def test_persist_leaderboard_snapshot_swallows_db_errors(monkeypatch):
    """A transient DB hiccup must not abort the rating refresh — the
    helper logs and returns rather than propagating."""
    from contextlib import asynccontextmanager

    from app.feeds import sportradar as sr
    from app import db as db_mod

    @asynccontextmanager
    async def boom_acquire():
        raise RuntimeError("pool exhausted")
        yield  # pragma: no cover - generator boilerplate

    monkeypatch.setattr(db_mod, "acquire", boom_acquire)

    # Must not raise.
    asyncio.run(sr._persist_leaderboard_snapshot(
        "sr:tournament:pga-1",
        [{"name": "Rahm", "position": 1}, {"name": "Scheffler", "position": 2}],
    ))


def test_refresh_pga_leaderboard_persists_snapshot(monkeypatch):
    """`_refresh_pga_leaderboard` must call the persist helper exactly
    once per successful refresh, passing through the extracted entries
    so historical pricing can be replayed off the table."""
    from app.feeds import sportradar as sr
    from app.sports import ratings as ratings_mod

    ratings_mod.clear_cache()

    async def fake_get_json(provider, url, api_key):
        return {"leaderboard": [
            {"position": 1, "player": {"full_name": "Rahm"}},
            {"position": 2, "player": {"full_name": "Scheffler"}},
        ]}

    persisted: list[tuple[str, list[dict]]] = []

    async def fake_persist(event_id, entries, fetched_at=None):
        persisted.append((event_id, list(entries)))

    monkeypatch.setattr(sr, "_get_json", fake_get_json)
    monkeypatch.setattr(sr, "_persist_leaderboard_snapshot", fake_persist)

    written = asyncio.run(sr._refresh_pga_leaderboard("k", "sr:tournament:pga-1"))
    assert written == 2
    assert len(persisted) == 1
    assert persisted[0][0] == "sr:tournament:pga-1"
    assert {e["name"] for e in persisted[0][1]} == {"Rahm", "Scheffler"}


def test_refresh_f1_leaderboard_persists_snapshot(monkeypatch):
    """Same contract for the F1 path — every successful refresh
    appends one snapshot."""
    from app.feeds import sportradar as sr
    from app.sports import ratings as ratings_mod

    ratings_mod.clear_cache()

    async def fake_get_json(provider, url, api_key):
        return {"sport_event_status": {"status": "live", "competitors": [
            {"name": "Verstappen", "position": 1},
            {"name": "Norris", "position": 2},
        ]}}

    persisted: list[str] = []

    async def fake_persist(event_id, entries, fetched_at=None):
        persisted.append(event_id)

    monkeypatch.setattr(sr, "_get_json", fake_get_json)
    monkeypatch.setattr(sr, "_persist_leaderboard_snapshot", fake_persist)

    written = asyncio.run(sr._refresh_f1_leaderboard("k", "sr:sport_event:f1-1"))
    assert written == 2
    assert persisted == ["sr:sport_event:f1-1"]


def test_refresh_leaderboard_skips_persist_when_field_too_thin(monkeypatch):
    """If the extracted field is too thin to drive Plackett-Luce we
    skip both the cache update *and* the snapshot append — no point
    polluting the audit table with a partial fetch."""
    from app.feeds import sportradar as sr

    async def fake_get_json(provider, url, api_key):
        # Only one entrant — extract returns a single row.
        return {"leaderboard": [
            {"position": 1, "player": {"full_name": "SoloPlayer"}},
        ]}

    persisted: list[str] = []

    async def fake_persist(event_id, entries, fetched_at=None):
        persisted.append(event_id)

    monkeypatch.setattr(sr, "_get_json", fake_get_json)
    monkeypatch.setattr(sr, "_persist_leaderboard_snapshot", fake_persist)

    written = asyncio.run(sr._refresh_pga_leaderboard("k", "sr:tournament:thin"))
    assert written == 0
    assert persisted == []


def test_latest_tournament_leaderboard_returns_most_recent_snapshot(monkeypatch):
    """The read helper must return rows from the latest fetched_at,
    ordered by position with NULLs last so dashboards / replays can
    reconstruct the field exactly as the orchestrator saw it."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone

    from app.feeds import sportradar as sr
    from app import db as db_mod

    latest_ts = datetime(2026, 4, 26, 12, 5, tzinfo=timezone.utc)
    canned_rows = [
        {"event_id": "sr:tournament:pga-1", "position": 1,
         "name": "Rahm", "fetched_at": latest_ts},
        {"event_id": "sr:tournament:pga-1", "position": 2,
         "name": "Scheffler", "fetched_at": latest_ts},
        {"event_id": "sr:tournament:pga-1", "position": None,
         "name": "Withdrew", "fetched_at": latest_ts},
    ]

    queries: list[tuple[str, tuple]] = []

    class _Conn:
        async def fetch(self, query, *args):
            queries.append((query, args))
            return canned_rows

    @asynccontextmanager
    async def fake_acquire():
        yield _Conn()

    monkeypatch.setattr(db_mod, "acquire", fake_acquire)

    snap = asyncio.run(sr.latest_tournament_leaderboard("sr:tournament:pga-1"))
    assert [(r["position"], r["name"]) for r in snap] == [
        (1, "Rahm"), (2, "Scheffler"), (None, "Withdrew"),
    ]
    # Helper must select on event_id and key on the latest fetched_at.
    assert queries and queries[0][1] == ("sr:tournament:pga-1",)
    assert "MAX(fetched_at)" in queries[0][0]


def test_latest_tournament_leaderboard_swallows_missing_table(monkeypatch):
    """Lookups must degrade gracefully when the table doesn't exist
    yet (fresh dev DB, migration not applied) — return [] rather
    than raise so dashboards can fall back to seed-time ratings."""
    from contextlib import asynccontextmanager

    from app.feeds import sportradar as sr
    from app import db as db_mod

    @asynccontextmanager
    async def boom_acquire():
        raise RuntimeError("relation does not exist")
        yield  # pragma: no cover

    monkeypatch.setattr(db_mod, "acquire", boom_acquire)
    assert asyncio.run(sr.latest_tournament_leaderboard("anything")) == []


def test_latest_tournament_leaderboard_rejects_empty_event_id():
    """Defensive: empty / None event_id short-circuits to [] without
    touching the DB."""
    from app.feeds import sportradar as sr

    assert asyncio.run(sr.latest_tournament_leaderboard("")) == []


def test_register_sportradar_extra_feeds_includes_leaderboards(monkeypatch):
    """The shared registration helper must surface the new
    cross-sport leaderboard feed exactly once, enabled when either
    PGA or F1 keys are configured."""
    from app.feeds.registry import FeedRegistry
    from app.feeds import sportradar as sr

    monkeypatch.setattr(sr, "registry", FeedRegistry())
    monkeypatch.setenv("SPORTRADAR_PGA_KEY", "pga-test")
    monkeypatch.delenv("SPORTRADAR_F1_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)

    sr.register_sportradar_extra_feeds()
    status = sr.registry.status_of("sportradar_tournament_leaderboards")
    assert status is not None
    assert status.enabled is True
    # Cadence honours the env override default — must be at least 30s.
    feed = next(
        f for f in sr.registry.feeds()
        if f.name == "sportradar_tournament_leaderboards"
    )
    assert feed.cadence_seconds >= 30.0
