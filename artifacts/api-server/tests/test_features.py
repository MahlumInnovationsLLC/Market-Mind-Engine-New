"""Tests for the live feature builder."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.orchestrator.features import (
    FEATURE_COUNT,
    FEATURE_NAMES,
    build_features,
)


SAT_AFTERNOON = datetime(2026, 4, 18, 18, 30, tzinfo=timezone.utc)  # weekend, prime
TUE_MORNING = datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc)      # weekday, off-prime


def test_feature_vector_has_fixed_width():
    fv = build_features({}, now=TUE_MORNING)
    assert len(fv.values) == FEATURE_COUNT == 32
    assert fv.names == FEATURE_NAMES


def test_defaults_when_candidate_is_empty():
    fv = build_features({}, now=TUE_MORNING)
    d = fv.as_dict()
    assert d["pred_prob"] == 0.5
    assert d["market_prob"] == 0.5
    assert d["edge"] == 0.0          # 0.5 - 0.5
    assert d["signal_strength"] == 0.0
    assert d["kelly"] == 0.25
    assert d["side_val"] == 1.0      # default 'yes'
    assert d["league_idx_norm"] == 1.0   # unknown -> 7/7
    assert d["market_type_idx_norm"] == 1.0
    assert d["is_weekend"] == 0.0
    assert d["is_prime"] == 0.0      # 8am UTC
    assert d["reserved_0"] == 0.0
    assert d["reserved_1"] == 0.0
    # No upstream signals were "really" present.
    assert fv.present == {}


def test_named_signals_flow_through_for_cached_debate():
    cand = {
        "consensus_probability": 0.62,
        "agreement_rate": 0.88,
        "league": "NBA",
        "market_type": "Winner",
        "recommended_action": "BUY_NO",
        "game_context": {"kalshi_market_id": "M-1", "kalshi_price": 0.55},
    }
    fv = build_features(cand, now=SAT_AFTERNOON)
    d = fv.as_dict()
    assert d["pred_prob"] == pytest.approx(0.62)
    assert d["market_prob"] == pytest.approx(0.55)
    assert d["edge"] == pytest.approx(0.07)              # 0.62 - 0.55
    assert d["signal_strength"] == pytest.approx(0.88)
    assert d["side_val"] == 0.0                          # BUY_NO
    assert d["league_idx_norm"] == pytest.approx(0.0)    # NBA -> 0
    assert d["market_type_idx_norm"] == pytest.approx(0.0)
    assert d["is_weekend"] == 1.0
    assert d["is_prime"] == 1.0
    # Derived expressions match training-time formulas.
    assert d["prob_edge"] == pytest.approx(0.62 * 0.07)
    assert d["edge_abs"] == pytest.approx(0.07)
    assert d["prob_dev"] == pytest.approx(0.12)
    assert d["market_dev"] == pytest.approx(0.05)
    assert d["prob_market_gap"] == pytest.approx(0.07)
    assert "consensus_probability" in fv.present
    assert "kalshi_price" in fv.present


def test_consensus_kwarg_overrides_candidate_field():
    cand = {"consensus_probability": 0.30, "game_context": {"kalshi_price": 0.40}}
    fv = build_features(cand, consensus=0.71, current_price=0.55, edge=0.16)
    d = fv.as_dict()
    assert d["pred_prob"] == pytest.approx(0.71)
    assert d["market_prob"] == pytest.approx(0.55)
    assert d["edge"] == pytest.approx(0.16)


def test_market_vs_model_inferred_when_label_missing():
    # market well above model -> market_higher (+1)
    fv_high = build_features(
        {"consensus_probability": 0.40},
        current_price=0.55,
    )
    assert fv_high.as_dict()["market_vs_model_val"] == 1.0
    # market well below model -> market_lower (-1)
    fv_low = build_features(
        {"consensus_probability": 0.60},
        current_price=0.45,
    )
    assert fv_low.as_dict()["market_vs_model_val"] == -1.0
    # within ±2pp -> aligned (0)
    fv_mid = build_features(
        {"consensus_probability": 0.50},
        current_price=0.51,
    )
    assert fv_mid.as_dict()["market_vs_model_val"] == 0.0


def test_explicit_market_vs_model_label_wins():
    fv = build_features(
        {
            "consensus_probability": 0.30,
            "market_vs_model_direction": "market_higher",
        },
        current_price=0.10,  # would otherwise infer market_lower
    )
    assert fv.as_dict()["market_vs_model_val"] == 1.0


def test_string_game_context_is_decoded():
    cand = {
        "consensus_probability": 0.60,
        "game_context": '{"kalshi_price": 0.42, "league": "NHL"}',
    }
    fv = build_features(cand)
    d = fv.as_dict()
    assert d["market_prob"] == pytest.approx(0.42)
    # NHL idx = 2 -> normalized 2/7
    assert d["league_idx_norm"] == pytest.approx(2 / 7)


def test_garbage_game_context_does_not_raise():
    fv = build_features({"game_context": "not-json"})
    assert len(fv.values) == FEATURE_COUNT


def test_zero_consensus_is_not_treated_as_missing():
    # A legitimate 0.0 must NOT be defaulted away to 0.5.
    fv = build_features({"consensus_probability": 0.0}, current_price=0.5)
    assert fv.as_dict()["pred_prob"] == 0.0


def test_zero_kalshi_price_is_not_treated_as_missing():
    fv = build_features(
        {"consensus_probability": 0.5, "game_context": {"kalshi_price": 0.0}}
    )
    assert fv.as_dict()["market_prob"] == 0.0


def test_zero_consensus_kwarg_is_not_treated_as_missing():
    fv = build_features({}, consensus=0.0, current_price=0.0)
    d = fv.as_dict()
    assert d["pred_prob"] == 0.0
    assert d["market_prob"] == 0.0
