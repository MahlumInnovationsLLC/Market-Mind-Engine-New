"""Tests for niche-sport feature builders (task #831).

Covers null-safety, value-range sanity, fixed column order, and the
SPORT_BUILDERS dispatch contract for tennis (ATP+WTA), F1, PGA, UFC.
"""
from __future__ import annotations

import math

import pytest

from app.niche_sports import features as nf


def _assert_finite(values: list[float]) -> None:
    for v in values:
        assert isinstance(v, float)
        assert math.isfinite(v), f"non-finite feature value: {v}"


def test_build_tennis_null_safe_returns_defaults():
    names, values = nf.build_tennis({})
    assert len(names) == len(values)
    _assert_finite(values)
    # surface one-hot all zero when unknown
    assert values[:4] == [0.0, 0.0, 0.0, 0.0]
    # h2h default 0.5, depth default 0.5, retirement default 0.0
    assert values[5] == 0.5
    assert values[6] == 0.5
    assert values[7] == 0.0


def test_build_tennis_surface_one_hot():
    names, values = nf.build_tennis({"surface": "Clay", "h2h_win_rate": 0.7})
    # surface=clay → index 1
    assert values[0] == 0.0 and values[1] == 1.0
    assert values[5] == 0.7


def test_build_tennis_clamps_out_of_range():
    _, values = nf.build_tennis(
        {"surface_elo_diff": 99.0, "h2h_win_rate": 5.0,
         "recent_round_depth": -3.0, "retirement_rate_3mo": 99.0}
    )
    assert values[4] == 2.0
    assert values[5] == 1.0
    assert values[6] == 0.0
    assert values[7] == 1.0


def test_build_tennis_rejects_nan_and_strings():
    _, values = nf.build_tennis(
        {"surface_elo_diff": float("nan"), "h2h_win_rate": "not-a-number"}
    )
    _assert_finite(values)
    # nan/string → defaults (0.0 for elo, 0.5 for h2h)
    assert values[4] == 0.0
    assert values[5] == 0.5


def test_build_f1_null_safe():
    names, values = nf.build_f1({})
    assert len(names) == len(values) == 7
    _assert_finite(values)
    assert values[:3] == [0.0, 0.0, 0.0]


def test_build_f1_clamps_grid_delta():
    _, values = nf.build_f1({"grid_vs_quali_delta": 999.0,
                             "fp3_long_run_pace": -99.0})
    assert values[3] == 20.0
    assert values[4] == -3.0


def test_build_pga_null_safe():
    names, values = nf.build_pga({})
    assert len(names) == len(values) == 10
    _assert_finite(values)
    # default made_cuts=0.5, top10=0.1
    assert values[4] == 0.5
    assert values[5] == 0.1


def test_build_pga_green_one_hot():
    _, values = nf.build_pga({"green_type": "POA"})
    assert values[:4] == [0.0, 0.0, 1.0, 0.0]


def test_build_ufc_null_safe():
    names, values = nf.build_ufc({})
    # 9 weight classes + 5 numeric = 14
    assert len(names) == len(values) == 14
    _assert_finite(values)
    # default td_defense_rate = 0.5
    assert values[10] == 0.5


def test_build_ufc_weight_one_hot_and_clamp():
    _, values = nf.build_ufc(
        {"weight_class": "Heavyweight", "reach_diff_inches": 99.0,
         "days_since_last_fight": 5000.0}
    )
    # heavyweight is index 8
    assert values[8] == 1.0
    assert values[9] == 12.0  # reach clamped to 12
    # days normalised to [0,1]
    assert values[13] == 1.0


def test_sport_builders_map_dispatches():
    assert nf.SPORT_BUILDERS["ATP"] is nf.build_tennis
    assert nf.SPORT_BUILDERS["WTA"] is nf.build_tennis
    assert nf.SPORT_BUILDERS["F1"] is nf.build_f1
    assert nf.SPORT_BUILDERS["PGA"] is nf.build_pga
    assert nf.SPORT_BUILDERS["UFC"] is nf.build_ufc


def test_build_for_league_unknown_returns_empty():
    names, values = nf.build_for_league("CRICKET", {})
    assert names == [] and values == []


def test_build_for_league_dispatch_case_insensitive():
    names_lo, values_lo = nf.build_for_league("atp", {})
    names_hi, values_hi = nf.build_for_league("ATP", {})
    assert names_lo == names_hi
    assert values_lo == values_hi
