"""Tests for the NBA live in-game prediction wiring (task #855).

Behaviour under test:
  * `_predict_live_for_market` returns None for non-NBA leagues (the
    per-sport rollout: NBA goes first, other sports land later).
  * `_predict_live_for_market` returns a calibrated YES probability
    for an NBA candidate when the predictor is patched.
  * `run_in_game_cycle("nba")` calls the predictor for every live
    NBA candidate, routes the result through the live-only gates
    (gate_stale_live_state), and stamps the calibrated probability
    onto the matching `predictions` row.
  * A stale live-state row (gate rejection) prevents the predicted
    probability from being persisted but still counts as a
    `gate_rejections`.
  * The NBA live feature builder produces the expected score-diff
    anchor (logistic on weighted margin).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop
from app.learning import nba_live_features as _nba


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


# ---------------------------------------------------------------------------
# `_predict_live_for_market` unit tests
# ---------------------------------------------------------------------------


def test_predict_returns_none_for_non_nba(monkeypatch):
    # Task #881 expanded the meta-model dispatcher to NHL/MLB/NFL/
    # NCAAB/NCAAF so this test now exercises a league that is still
    # NOT on the supported list — the dispatcher must short-circuit
    # to None so the loop doesn't accidentally score the row through
    # a builder that wasn't sanity-checked for the league.
    result = _run(_loop._predict_live_for_market_nba(
        {"market_id": "KX-WNBA-A", "league": "WNBA"}
    ))
    assert result is None


def test_predict_nba_calls_predictor(monkeypatch):
    # Patch the predictor's `predict` to return a synthetic result so
    # the test doesn't depend on a real model being loaded.  We
    # monkeypatch the module *after* importing it because the
    # in_game_loop imports it lazily inside `_predict_live_for_market`.
    from app import predictor as _predictor

    class _FakePred:
        probability = 0.62
        confidence = 0.4
        model_name = "test-model"
        model_version = "v0"

    async def _fake_predict(features, *, league=None):
        assert league == "NBA", "live loop must request NBA calibration"
        return _FakePred()

    monkeypatch.setattr(_predictor, "predict", _fake_predict)

    row = {
        "market_id": "KX-NBA-X",
        "league": "NBA",
        "game_id": "nba-g1",
        "model_yes_prob": 0.55,
        "kalshi_price": 0.50,
        "home_score": 72,
        "away_score": 65,
        "period": "3rd_quarter",
        "clock": "5:00",
        "status": "inprogress",
    }
    out = _run(_loop._predict_live_for_market_nba(row))
    assert out is not None
    assert out["probability"] == pytest.approx(0.62)
    assert out["model_name"] == "test-model"
    assert out["feature_inputs"]["score_diff"] == 7
    assert out["feature_inputs"]["period_idx"] == 3
    assert 0.0 < out["live_anchor"] < 1.0


def test_predict_returns_none_when_predictor_unavailable(monkeypatch):
    from app import predictor as _predictor

    async def _raise(features, *, league=None):
        raise _predictor.PredictorUnavailable("no model loaded")

    monkeypatch.setattr(_predictor, "predict", _raise)

    row = {
        "market_id": "KX-NBA-Y",
        "league": "NBA",
        "model_yes_prob": 0.5,
        "kalshi_price": 0.5,
        "home_score": 0, "away_score": 0,
        "period": "1", "clock": "12:00", "status": "inprogress",
    }
    assert _run(_loop._predict_live_for_market_nba(row)) is None


# ---------------------------------------------------------------------------
# End-to-end: run_in_game_cycle("nba") with predictor wired
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]],
        stale_age_seconds: float = 5.0,
        live_state_present: bool = True,
    ) -> None:
        self.rows = rows
        self.stale_age_seconds = stale_age_seconds
        self.live_state_present = live_state_present
        self.updates: list[tuple[str, tuple]] = []
        self.persisted_probs: list[tuple[str, float]] = []

    async def fetchrow(self, query: str, *args: Any):
        # runtime_settings.value lookups
        if "runtime_settings" in query:
            key = args[0] if args else ""
            if "live_loop_enabled_" in key:
                return {"value": "true"}
            if "live_loop_cadence_" in key:
                return {"value": "0.001"}
            return None
        # gate_stale_live_state queries sportradar_live_state for one
        # game.  Return a stale row when configured so the gate rejects.
        if "sportradar_live_state" in query and "ORDER BY fetched_at" in query:
            if not self.live_state_present:
                return None
            return {
                "age_seconds": float(self.stale_age_seconds),
                "status": "inprogress",
            }
        return None

    async def fetch(self, query: str, *args: Any):
        if "sportradar_live_state" in query:
            return self.rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = query.lower()
        if "update predictions" in q and "is_live = true" in q and "model_yes_prob" in q:
            # _persist_live_model_yes_prob path
            self.persisted_probs.append((args[0], float(args[1])))
            return "UPDATE 1"
        if "update predictions" in q and "is_live = true" in q:
            self.updates.append(args)
            return f"UPDATE {len(args[0]) if args else 0}"
        return "OK"


def _nba_row(market_id: str = "KX-NBA-A", is_live: bool = False) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "league": "NBA",
        "game_id": "nba-g1",
        "game_time": None,
        "is_live": is_live,
        "model_yes_prob": 0.55,
        "kalshi_price": 0.50,
        "status": "inprogress",
        "home_score": 72,
        "away_score": 65,
        "period": "3rd_quarter",
        "clock": "5:00",
        "state_age_seconds": 5.0,
    }


def test_cycle_predicts_and_persists_for_nba(monkeypatch):
    rows = [_nba_row("KX-NBA-A", is_live=False)]
    fake = _FakeDB(rows=rows, stale_age_seconds=5.0)
    monkeypatch.setattr(_loop, "db", fake)
    # gate_stale_live_state reads `gates.db`, separate from the loop's
    # `_loop.db` patch — patch both so the test stays self-contained.
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

    result = _run(_loop.run_in_game_cycle("nba"))
    assert result.enabled is True
    assert result.candidates_scanned == 1
    assert result.predictions_made == 1
    assert result.gate_rejections == 0
    assert result.predictions_persisted == 1
    assert fake.persisted_probs == [("KX-NBA-A", pytest.approx(0.61))]


def test_cycle_skips_persist_when_live_state_stale(monkeypatch):
    rows = [_nba_row("KX-NBA-A", is_live=True)]
    # NBA freshness budget is 60s — 9999s definitely trips the gate.
    fake = _FakeDB(rows=rows, stale_age_seconds=9999.0)
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

    result = _run(_loop.run_in_game_cycle("nba"))
    assert result.predictions_made == 1
    assert result.gate_rejections == 1, (
        "stale live-state must reject the in-game prediction"
    )
    assert result.predictions_persisted == 0
    assert fake.persisted_probs == [], (
        "stale-feed rejections must NOT stamp the probability — that's "
        "exactly the failure mode gate_stale_live_state exists to prevent"
    )


# ---------------------------------------------------------------------------
# NBA live-feature builder unit tests
# ---------------------------------------------------------------------------


def test_period_parser_handles_common_shapes():
    assert _nba._parse_period(None) == 1
    assert _nba._parse_period("") == 1
    assert _nba._parse_period("1") == 1
    assert _nba._parse_period("4th_quarter") == 4
    assert _nba._parse_period("OT") == 5
    assert _nba._parse_period("overtime") == 5
    assert _nba._parse_period("2nd_half") == 3


def test_score_diff_anchor_blends_with_pre_game_prior():
    # Tied game in 1st quarter with a pre-game prior of 0.60 -> the
    # blend should stay closer to the prior because the period weight
    # is low.
    p = _nba._score_diff_yes_prob(
        score_diff=0, period_idx=1, pre_game_prob=0.60
    )
    assert 0.50 < p < 0.60, f"expected near prior, got {p}"

    # Big lead in OT -> live anchor dominates and pushes toward 1.0.
    p = _nba._score_diff_yes_prob(
        score_diff=20, period_idx=5, pre_game_prob=0.30
    )
    assert p > 0.90, f"expected anchor-dominant high prob, got {p}"

    # Symmetric: negative diff in 4th quarter -> low prob.
    p_lo = _nba._score_diff_yes_prob(
        score_diff=-15, period_idx=4, pre_game_prob=0.55
    )
    assert p_lo < 0.20, f"expected anchor-dominant low prob, got {p_lo}"


def test_build_nba_live_candidate_carries_live_state():
    row = {
        "market_id": "KX-NBA-Z",
        "league": "NBA",
        "model_yes_prob": 0.45,
        "kalshi_price": 0.52,
        "game_id": "nba-g99",
    }
    live_state = {
        "home_score": 88,
        "away_score": 80,
        "period": "4th_quarter",
        "clock": "1:23",
        "status": "inprogress",
    }
    cand = _nba.build_nba_live_candidate(row, live_state=live_state)
    assert cand["league"] == "NBA"
    assert cand["market_type"] == "moneyline"
    assert cand["kalshi_price"] == 0.52
    assert cand["live_state"]["score_diff"] == 8
    assert cand["live_state"]["period_idx"] == 4
    # 8-point lead in the 4th with a 0.45 prior should push the anchor
    # firmly above 0.5 — the live state has dispositively shifted the
    # implied probability.
    assert cand["consensus_probability"] > 0.55
