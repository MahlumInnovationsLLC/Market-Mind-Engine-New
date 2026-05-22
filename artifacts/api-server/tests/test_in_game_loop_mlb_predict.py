"""Tests for the MLB live in-game prediction wiring (task #881).

Mirrors ``tests/test_in_game_loop_nba_predict.py``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop
from app.learning import mlb_live_features as _mlb


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


def test_predict_returns_none_for_non_mlb():
    result = _run(_loop._predict_live_for_market_meta(
        {"market_id": "KX-WNBA-A", "league": "WNBA"}
    ))
    assert result is None


def test_predict_mlb_calls_predictor(monkeypatch):
    from app import predictor as _predictor

    class _FakePred:
        probability = 0.59
        confidence = 0.4
        model_name = "test-model"
        model_version = "v0"

    async def _fake_predict(features, *, league=None):
        assert league == "MLB"
        return _FakePred()

    monkeypatch.setattr(_predictor, "predict", _fake_predict)

    row = {
        "market_id": "KX-MLB-X",
        "league": "MLB",
        "game_id": "mlb-g1",
        "model_yes_prob": 0.52,
        "kalshi_price": 0.50,
        "home_score": 5,
        "away_score": 3,
        "period": "inning_7",
        "clock": "",
        "status": "inprogress",
    }
    out = _run(_loop._predict_live_for_market_meta(row))
    assert out is not None
    assert out["probability"] == pytest.approx(0.59)
    assert out["feature_inputs"]["score_diff"] == 2
    assert out["feature_inputs"]["period_idx"] == 7
    assert 0.0 < out["live_anchor"] < 1.0


def test_predict_returns_none_when_predictor_unavailable(monkeypatch):
    from app import predictor as _predictor

    async def _raise(features, *, league=None):
        raise _predictor.PredictorUnavailable("no model loaded")

    monkeypatch.setattr(_predictor, "predict", _raise)

    row = {
        "market_id": "KX-MLB-Y", "league": "MLB",
        "model_yes_prob": 0.5, "kalshi_price": 0.5,
        "home_score": 0, "away_score": 0,
        "period": "1", "clock": "", "status": "inprogress",
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


def _mlb_row(market_id="KX-MLB-A", is_live=False):
    return {
        "market_id": market_id,
        "league": "MLB",
        "game_id": "mlb-g1",
        "game_time": None,
        "is_live": is_live,
        "model_yes_prob": 0.55,
        "kalshi_price": 0.50,
        "status": "inprogress",
        "home_score": 5,
        "away_score": 3,
        "period": "inning_7",
        "clock": "",
        "state_age_seconds": 5.0,
    }


def test_cycle_predicts_and_persists_for_mlb(monkeypatch):
    fake = _FakeDB(rows=[_mlb_row("KX-MLB-A")], stale_age_seconds=5.0)
    monkeypatch.setattr(_loop, "db", fake)
    from app.risk import gates as _gates
    monkeypatch.setattr(_gates, "db", fake)
    from app import predictor as _predictor

    class _P:
        probability = 0.62
        confidence = 0.4
        model_name = "m"
        model_version = "v0"

    async def _pred(features, *, league=None):
        return _P()

    monkeypatch.setattr(_predictor, "predict", _pred)

    result = _run(_loop.run_in_game_cycle("mlb"))
    assert result.enabled is True
    assert result.candidates_scanned == 1
    assert result.predictions_made == 1
    assert result.gate_rejections == 0
    assert result.predictions_persisted == 1
    assert fake.persisted_probs == [("KX-MLB-A", pytest.approx(0.62))]


def test_cycle_skips_persist_when_live_state_stale(monkeypatch):
    fake = _FakeDB(rows=[_mlb_row("KX-MLB-A", is_live=True)],
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

    result = _run(_loop.run_in_game_cycle("mlb"))
    assert result.predictions_made == 1
    assert result.gate_rejections == 1
    assert result.predictions_persisted == 0
    assert fake.persisted_probs == []


def test_period_parser_handles_common_shapes():
    assert _mlb._parse_period(None) == 1
    assert _mlb._parse_period("") == 1
    assert _mlb._parse_period("1") == 1
    assert _mlb._parse_period("inning_5") == 5
    assert _mlb._parse_period("top_3") == 3
    assert _mlb._parse_period("bottom_8") == 8
    assert _mlb._parse_period("extras") == 10


def test_score_diff_anchor_blends_with_pre_game_prior():
    # 1st inning tied with prior 0.55 → near prior (period weight tiny).
    p = _mlb._score_diff_yes_prob(score_diff=0, period_idx=1, pre_game_prob=0.55)
    assert 0.50 < p < 0.60
    # 4-run lead in 9th → anchor dominates and pushes high.
    p = _mlb._score_diff_yes_prob(score_diff=4, period_idx=9, pre_game_prob=0.30)
    assert p > 0.75
    # 3-run deficit in 8th → low.
    p_lo = _mlb._score_diff_yes_prob(score_diff=-3, period_idx=8, pre_game_prob=0.55)
    assert p_lo < 0.30


def test_build_mlb_live_candidate_carries_live_state():
    row = {
        "market_id": "KX-MLB-Z", "league": "MLB",
        "model_yes_prob": 0.45, "kalshi_price": 0.52,
        "game_id": "mlb-g99",
    }
    live_state = {"home_score": 7, "away_score": 4,
                  "period": "inning_8", "clock": "", "status": "inprogress"}
    cand = _mlb.build_mlb_live_candidate(row, live_state=live_state)
    assert cand["league"] == "MLB"
    assert cand["market_type"] == "moneyline"
    assert cand["live_state"]["score_diff"] == 3
    assert cand["live_state"]["period_idx"] == 8
    # 3-run lead in 8th with 0.45 prior pushes anchor above 0.5.
    assert cand["consensus_probability"] > 0.55
