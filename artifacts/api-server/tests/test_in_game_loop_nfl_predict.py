"""Tests for the NFL live in-game prediction wiring (task #881).

Mirrors ``tests/test_in_game_loop_nba_predict.py``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop
from app.learning import nfl_live_features as _nfl


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop._reset_state_for_tests()
    yield
    loop.close()
    _loop._reset_state_for_tests()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_predict_returns_none_for_non_nfl():
    result = _run(_loop._predict_live_for_market_meta(
        {"market_id": "KX-XFL-A", "league": "XFL"}
    ))
    assert result is None


def test_predict_nfl_calls_predictor(monkeypatch):
    from app import predictor as _predictor

    class _FakePred:
        probability = 0.63
        confidence = 0.4
        model_name = "test-model"
        model_version = "v0"

    async def _fake_predict(features, *, league=None):
        assert league == "NFL"
        return _FakePred()

    monkeypatch.setattr(_predictor, "predict", _fake_predict)

    row = {
        "market_id": "KX-NFL-X",
        "league": "NFL",
        "game_id": "nfl-g1",
        "model_yes_prob": 0.55,
        "kalshi_price": 0.50,
        "home_score": 21,
        "away_score": 14,
        "period": "4th_quarter",
        "clock": "5:00",
        "status": "inprogress",
    }
    out = _run(_loop._predict_live_for_market_meta(row))
    assert out is not None
    assert out["probability"] == pytest.approx(0.63)
    assert out["feature_inputs"]["score_diff"] == 7
    assert out["feature_inputs"]["period_idx"] == 4
    assert 0.0 < out["live_anchor"] < 1.0


def test_predict_returns_none_when_predictor_unavailable(monkeypatch):
    from app import predictor as _predictor

    async def _raise(features, *, league=None):
        raise _predictor.PredictorUnavailable("no model loaded")

    monkeypatch.setattr(_predictor, "predict", _raise)

    row = {
        "market_id": "KX-NFL-Y", "league": "NFL",
        "model_yes_prob": 0.5, "kalshi_price": 0.5,
        "home_score": 0, "away_score": 0,
        "period": "1", "clock": "15:00", "status": "inprogress",
    }
    assert _run(_loop._predict_live_for_market_meta(row)) is None


class _FakeDB:
    def __init__(self, *, rows, stale_age_seconds=5.0):
        self.rows = rows
        self.stale_age_seconds = stale_age_seconds
        self.persisted_probs: list[tuple[str, float]] = []

    async def fetchrow(self, query: str, *args: Any):
        if "runtime_settings" in query:
            key = args[0] if args else ""
            if "live_loop_enabled_" in key:
                return {"value": "true"}
            if "live_loop_cadence_" in key:
                return {"value": "0.001"}
            return None
        if "sportradar_live_state" in query and "ORDER BY fetched_at" in query:
            return {"age_seconds": float(self.stale_age_seconds),
                    "status": "inprogress"}
        return None

    async def fetch(self, query: str, *args: Any):
        if "sportradar_live_state" in query:
            return self.rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.lower()
        if "update predictions" in q and "is_live = true" in q and "model_yes_prob" in q:
            self.persisted_probs.append((args[0], float(args[1])))
            return "UPDATE 1"
        if "update predictions" in q and "is_live = true" in q:
            return f"UPDATE {len(args[0]) if args else 0}"
        return "OK"


def _nfl_row(market_id="KX-NFL-A", is_live=False):
    return {
        "market_id": market_id, "league": "NFL", "game_id": "nfl-g1",
        "game_time": None, "is_live": is_live,
        "model_yes_prob": 0.55, "kalshi_price": 0.50,
        "status": "inprogress",
        "home_score": 21, "away_score": 14,
        "period": "4th_quarter", "clock": "5:00",
        "state_age_seconds": 5.0,
    }


def test_cycle_predicts_and_persists_for_nfl(monkeypatch):
    fake = _FakeDB(rows=[_nfl_row("KX-NFL-A")], stale_age_seconds=5.0)
    monkeypatch.setattr(_loop, "db", fake)
    from app.risk import gates as _gates
    monkeypatch.setattr(_gates, "db", fake)
    from app import predictor as _predictor

    class _P:
        probability = 0.61
        confidence = 0.4
        model_name = "m"
        model_version = "v0"

    async def _pred(features, *, league=None):
        return _P()

    monkeypatch.setattr(_predictor, "predict", _pred)

    result = _run(_loop.run_in_game_cycle("nfl"))
    assert result.enabled is True
    assert result.candidates_scanned == 1
    assert result.predictions_made == 1
    assert result.gate_rejections == 0
    assert result.predictions_persisted == 1
    assert fake.persisted_probs == [("KX-NFL-A", pytest.approx(0.61))]


def test_cycle_skips_persist_when_live_state_stale(monkeypatch):
    fake = _FakeDB(rows=[_nfl_row("KX-NFL-A", is_live=True)],
                   stale_age_seconds=9999.0)
    monkeypatch.setattr(_loop, "db", fake)
    from app.risk import gates as _gates
    monkeypatch.setattr(_gates, "db", fake)
    from app import predictor as _predictor

    class _P:
        probability = 0.7
        confidence = 0.3
        model_name = "m"
        model_version = "v0"

    async def _pred(features, *, league=None):
        return _P()

    monkeypatch.setattr(_predictor, "predict", _pred)

    result = _run(_loop.run_in_game_cycle("nfl"))
    assert result.predictions_made == 1
    assert result.gate_rejections == 1
    assert result.predictions_persisted == 0
    assert fake.persisted_probs == []


def test_period_parser_handles_common_shapes():
    assert _nfl._parse_period(None) == 1
    assert _nfl._parse_period("") == 1
    assert _nfl._parse_period("4th_quarter") == 4
    assert _nfl._parse_period("OT") == 5
    assert _nfl._parse_period("overtime") == 5
    assert _nfl._parse_period("2nd_half") == 3


def test_score_diff_anchor_blends_with_pre_game_prior():
    p = _nfl._score_diff_yes_prob(score_diff=0, period_idx=1, pre_game_prob=0.60)
    assert 0.50 < p < 0.65
    p = _nfl._score_diff_yes_prob(score_diff=21, period_idx=5, pre_game_prob=0.30)
    assert p > 0.85
    p_lo = _nfl._score_diff_yes_prob(score_diff=-17, period_idx=4, pre_game_prob=0.55)
    assert p_lo < 0.20


def test_build_nfl_live_candidate_carries_live_state():
    row = {"market_id": "KX-NFL-Z", "league": "NFL",
           "model_yes_prob": 0.45, "kalshi_price": 0.52, "game_id": "nfl-g99"}
    live_state = {"home_score": 24, "away_score": 17,
                  "period": "4th_quarter", "clock": "1:23",
                  "status": "inprogress"}
    cand = _nfl.build_nfl_live_candidate(row, live_state=live_state)
    assert cand["league"] == "NFL"
    assert cand["market_type"] == "moneyline"
    assert cand["live_state"]["score_diff"] == 7
    assert cand["live_state"]["period_idx"] == 4
    # 7-point lead in 4th with 0.45 prior pushes anchor above 0.5.
    assert cand["consensus_probability"] > 0.55
