"""End-to-end test: the cycle's candidate-construction path threads
home/away team names through to `attach_elo_to_candidate` so the Elo
enrichment actually populates `team_elo` / `opponent_elo` instead of
silently no-op'ing (task #130).

We exercise the two production candidate sources:

  * `_cached_debates`  — adds `home_team`/`away_team` to its SELECT via
    the existing `LEFT JOIN games`.
  * `_candidate_predictions` -> `_fresh_debate_candidate(...)`  — joins
    `games` and folds the team names into `game_context`.

The Elo store is the in-memory shim from `test_elo` (one game seeded
so the lookup returns a non-default rating), and `attach_elo_to_candidate`
runs against it.  No real DB is opened — `db.fetch` / `db.fetchrow` /
`db.get_pool` are monkey-patched.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from app import db as _db
from app import elo as _elo
from app.orchestrator import cycle as _cycle
from tests.test_elo import _FakeStore  # reuse the in-memory elo shim


@pytest.fixture
def elo_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    s = _FakeStore()
    monkeypatch.setattr(_db, "fetchrow", s.fetchrow)
    monkeypatch.setattr(_db, "fetch", s.fetch)
    monkeypatch.setattr(_db, "execute", s.execute)
    monkeypatch.setattr(_db, "get_pool", s.get_pool)

    # Seed a single game so Lakers' rating drifts off the default 1500.
    asyncio.run(_elo.record_game(
        "NBA", "Lakers", "Celtics", 110, 100,
        game_id="seed-1",
        played_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
    ))
    return s


def test_fresh_debate_candidate_carries_team_names_into_game_context() -> None:
    """The fresh-debate fan-out builds a candidate dict that must
    surface home_team/away_team where attach_elo_to_candidate looks
    for them."""
    pred = {
        "id": 1,
        "game_id": "g-1",
        "league": "NBA",
        "kalshi_market_id": "MKT-1",
        "kalshi_price": 0.55,
        "home_win_prob": 0.6,
        "confidence": 0.7,
        "edge": 0.05,
        "game_time": None,
        "home_team": "Lakers",
        "away_team": "Celtics",
    }
    debate = {
        "debate_id": 42,
        "game_id": "g-1",
        "matchup": "LAL vs BOS",
        "league": "NBA",
        "market_type": "moneyline",
        "consensus_probability": 0.6,
        "confidence_band_low": 0.55,
        "confidence_band_high": 0.65,
        "agreement_rate": 0.8,
        "approved": True,
        "recommended_action": "BUY_YES",
    }
    cand = _cycle._fresh_debate_candidate(pred, debate)
    assert cand["home_team"] == "Lakers"
    assert cand["away_team"] == "Celtics"
    assert cand["game_context"]["home_team"] == "Lakers"
    assert cand["game_context"]["away_team"] == "Celtics"
    # Existing fields preserved.
    assert cand["game_context"]["kalshi_market_id"] == "MKT-1"
    assert cand["debate_id"] == 42


def test_attach_elo_fires_for_fresh_candidate(elo_store: _FakeStore) -> None:
    """End-to-end: a fresh-debate candidate built from a row carrying
    team names yields non-default team_elo/opponent_elo after the
    enrichment step the cycle invokes."""
    pred = {
        "game_id": "g-1", "league": "NBA",
        "kalshi_market_id": "MKT-1", "kalshi_price": 0.55,
        "home_win_prob": 0.6, "confidence": 0.7, "edge": 0.05,
        "game_time": None,
        "home_team": "Lakers", "away_team": "Celtics",
    }
    debate = {
        "debate_id": 1, "game_id": "g-1", "matchup": None,
        "league": "NBA", "market_type": "moneyline",
        "consensus_probability": 0.6, "confidence_band_low": None,
        "confidence_band_high": None, "agreement_rate": None,
        "approved": True, "recommended_action": "BUY_YES",
    }
    cand = _cycle._fresh_debate_candidate(pred, debate)
    asyncio.run(_elo.attach_elo_to_candidate(cand))
    ctx = cand["game_context"]
    # Lakers won the seeded game -> their rating moved up off 1500.
    assert ctx["team_elo"] != _elo.INITIAL_RATING
    assert ctx["opponent_elo"] != _elo.INITIAL_RATING
    assert ctx["team_elo"] > ctx["opponent_elo"]
    assert "elo_diff" in ctx
    assert "recent_form_delta" in ctx


def test_attach_elo_fires_for_cached_debate_row(elo_store: _FakeStore) -> None:
    """Mirror of the cached-debate path: the row shape returned by
    `_cached_debates` (game_context + top-level home/away from the
    games join) must enrich after attach_elo_to_candidate runs."""
    cached_row = {
        "debate_id": 7,
        "game_id": "g-1",
        "matchup": "LAL vs BOS",
        "league": "NBA",
        "market_type": "moneyline",
        "consensus_probability": 0.58,
        "confidence_band_low": 0.5,
        "confidence_band_high": 0.66,
        "agreement_rate": 0.75,
        "approved": True,
        "recommended_action": "BUY_YES",
        # game_context as JSON-string mirrors how asyncpg returns jsonb
        # when row factories don't auto-decode — attach_elo handles both.
        "game_context": '{"kalshi_market_id": "MKT-1", "kalshi_price": 0.5}',
        "game_time": None,
        # Top-level home_team/away_team come straight from the new
        # `LEFT JOIN games g` columns.
        "home_team": "Lakers",
        "away_team": "Celtics",
    }
    asyncio.run(_elo.attach_elo_to_candidate(cached_row))
    ctx = cached_row["game_context"]
    assert isinstance(ctx, dict)
    assert ctx["team_elo"] != _elo.INITIAL_RATING
    assert ctx["team_elo"] > ctx["opponent_elo"]


def test_candidate_prediction_sql_pulls_home_and_away_columns() -> None:
    """Lock in the SQL contract: both branches of `_candidate_predictions`
    must select the team-name columns so the rows reach
    `_fresh_debate_candidate` with them populated."""
    import inspect
    src = inspect.getsource(_cycle._candidate_predictions)
    # Both paths (leagues filter + no-league fallback) must JOIN games
    # and select the team columns.
    assert src.count("LEFT JOIN games g ON g.game_id = p.game_id") == 2
    assert src.count("g.home_team, g.away_team") == 2


def test_cached_debates_sql_pulls_home_and_away_columns() -> None:
    """Same contract for the cached-debate path."""
    import inspect
    src = inspect.getsource(_cycle._cached_debates)
    assert src.count("g.home_team, g.away_team") == 2
