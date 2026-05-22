"""Tests for the soccer in-game live predictor (task #857).

Covers:
  * Feature builder degrades cleanly when there's no live_state row
    (predictor falls back to the pre-game prior).
  * The blend pulls the result from the pre-game prior at kickoff
    toward the Kalshi mid as minutes elapse.
  * The dispatcher in ``in_game_loop._predict_live_for_market``
    routes soccer leagues to the soccer module and other leagues
    fall through to a None probability.
  * The in-game cycle persists a fresh ``model_yes_prob`` for
    soccer rows when the loop is enabled.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop
from app.learning import soccer_live as _sl


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
# Pure feature-builder + blend tests (no DB)
# ---------------------------------------------------------------------------


def test_is_soccer_league_canonical_names():
    for lg in ("EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL"):
        assert _sl.is_soccer_league(lg)
        assert _sl.is_soccer_league(lg.lower())
    assert not _sl.is_soccer_league("NBA")
    assert not _sl.is_soccer_league(None)
    assert not _sl.is_soccer_league("")


def test_sport_leagues_soccer_matches_canonical_names():
    # The fetch query joins on upper(league) ANY($1) — if these names
    # drift from the canonical SOCCER_LEAGUES the query never matches
    # an actual soccer row and the loop runs empty forever.
    assert set(_loop.SPORT_LEAGUES["soccer"]) == set(_sl.SOCCER_LEAGUES)


def test_build_features_no_live_state_falls_back_to_pregame():
    row = {
        "model_yes_prob": 0.62, "kalshi_price": 0.55,
        "league": "EPL",
    }
    feats = _sl.build_live_features(row=row, live_state=None)
    assert feats["pregame_prob"] == pytest.approx(0.62)
    assert feats["kalshi_price"] == pytest.approx(0.55)
    assert feats["has_live_state"] == 0.0
    assert feats["minutes_elapsed"] == 0.0


def test_build_features_with_live_state_carries_score_and_clock():
    row = {"model_yes_prob": 0.4, "kalshi_price": 0.3, "league": "EPL"}
    live = {
        "status": "2nd_half", "home_score": 2, "away_score": 0,
        "period": "2nd_half", "clock": "30:00",
        "state_age_seconds": 12.0,
    }
    feats = _sl.build_live_features(row=row, live_state=live)
    assert feats["home_score"] == 2.0
    assert feats["away_score"] == 0.0
    assert feats["score_diff"] == 2.0
    # 2nd_half base = 45, clock = 30:00 → 75 minutes elapsed.
    assert feats["minutes_elapsed"] == pytest.approx(75.0)
    assert feats["minutes_fraction"] == pytest.approx(75.0 / 90.0)
    assert feats["has_live_state"] == 1.0


def test_raw_probability_at_kickoff_is_pregame_prior():
    feats = _sl.build_live_features(
        row={"model_yes_prob": 0.7, "kalshi_price": 0.5, "league": "EPL"},
        live_state={
            "home_score": 0, "away_score": 0,
            "period": "1st_half", "clock": "00:00",
            "state_age_seconds": 5.0,
        },
    )
    p = _sl._raw_live_probability(feats)
    # At minute 0 the blend weight is 0 → posterior == pre-game prior.
    assert p == pytest.approx(0.7)


def test_raw_probability_late_match_pulls_toward_market():
    feats = _sl.build_live_features(
        row={"model_yes_prob": 0.7, "kalshi_price": 0.2, "league": "EPL"},
        live_state={
            "home_score": 0, "away_score": 1,
            "period": "2nd_half", "clock": "44:00",
            "state_age_seconds": 5.0,
        },
    )
    p = _sl._raw_live_probability(feats)
    # 89 minutes elapsed → blend weight ~ 0.99 * 0.85 = 0.84.
    # 0.16 * 0.7 + 0.84 * 0.2 = 0.112 + 0.168 = 0.28.
    # Score gap kicker: |diff|=1, market=0.2 (<0.5) → push DOWN by
    # min(0.06, 0.02 * 1) * fraction ≈ 0.0198.
    # Expected ~ 0.28 - 0.02 ≈ 0.26.
    assert 0.20 <= p <= 0.32
    assert p < 0.7  # market pulled the posterior way down from the prior


def test_raw_probability_no_signal_returns_none():
    # No pre-game prior AND no market price → caller should treat as
    # "no live update this cycle".
    feats = _sl.build_live_features(
        row={"model_yes_prob": None, "kalshi_price": None, "league": "EPL"},
        live_state=None,
    )
    assert _sl._raw_live_probability(feats) is None


def test_parse_clock_minutes_robust_to_garbage():
    assert _sl._parse_clock_minutes(None) == 0.0
    assert _sl._parse_clock_minutes("") == 0.0
    assert _sl._parse_clock_minutes("not_a_clock") == 0.0
    assert _sl._parse_clock_minutes("12") == 12.0
    assert _sl._parse_clock_minutes("12:30") == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# Dispatcher tests
# ---------------------------------------------------------------------------


class _PredictorFakeDB:
    """Fake DB exposing only the fetchrow path soccer_live needs."""

    def __init__(self, live_state: dict[str, Any] | None) -> None:
        self.live_state = live_state
        self.fetchrow_calls = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "sportradar_live_state" in query:
            self.fetchrow_calls += 1
            return self.live_state
        return None


def test_dispatcher_routes_soccer_through_soccer_live(monkeypatch):
    fake = _PredictorFakeDB(
        live_state={
            "status": "2nd_half", "home_score": 1, "away_score": 0,
            "period": "2nd_half", "clock": "10:00",
            "state_age_seconds": 4.0,
        }
    )
    monkeypatch.setattr(_sl, "db", fake)

    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-EPL-A", "league": "EPL", "game_id": "g1",
        "model_yes_prob": 0.6, "kalshi_price": 0.55, "is_live": True,
    }))
    assert prob is not None
    assert 0.0 <= prob <= 1.0
    assert meta["source"] == "soccer_live"
    assert meta["league"] == "EPL"
    assert "features" in meta
    assert fake.fetchrow_calls == 1


def test_dispatcher_skips_when_no_signal(monkeypatch):
    fake = _PredictorFakeDB(live_state=None)
    monkeypatch.setattr(_sl, "db", fake)
    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-EPL-A", "league": "EPL", "game_id": "g1",
        "model_yes_prob": None, "kalshi_price": None,
    }))
    assert prob is None
    assert meta["source"] == "soccer_live"
    assert "skipped_reason" in meta


# ---------------------------------------------------------------------------
# Integration: in_game_loop cycle persists soccer live predictions
# ---------------------------------------------------------------------------


class _CycleFakeDB:
    """Fake DB for the full cycle integration test."""

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]],
        live_state: dict[str, Any] | None,
        flipped: int,
    ) -> None:
        self.rows = rows
        self.live_state = live_state
        self.flipped = flipped
        self.update_calls: list[tuple[str, ...]] = []
        self.persist_calls: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "runtime_settings" in query and "live_loop_enabled_" in args[0]:
            return {"value": "true"}
        if "runtime_settings" in query and "live_loop_cadence_" in args[0]:
            return None
        if "sportradar_live_state" in query:
            return self.live_state
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_live_state" in query and "predictions" in query:
            return self.rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        if "UPDATE predictions" in query and "is_live = true" in query:
            self.update_calls.append(args)
            return f"UPDATE {self.flipped}"
        if "UPDATE predictions" in query and "model_yes_prob" in query:
            self.persist_calls.append(args)
            return "UPDATE 1"
        return "OK"


def test_cycle_persists_live_prediction_for_soccer(monkeypatch):
    fake = _CycleFakeDB(
        rows=[{
            "market_id": "KX-EPL-A", "league": "EPL",
            "game_id": "g1", "game_time": None, "is_live": False,
            "model_yes_prob": 0.6, "kalshi_price": 0.45,
            "status": "2nd_half", "state_age_seconds": 5.0,
        }],
        live_state={
            "status": "2nd_half", "home_score": 1, "away_score": 0,
            "period": "2nd_half", "clock": "30:00",
            "state_age_seconds": 5.0,
        },
        flipped=1,
    )
    monkeypatch.setattr(_loop, "db", fake)
    monkeypatch.setattr(_sl, "db", fake)

    r = _run(_loop.run_in_game_cycle("soccer"))
    assert r.enabled is True
    assert r.candidates_scanned == 1
    assert r.candidates_live == 1
    assert r.candidates_tagged == 1
    assert r.live_predictions_computed == 1
    assert r.live_predictions_persisted == 1
    assert len(fake.persist_calls) == 1
    # First UPDATE arg is the market_id, second is the new probability.
    args = fake.persist_calls[0]
    assert args[0] == "KX-EPL-A"
    assert 0.0 <= float(args[1]) <= 1.0


def test_cycle_does_not_persist_for_unwired_sport(monkeypatch):
    fake = _CycleFakeDB(
        rows=[{
            "market_id": "KX-NBA-A", "league": "NBA",
            "game_id": "g1", "game_time": None, "is_live": False,
            "model_yes_prob": 0.6, "kalshi_price": 0.5,
            "status": "inprogress", "state_age_seconds": 5.0,
        }],
        live_state=None,
        flipped=1,
    )
    monkeypatch.setattr(_loop, "db", fake)
    monkeypatch.setattr(_sl, "db", fake)

    r = _run(_loop.run_in_game_cycle("nba"))
    assert r.candidates_tagged == 1
    assert r.live_predictions_computed == 0
    assert r.live_predictions_persisted == 0
    assert fake.persist_calls == []
