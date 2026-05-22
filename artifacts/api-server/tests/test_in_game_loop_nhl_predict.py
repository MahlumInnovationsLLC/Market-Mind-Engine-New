"""Tests for the NHL live in-game prediction wiring (task #881).

Mirrors ``tests/test_in_game_loop_nba_predict.py``: per-sport coverage
of the live-feature builder, the meta-model dispatcher, and the
end-to-end ``run_in_game_cycle("nhl")`` path including the live-only
gate chain.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop
from app.learning import nhl_live_features as _nhl


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


def test_predict_returns_none_for_non_nhl():
    result = _run(_loop._predict_live_for_market_meta(
        {"market_id": "KX-MLB-A", "league": "WNBA"}
    ))
    assert result is None


def test_predict_nhl_calls_predictor(monkeypatch):
    from app import predictor as _predictor

    class _FakePred:
        probability = 0.58
        confidence = 0.4
        model_name = "test-model"
        model_version = "v0"

    async def _fake_predict(features, *, league=None):
        assert league == "NHL", "live loop must request NHL calibration"
        return _FakePred()

    monkeypatch.setattr(_predictor, "predict", _fake_predict)

    row = {
        "market_id": "KX-NHL-X",
        "league": "NHL",
        "game_id": "nhl-g1",
        "model_yes_prob": 0.52,
        "kalshi_price": 0.50,
        "home_score": 3,
        "away_score": 1,
        "period": "3rd_period",
        "clock": "5:00",
        "status": "inprogress",
    }
    out = _run(_loop._predict_live_for_market_meta(row))
    assert out is not None
    assert out["probability"] == pytest.approx(0.58)
    assert out["feature_inputs"]["score_diff"] == 2
    assert out["feature_inputs"]["period_idx"] == 3
    assert 0.0 < out["live_anchor"] < 1.0


def test_predict_returns_none_when_predictor_unavailable(monkeypatch):
    from app import predictor as _predictor

    async def _raise(features, *, league=None):
        raise _predictor.PredictorUnavailable("no model loaded")

    monkeypatch.setattr(_predictor, "predict", _raise)

    row = {
        "market_id": "KX-NHL-Y", "league": "NHL",
        "model_yes_prob": 0.5, "kalshi_price": 0.5,
        "home_score": 0, "away_score": 0,
        "period": "1", "clock": "20:00", "status": "inprogress",
    }
    assert _run(_loop._predict_live_for_market_meta(row)) is None


class _FakeDB:
    def __init__(self, *, rows, stale_age_seconds=5.0, live_state_present=True):
        self.rows = rows
        self.stale_age_seconds = stale_age_seconds
        self.live_state_present = live_state_present
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
            if not self.live_state_present:
                return None
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


def _nhl_row(market_id="KX-NHL-A", is_live=False):
    return {
        "market_id": market_id,
        "league": "NHL",
        "game_id": "nhl-g1",
        "game_time": None,
        "is_live": is_live,
        "model_yes_prob": 0.55,
        "kalshi_price": 0.50,
        "status": "inprogress",
        "home_score": 3,
        "away_score": 1,
        "period": "3rd_period",
        "clock": "5:00",
        "state_age_seconds": 5.0,
    }


def test_cycle_predicts_and_persists_for_nhl(monkeypatch):
    fake = _FakeDB(rows=[_nhl_row("KX-NHL-A")], stale_age_seconds=5.0)
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

    result = _run(_loop.run_in_game_cycle("nhl"))
    assert result.enabled is True
    assert result.candidates_scanned == 1
    assert result.predictions_made == 1
    assert result.gate_rejections == 0
    assert result.predictions_persisted == 1
    assert fake.persisted_probs == [("KX-NHL-A", pytest.approx(0.61))]


def test_cycle_skips_persist_when_live_state_stale(monkeypatch):
    fake = _FakeDB(rows=[_nhl_row("KX-NHL-A", is_live=True)],
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

    result = _run(_loop.run_in_game_cycle("nhl"))
    assert result.predictions_made == 1
    assert result.gate_rejections == 1
    assert result.predictions_persisted == 0
    assert fake.persisted_probs == []


def test_period_parser_handles_common_shapes():
    assert _nhl._parse_period(None) == 1
    assert _nhl._parse_period("") == 1
    assert _nhl._parse_period("1") == 1
    assert _nhl._parse_period("3rd_period") == 3
    assert _nhl._parse_period("OT") == 4
    assert _nhl._parse_period("overtime") == 4
    assert _nhl._parse_period("shootout") == 5


def test_score_diff_anchor_blends_with_pre_game_prior():
    # Tied game in 1st period with a 0.55 prior stays near the prior.
    p = _nhl._score_diff_yes_prob(score_diff=0, period_idx=1, pre_game_prob=0.55)
    assert 0.45 < p < 0.60
    # Two-goal lead in OT dominates and pushes high.
    p = _nhl._score_diff_yes_prob(score_diff=2, period_idx=4, pre_game_prob=0.40)
    assert p > 0.75
    # Two-goal deficit late drives it low.
    p_lo = _nhl._score_diff_yes_prob(score_diff=-2, period_idx=3, pre_game_prob=0.55)
    assert p_lo < 0.25


def test_build_nhl_live_candidate_carries_live_state():
    row = {
        "market_id": "KX-NHL-Z",
        "league": "NHL",
        "model_yes_prob": 0.45,
        "kalshi_price": 0.52,
        "game_id": "nhl-g99",
    }
    live_state = {
        "home_score": 4,
        "away_score": 2,
        "period": "3rd_period",
        "clock": "1:23",
        "status": "inprogress",
    }
    cand = _nhl.build_nhl_live_candidate(row, live_state=live_state)
    assert cand["league"] == "NHL"
    assert cand["market_type"] == "moneyline"
    assert cand["kalshi_price"] == 0.52
    assert cand["live_state"]["score_diff"] == 2
    assert cand["live_state"]["period_idx"] == 3
    # 2-goal lead in 3rd with 0.45 prior must push the anchor above 0.5.
    assert cand["consensus_probability"] > 0.55
