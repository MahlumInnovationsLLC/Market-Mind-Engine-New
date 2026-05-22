"""Tests for the NHL in-game live predictor (task #885).

Covers:
  * Feature builder degrades cleanly when there's no live_state row
    (predictor falls back to the pre-game prior).
  * NHL-specific clock parsing: the Sportradar feed counts DOWN within
    each period, same as NBA, opposite to soccer's count-up convention.
  * The blend pulls the result from the pre-game prior at puck-drop
    toward the Kalshi mid as game minutes elapse.
  * The dispatcher in ``in_game_loop._predict_live_for_market`` routes
    NHL rows to the NHL module while leaving the soccer + NBA paths
    intact.
  * The in-game cycle persists a fresh ``model_yes_prob`` for NHL
    rows when the loop is enabled.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop
from app.learning import nhl_live as _nl


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


def test_is_nhl_league_canonical_names():
    assert _nl.is_nhl_league("NHL")
    assert _nl.is_nhl_league("nhl")
    assert not _nl.is_nhl_league("NBA")
    assert not _nl.is_nhl_league("EPL")
    assert not _nl.is_nhl_league(None)
    assert not _nl.is_nhl_league("")


def test_sport_leagues_nhl_matches_canonical_names():
    assert set(_loop.SPORT_LEAGUES["nhl"]) == set(_nl.NHL_LEAGUES)


def test_build_features_no_live_state_falls_back_to_pregame():
    row = {"model_yes_prob": 0.62, "kalshi_price": 0.55, "league": "NHL"}
    feats = _nl.build_live_features(row=row, live_state=None)
    assert feats["pregame_prob"] == pytest.approx(0.62)
    assert feats["kalshi_price"] == pytest.approx(0.55)
    assert feats["has_live_state"] == 0.0
    assert feats["minutes_elapsed"] == 0.0


def test_build_features_with_live_state_carries_score_and_clock():
    # NHL clock counts DOWN: "15:00" remaining in P2 = 5 minutes into P2
    # → 20 (P1) + 5 = 25 minutes elapsed.
    row = {"model_yes_prob": 0.4, "kalshi_price": 0.3, "league": "NHL"}
    live = {
        "status": "inprogress", "home_score": 2, "away_score": 1,
        "period": "2", "clock": "15:00", "state_age_seconds": 8.0,
    }
    feats = _nl.build_live_features(row=row, live_state=live)
    assert feats["home_score"] == 2.0
    assert feats["away_score"] == 1.0
    assert feats["score_diff"] == 1.0
    assert feats["minutes_elapsed"] == pytest.approx(25.0)
    assert feats["minutes_fraction"] == pytest.approx(25.0 / 60.0)
    assert feats["has_live_state"] == 1.0


def test_minutes_elapsed_overtime_pushes_past_regulation():
    # 1st OT, 02:30 remaining → 60 (regulation) + (5 - 2.5) = 62.5.
    minutes = _nl._estimate_minutes_elapsed("ot", "02:30")
    assert minutes == pytest.approx(62.5)


def test_minutes_elapsed_intermission_pins_to_end_of_prior_period():
    # Intermission 1 = end of P1 = 20 minutes, regardless of clock.
    assert _nl._estimate_minutes_elapsed("intermission", "20:00") == pytest.approx(20.0)
    assert _nl._estimate_minutes_elapsed("intermission1", None) == pytest.approx(20.0)
    assert _nl._estimate_minutes_elapsed("intermission2", "") == pytest.approx(40.0)


def test_minutes_elapsed_handles_long_form_period():
    # "3rd_period" with 04:30 remaining → 40 + (20 - 4.5) = 55.5.
    minutes = _nl._estimate_minutes_elapsed("3rd_period", "04:30")
    assert minutes == pytest.approx(55.5)


def test_raw_probability_at_puckdrop_is_pregame_prior():
    feats = _nl.build_live_features(
        row={"model_yes_prob": 0.7, "kalshi_price": 0.5, "league": "NHL"},
        live_state={
            "home_score": 0, "away_score": 0,
            "period": "1", "clock": "20:00", "state_age_seconds": 5.0,
        },
    )
    p = _nl._raw_live_probability(feats)
    # 0 minutes elapsed → blend weight = 0 → posterior == pre-game prior.
    assert p == pytest.approx(0.7)


def test_raw_probability_late_game_pulls_toward_market():
    feats = _nl.build_live_features(
        row={"model_yes_prob": 0.7, "kalshi_price": 0.2, "league": "NHL"},
        live_state={
            # 3rd period, 30 seconds remaining → ~59.5 of 60 minutes.
            "home_score": 1, "away_score": 3,
            "period": "3", "clock": "00:30", "state_age_seconds": 5.0,
        },
    )
    p = _nl._raw_live_probability(feats)
    # Heavy market pull + score-gap kicker push the posterior well below
    # the pre-game prior.
    assert p < 0.4


def test_raw_probability_no_signal_returns_none():
    feats = _nl.build_live_features(
        row={"model_yes_prob": None, "kalshi_price": None, "league": "NHL"},
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
    """Fake DB exposing only the fetchrow path nhl_live needs."""

    def __init__(self, live_state: dict[str, Any] | None) -> None:
        self.live_state = live_state
        self.fetchrow_calls = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "sportradar_live_state" in query:
            self.fetchrow_calls += 1
            return self.live_state
        return None


def test_dispatcher_routes_nhl_through_nhl_live(monkeypatch):
    fake = _PredictorFakeDB(
        live_state={
            "status": "inprogress", "home_score": 2, "away_score": 1,
            "period": "2", "clock": "10:00", "state_age_seconds": 4.0,
        }
    )
    monkeypatch.setattr(_nl, "db", fake)

    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-NHL-A", "league": "NHL", "game_id": "g1",
        "model_yes_prob": 0.6, "kalshi_price": 0.55, "is_live": True,
    }))
    assert prob is not None
    assert 0.0 <= prob <= 1.0
    assert meta["source"] == "nhl_live"
    assert meta["league"] == "NHL"
    assert "features" in meta
    assert fake.fetchrow_calls == 1


def test_dispatcher_skips_nhl_when_no_signal(monkeypatch):
    fake = _PredictorFakeDB(live_state=None)
    monkeypatch.setattr(_nl, "db", fake)
    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-NHL-A", "league": "NHL", "game_id": "g1",
        "model_yes_prob": None, "kalshi_price": None,
    }))
    assert prob is None
    assert meta["source"] == "nhl_live"
    assert "skipped_reason" in meta


def test_dispatcher_still_returns_none_for_unwired_league():
    # Sanity: an unrelated league (e.g. WNBA) keeps falling through.
    prob, meta = _run(_loop._predict_live_for_market({
        "market_id": "KX-WNBA-A", "league": "WNBA", "game_id": "g1",
        "model_yes_prob": 0.6, "kalshi_price": 0.55,
    }))
    assert prob is None
    assert meta["source"] is None
