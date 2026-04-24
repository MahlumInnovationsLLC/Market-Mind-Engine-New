"""Tests for the internal team-Elo rating engine.

The asyncpg pool is never opened — `app.db` primitives are
monkey-patched onto a tiny in-memory store that mirrors the real CRUD
semantics for `team_elo` and `team_elo_history` (UPSERT on PK,
idempotency on (league, team, game_id)). The schema itself is owned by
Alembic (revision 0018_lazy_ddl_relocation), so no schema-bootstrap
patching is needed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app import elo as _elo


# ---------------------------------------------------------------------------
# In-memory shim that mimics the team_elo + team_elo_history tables and
# the asyncpg connection used by record_game's transactional path.
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        # (league, team) -> {"rating", "games_played", "last_updated"}
        self.team_elo: dict[tuple[str, str], dict[str, Any]] = {}
        # list of history rows (insertion order = chronological)
        self.history: list[dict[str, Any]] = []

    # ---- low-level db.* primitives ----
    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM team_elo " in query and "WHERE league" in query:
            league, team = args
            row = self.team_elo.get((league, team))
            if row is None:
                return None
            return {"rating": row["rating"], "games_played": row["games_played"]}
        raise AssertionError(f"unhandled fetchrow: {query}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "rating_after - rating_before" in query:
            league, team, n = args
            rows = [
                h for h in self.history
                if h["league"] == league and h["team"] == team
            ]
            rows.sort(key=lambda r: r["played_at"], reverse=True)
            return [{"delta": r["rating_after"] - r["rating_before"]} for r in rows[:int(n)]]
        raise AssertionError(f"unhandled fetch: {query}")

    async def execute(self, *_args: Any, **_kw: Any) -> str:
        # Bookkeeping inserts/updates issued through the module-level
        # ``db.execute`` helper — we no-op all of them; the in-memory
        # state mutations live in the per-conn ``execute`` below.
        return "OK"

    # ---- connection used in transactional record_game ----
    async def get_pool(self) -> "_FakePool":
        return _FakePool(self)


class _FakePool:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    def acquire(self) -> "_FakeAcquire":
        return _FakeAcquire(self.store)


class _FakeAcquire:
    def __init__(self, store: _FakeStore) -> None:
        self.conn = _FakeConn(store)

    async def __aenter__(self) -> "_FakeConn":
        return self.conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeTx:
    async def __aenter__(self) -> "_FakeTx":
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeConn:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "team_elo_history" in query and "team IN" in query:
            league, game_id, t1, t2 = args
            for h in self.store.history:
                if h["league"] == league and h["game_id"] == game_id and h["team"] in (t1, t2):
                    return 1
            return None
        raise AssertionError(f"unhandled conn.fetchval: {query}")

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO team_elo_history" in query:
            (league, team, opponent, game_id, played_at, rb, ra, opp_rb,
             k, score, expected) = args
            is_home = "true" in query.split("VALUES")[0] and False  # arg not used
            self.store.history.append({
                "league": league, "team": team, "opponent": opponent,
                "game_id": game_id, "played_at": played_at,
                "rating_before": float(rb), "rating_after": float(ra),
                "opponent_rating_before": float(opp_rb),
                "k": float(k), "score": float(score), "expected": float(expected),
            })
            return "INSERT 1"
        if "INSERT INTO team_elo" in query and "ON CONFLICT" in query:
            league, team, rating, played_at = args
            row = self.store.team_elo.get((league, team))
            if row is None:
                self.store.team_elo[(league, team)] = {
                    "rating": float(rating),
                    "games_played": 1,
                    "last_updated": played_at,
                }
            else:
                row["rating"] = float(rating)
                row["games_played"] = int(row["games_played"]) + 1
                row["last_updated"] = played_at
            return "INSERT 1"
        raise AssertionError(f"unhandled conn.execute: {query}")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    s = _FakeStore()
    monkeypatch.setattr(_db, "fetchrow", s.fetchrow)
    monkeypatch.setattr(_db, "fetch", s.fetch)
    monkeypatch.setattr(_db, "execute", s.execute)
    monkeypatch.setattr(_db, "get_pool", s.get_pool)
    return s


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def test_expected_score_symmetric_at_equal_rating() -> None:
    assert _elo.expected_score(1500, 1500) == pytest.approx(0.5)


def test_expected_score_400_gap_is_ten_to_one() -> None:
    # The classic Elo property: a 400-pt edge ~= 10:1 expected score odds.
    assert _elo.expected_score(1900, 1500) == pytest.approx(10 / 11, rel=1e-3)
    assert _elo.expected_score(1500, 1900) == pytest.approx(1 / 11, rel=1e-3)


def test_expected_scores_sum_to_one() -> None:
    a, b = 1640, 1480
    assert _elo.expected_score(a, b) + _elo.expected_score(b, a) == pytest.approx(1.0)


def test_k_factor_per_league_known_and_default() -> None:
    assert _elo.k_factor("NFL") == 24.0
    assert _elo.k_factor("MLB") == 6.0
    assert _elo.k_factor("ZZZ") == 20.0
    assert _elo.k_factor(None) == 20.0


# ---------------------------------------------------------------------------
# Read helpers default sensibly when no rows exist
# ---------------------------------------------------------------------------


def test_get_rating_defaults_to_initial(store: _FakeStore) -> None:
    rating = asyncio.run(_elo.get_rating("NBA", "Lakers"))
    assert rating.rating == _elo.INITIAL_RATING
    assert rating.games_played == 0


def test_recent_form_delta_zero_when_no_history(store: _FakeStore) -> None:
    assert asyncio.run(_elo.recent_form_delta("NBA", "Lakers")) == 0.0


# ---------------------------------------------------------------------------
# Online updater
# ---------------------------------------------------------------------------


def test_record_game_moves_rating_toward_winner(store: _FakeStore) -> None:
    update = asyncio.run(_elo.record_game(
        "NBA", "Lakers", "Celtics", 110, 100,
        game_id="g1",
        played_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
    ))
    # Winner gains, loser loses, total Elo conserved (zero-sum).
    assert update.home_rating_after > update.home_rating_before
    assert update.away_rating_after < update.away_rating_before
    delta_home = update.home_rating_after - update.home_rating_before
    delta_away = update.away_rating_before - update.away_rating_after
    assert delta_home == pytest.approx(delta_away, rel=1e-6)
    # Ratings landed in team_elo.
    assert store.team_elo[("NBA", "Lakers")]["rating"] == pytest.approx(update.home_rating_after)
    assert store.team_elo[("NBA", "Celtics")]["rating"] == pytest.approx(update.away_rating_after)
    assert store.team_elo[("NBA", "Lakers")]["games_played"] == 1
    # Both perspectives recorded in history.
    assert len(store.history) == 2


def test_record_game_idempotent_on_game_id(store: _FakeStore) -> None:
    asyncio.run(_elo.record_game(
        "NBA", "Lakers", "Celtics", 110, 100,
        game_id="g1",
        played_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
    ))
    rating_after_first = store.team_elo[("NBA", "Lakers")]["rating"]
    # Re-run — must be a no-op.
    again = asyncio.run(_elo.record_game(
        "NBA", "Lakers", "Celtics", 110, 100,
        game_id="g1",
        played_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
    ))
    assert again.skipped is True
    assert store.team_elo[("NBA", "Lakers")]["rating"] == pytest.approx(rating_after_first)
    assert store.team_elo[("NBA", "Lakers")]["games_played"] == 1
    assert len(store.history) == 2  # not 4


def test_record_game_draw_keeps_ratings_stable(store: _FakeStore) -> None:
    update = asyncio.run(_elo.record_game(
        "Soccer", "TeamA", "TeamB", 1, 1,
        game_id="g-draw",
        played_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
    ))
    # Equal-rated teams to a draw -> rating must stay near 1500 (HFA
    # only nudges expected score, but K * (0.5 - exp) is still small).
    assert abs(update.home_rating_after - 1500) < abs(update.k)


def test_higher_k_factor_yields_bigger_swing(store: _FakeStore) -> None:
    nfl = asyncio.run(_elo.record_game(
        "NFL", "A", "B", 28, 14,
        game_id="g-nfl",
        played_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
    ))
    mlb = asyncio.run(_elo.record_game(
        "MLB", "C", "D", 5, 3,
        game_id="g-mlb",
        played_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
    ))
    assert (nfl.home_rating_after - nfl.home_rating_before) > (
        mlb.home_rating_after - mlb.home_rating_before
    )


# ---------------------------------------------------------------------------
# Recent-form delta + team_features
# ---------------------------------------------------------------------------


def test_recent_form_delta_reflects_streak(store: _FakeStore) -> None:
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    for i in range(5):
        asyncio.run(_elo.record_game(
            "NBA", "Lakers", f"Opp{i}", 110, 100,
            game_id=f"streak-{i}",
            played_at=base + timedelta(days=i),
        ))
    delta = asyncio.run(_elo.recent_form_delta("NBA", "Lakers", n=10))
    assert delta > 0  # five wins in a row -> positive form


def test_get_team_features_payload_shape(store: _FakeStore) -> None:
    asyncio.run(_elo.record_game(
        "NBA", "Lakers", "Celtics", 110, 100,
        game_id="g1",
        played_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
    ))
    feats = asyncio.run(_elo.get_team_features("NBA", "Lakers", "Celtics"))
    expected_keys = {
        "team_elo", "opponent_elo", "elo_diff", "expected_win_prob",
        "recent_form_delta", "opponent_recent_form_delta",
        "team_games_played", "opponent_games_played",
    }
    assert expected_keys <= set(feats.keys())
    # Lakers won -> their rating > opponent_elo.
    assert feats["team_elo"] > feats["opponent_elo"]
    assert feats["elo_diff"] == pytest.approx(feats["team_elo"] - feats["opponent_elo"])
    # Probability is in (0, 1).
    assert 0.0 < feats["expected_win_prob"] < 1.0


# ---------------------------------------------------------------------------
# Seed/backfill replay
# ---------------------------------------------------------------------------


def test_seed_from_history_processes_and_dedupes(store: _FakeStore) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    games = [
        {"league": "NBA", "home_team": "A", "away_team": "B",
         "home_score": 100, "away_score": 90,
         "game_id": f"h-{i}", "played_at": base + timedelta(days=i)}
        for i in range(3)
    ]
    res = asyncio.run(_elo.seed_from_history(games))
    assert res.games_processed == 3
    assert res.games_skipped == 0
    # Replay -> all skipped.
    res2 = asyncio.run(_elo.seed_from_history(games))
    assert res2.games_processed == 0
    assert res2.games_skipped == 3


def test_seed_accepts_iso_string_timestamps(store: _FakeStore) -> None:
    games = [
        {"league": "NBA", "home_team": "A", "away_team": "B",
         "home_score": 100, "away_score": 90,
         "game_id": "iso-1", "played_at": "2026-01-01T00:00:00+00:00"},
    ]
    res = asyncio.run(_elo.seed_from_history(games))
    assert res.games_processed == 1


# ---------------------------------------------------------------------------
# attach_elo_to_candidate enrichment
# ---------------------------------------------------------------------------


def test_attach_elo_noop_without_team_names(store: _FakeStore) -> None:
    cand: dict[str, Any] = {"league": "NBA", "game_context": {}}
    out = asyncio.run(_elo.attach_elo_to_candidate(cand))
    assert "team_elo" not in (out.get("game_context") or {})


def test_attach_elo_populates_game_context(store: _FakeStore) -> None:
    asyncio.run(_elo.record_game(
        "NBA", "Lakers", "Celtics", 110, 100,
        game_id="g1",
        played_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
    ))
    cand = {
        "league": "NBA",
        "game_context": {"home_team": "Lakers", "away_team": "Celtics"},
    }
    out = asyncio.run(_elo.attach_elo_to_candidate(cand))
    ctx = out["game_context"]
    assert ctx["team_elo"] > ctx["opponent_elo"]
    assert "elo_diff" in ctx
    assert "recent_form_delta" in ctx


def test_attach_elo_decodes_string_game_context(store: _FakeStore) -> None:
    cand = {
        "league": "NBA",
        "game_context": '{"home_team": "Lakers", "away_team": "Celtics"}',
    }
    out = asyncio.run(_elo.attach_elo_to_candidate(cand))
    assert isinstance(out["game_context"], dict)
    assert "team_elo" in out["game_context"]


# ---------------------------------------------------------------------------
# Feature builder telemetry
# ---------------------------------------------------------------------------


def test_build_features_exposes_elo_through_present() -> None:
    from app.orchestrator.features import build_features
    cand = {
        "league": "NBA",
        "consensus_probability": 0.6,
        "game_context": {
            "kalshi_price": 0.5,
            "team_elo": 1620.5,
            "opponent_elo": 1480.0,
            "elo_diff": 140.5,
            "recent_form_delta": 12.3,
        },
    }
    fv = build_features(cand)
    assert fv.present["team_elo"] == pytest.approx(1620.5)
    assert fv.present["opponent_elo"] == pytest.approx(1480.0)
    assert fv.present["elo_diff"] == pytest.approx(140.5)
    assert fv.present["recent_form_delta"] == pytest.approx(12.3)
