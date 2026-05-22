"""Tests for the NBA in-game live predictor (task #878).

Covers:
  * Feature builder degrades cleanly when there's no live_state row
    (predictor falls back to the pre-game prior).
  * NBA-specific clock parsing: the Sportradar feed counts DOWN within
    each quarter, opposite to soccer's count-up convention.
  * The blend pulls the result from the pre-game prior at tip-off
    toward the Kalshi mid as game minutes elapse.
  * The dispatcher in ``in_game_loop._predict_live_for_market`` routes
    NBA rows to the NBA module while leaving the soccer path intact.
  * The in-game cycle persists a fresh ``model_yes_prob`` for NBA
    rows when the loop is enabled.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop
from app.learning import nba_live as _nl


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


def test_is_nba_league_canonical_names():
    assert _nl.is_nba_league("NBA")
    assert _nl.is_nba_league("nba")
    assert not _nl.is_nba_league("WNBA")
    assert not _nl.is_nba_league("EPL")
    assert not _nl.is_nba_league(None)
    assert not _nl.is_nba_league("")


def test_sport_leagues_nba_matches_canonical_names():
    # The fetch query joins on upper(league) ANY($1) — if these names
    # drift the loop runs empty forever.
    assert set(_loop.SPORT_LEAGUES["nba"]) == set(_nl.NBA_LEAGUES)


def test_build_features_no_live_state_falls_back_to_pregame():
    row = {"model_yes_prob": 0.62, "kalshi_price": 0.55, "league": "NBA"}
    feats = _nl.build_live_features(row=row, live_state=None)
    assert feats["pregame_prob"] == pytest.approx(0.62)
    assert feats["kalshi_price"] == pytest.approx(0.55)
    assert feats["has_live_state"] == 0.0
    assert feats["minutes_elapsed"] == 0.0


def test_build_features_with_live_state_carries_score_and_clock():
    # NBA clock counts DOWN: "07:00" remaining in Q3 = 5 minutes into Q3
    # → 24 (Q1+Q2) + 5 = 29 minutes elapsed.
    row = {"model_yes_prob": 0.4, "kalshi_price": 0.3, "league": "NBA"}
    live = {
        "status": "inprogress", "home_score": 60, "away_score": 55,
        "period": "3", "clock": "07:00", "state_age_seconds": 8.0,
    }
    feats = _nl.build_live_features(row=row, live_state=live)
    assert feats["home_score"] == 60.0
    assert feats["away_score"] == 55.0
    assert feats["score_diff"] == 5.0
    assert feats["minutes_elapsed"] == pytest.approx(29.0)
    assert feats["minutes_fraction"] == pytest.approx(29.0 / 48.0)
    assert feats["has_live_state"] == 1.0


def test_minutes_elapsed_overtime_pushes_past_regulation():
    # 1st OT, 02:30 remaining → 48 (regulation) + (5 - 2.5) = 50.5.
    minutes = _nl._estimate_minutes_elapsed("ot", "02:30")
    assert minutes == pytest.approx(50.5)


def test_minutes_elapsed_halftime_pins_to_end_of_q2():
    # Halftime = end of Q2 = 24 minutes elapsed, regardless of what
    # the feed puts in the clock field during the break (sometimes
    # 0:00, sometimes a Q3 prep clock, sometimes missing entirely).
    assert _nl._estimate_minutes_elapsed("halftime", "12:00") == pytest.approx(24.0)
    assert _nl._estimate_minutes_elapsed("halftime", "00:00") == pytest.approx(24.0)
    assert _nl._estimate_minutes_elapsed("halftime", None) == pytest.approx(24.0)
    assert _nl._estimate_minutes_elapsed("halftime", "garbage") == pytest.approx(24.0)
    assert _nl._estimate_minutes_elapsed("HT", "") == pytest.approx(24.0)


def test_minutes_elapsed_handles_long_form_period():
    # "2nd_quarter" with 04:30 remaining → 12 + (12 - 4.5) = 19.5.
    minutes = _nl._estimate_minutes_elapsed("2nd_quarter", "04:30")
    assert minutes == pytest.approx(19.5)


def test_raw_probability_at_tipoff_is_pregame_prior():
    feats = _nl.build_live_features(
        row={"model_yes_prob": 0.7, "kalshi_price": 0.5, "league": "NBA"},
        live_state={
            "home_score": 0, "away_score": 0,
            "period": "1", "clock": "12:00", "state_age_seconds": 5.0,
        },
    )
    p = _nl._raw_live_probability(feats)
    # 0 minutes elapsed → blend weight = 0 → posterior == pre-game prior.
    assert p == pytest.approx(0.7)


def test_raw_probability_late_game_pulls_toward_market():
    feats = _nl.build_live_features(
        row={"model_yes_prob": 0.7, "kalshi_price": 0.2, "league": "NBA"},
        live_state={
            # 4th quarter, 30 seconds remaining → ~47.5 of 48 minutes.
            "home_score": 95, "away_score": 110,
            "period": "4", "clock": "00:30", "state_age_seconds": 5.0,
        },
    )
    p = _nl._raw_live_probability(feats)
    # fraction ≈ 0.99 → blend weight ≈ 0.84.
    # 0.16 * 0.7 + 0.84 * 0.2 ≈ 0.28; score-gap kicker pushes further down.
    assert p < 0.4
    assert p < 0.7  # market pulled the posterior way down from the prior


def test_raw_probability_no_signal_returns_none():
    feats = _nl.build_live_features(
        row={"model_yes_prob": None, "kalshi_price": None, "league": "NBA"},
        live_state=None,
    )
    assert _nl._raw_live_probability(feats) is None


def test_parse_clock_remaining_robust_to_garbage():
    assert _nl._parse_clock_remaining_minutes(None) == 0.0
    assert _nl._parse_clock_remaining_minutes("") == 0.0
    assert _nl._parse_clock_remaining_minutes("garbage") == 0.0
    assert _nl._parse_clock_remaining_minutes("7") == 7.0
    assert _nl._parse_clock_remaining_minutes("07:30") == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# Dispatcher tests
# ---------------------------------------------------------------------------


class _PredictorFakeDB:
    """Fake DB exposing only the fetchrow path nba_live needs."""

    def __init__(self, live_state: dict[str, Any] | None) -> None:
        self.live_state = live_state
        self.fetchrow_calls = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "sportradar_live_state" in query:
            self.fetchrow_calls += 1
            return self.live_state
        return None


def test_dispatcher_routes_nba_through_nba_live(monkeypatch):
    fake = _PredictorFakeDB(
        live_state={
            "status": "inprogress", "home_score": 50, "away_score": 48,
            "period": "2", "clock": "06:00", "state_age_seconds": 4.0,
        }
    )
    monkeypatch.setattr(_nl, "db", fake)

    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-NBA-A", "league": "NBA", "game_id": "g1",
        "model_yes_prob": 0.6, "kalshi_price": 0.55, "is_live": True,
    }))
    assert prob is not None
    assert 0.0 <= prob <= 1.0
    assert meta["source"] == "nba_live"
    assert meta["league"] == "NBA"
    assert "features" in meta
    assert fake.fetchrow_calls == 1


def test_dispatcher_skips_nba_when_no_signal(monkeypatch):
    fake = _PredictorFakeDB(live_state=None)
    monkeypatch.setattr(_nl, "db", fake)
    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-NBA-A", "league": "NBA", "game_id": "g1",
        "model_yes_prob": None, "kalshi_price": None,
    }))
    assert prob is None
    assert meta["source"] == "nba_live"
    assert "skipped_reason" in meta


def test_dispatcher_still_returns_none_for_unwired_league():
    # Sanity: an unrelated league (e.g. NHL) keeps falling through.
    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-NHL-A", "league": "NHL", "game_id": "g1",
        "model_yes_prob": 0.6, "kalshi_price": 0.55,
    }))
    assert prob is None
    assert meta["source"] is None


# ---------------------------------------------------------------------------
# Integration: in_game_loop cycle persists NBA live predictions
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


def test_cycle_persists_live_prediction_for_nba(monkeypatch):
    fake = _CycleFakeDB(
        rows=[{
            "market_id": "KX-NBA-A", "league": "NBA",
            "game_id": "g1", "game_time": None, "is_live": False,
            "model_yes_prob": 0.6, "kalshi_price": 0.45,
            "status": "inprogress", "state_age_seconds": 5.0,
        }],
        live_state={
            "status": "inprogress", "home_score": 78, "away_score": 70,
            "period": "3", "clock": "03:00", "state_age_seconds": 5.0,
        },
        flipped=1,
    )
    monkeypatch.setattr(_loop, "db", fake)
    monkeypatch.setattr(_nl, "db", fake)

    r = _run(_loop.run_in_game_cycle("nba"))
    assert r.enabled is True
    assert r.candidates_scanned == 1
    assert r.candidates_live == 1
    assert r.candidates_tagged == 1
    assert r.live_predictions_computed == 1
    assert r.live_predictions_persisted == 1
    assert len(fake.persist_calls) == 1
    args = fake.persist_calls[0]
    assert args[0] == "KX-NBA-A"
    assert 0.0 <= float(args[1]) <= 1.0
