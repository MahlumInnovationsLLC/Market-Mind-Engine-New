"""Tests for the periodic Elo updater (`app.elo.scheduler`)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from app import db as _db
from app import elo as _elo
from app.elo import scheduler as elo_scheduler


class _FakeStore:
    """Mimics the slice of `games` + `team_elo_history` that the elo
    scheduler queries, plus the asyncpg surface `record_game()` uses.
    """

    def __init__(self) -> None:
        self.games: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.team_elo: dict[tuple[str, str], dict[str, Any]] = {}

    # ---- top-level db.* primitives ----
    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM games g" in query and "team_elo_history" in query:
            statuses, limit = args
            done_gids = {h["game_id"] for h in self.history}
            out: list[dict[str, Any]] = []
            for g in self.games:
                if g.get("status") not in statuses:
                    continue
                if g.get("home_score") is None or g.get("away_score") is None:
                    continue
                if not (g.get("home_team") and g.get("away_team")):
                    continue
                if g["game_id"] in done_gids:
                    continue
                out.append(dict(g))
            out.sort(key=lambda r: r.get("last_updated") or r.get("scheduled_start") or "")
            return out[: int(limit)]
        if "rating_after - rating_before" in query:
            league, team, n = args
            rows = [h for h in self.history if h["league"] == league and h["team"] == team]
            rows.sort(key=lambda r: r["played_at"], reverse=True)
            return [{"delta": r["rating_after"] - r["rating_before"]} for r in rows[: int(n)]]
        raise AssertionError(f"unhandled fetch: {query}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM team_elo " in query:
            league, team = args
            row = self.team_elo.get((league, team))
            return None if row is None else {
                "rating": row["rating"], "games_played": row["games_played"],
            }
        raise AssertionError(f"unhandled fetchrow: {query}")

    async def execute(self, *_args: Any, **_kw: Any) -> str:
        return "OK"

    async def get_pool(self) -> "_FakePool":
        return _FakePool(self)


class _FakePool:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    def acquire(self) -> "_FakeAcquire":
        return _FakeAcquire(self.store)


class _FakeAcquire:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    async def __aenter__(self) -> "_FakeConn":
        return _FakeConn(self.store)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeTx:
    async def __aenter__(self) -> "_FakeTx":
        return self

    async def __aexit__(self, *exc: Any) -> None:
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
    monkeypatch.setattr(_db, "fetch", s.fetch)
    monkeypatch.setattr(_db, "fetchrow", s.fetchrow)
    monkeypatch.setattr(_db, "execute", s.execute)
    monkeypatch.setattr(_db, "get_pool", s.get_pool)

    async def _noop_schema() -> None:
        return None

    monkeypatch.setattr(_elo, "ensure_schema", _noop_schema)
    monkeypatch.setattr(elo_scheduler, "ensure_schema", _noop_schema)
    _elo._schema_ready = False
    return s


def _add_game(store: _FakeStore, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "game_id": "g1",
        "league": "NBA",
        "sport": "basketball_nba",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "home_score": 110,
        "away_score": 100,
        "scheduled_start": "2026-04-21T00:00:00+00:00",
        "last_updated": "2026-04-21T03:00:00+00:00",
        "status": "final",
    }
    base.update(overrides)
    store.games.append(base)
    return base


def test_run_cycle_records_finished_games(store: _FakeStore) -> None:
    _add_game(store, game_id="g1", home_team="Lakers", away_team="Celtics",
              home_score=110, away_score=100)
    _add_game(store, game_id="g2", home_team="Bulls", away_team="Heat",
              home_score=99, away_score=101, status="closed")

    cr = asyncio.run(elo_scheduler.run_cycle())
    assert cr.games_examined == 2
    assert cr.games_updated == 2
    assert cr.teams_touched == 4
    assert cr.errors == []
    # Both perspectives written for each game.
    assert len(store.history) == 4
    # Winners gain rating.
    assert store.team_elo[("NBA", "Lakers")]["rating"] > _elo.INITIAL_RATING
    assert store.team_elo[("NBA", "Heat")]["rating"] > _elo.INITIAL_RATING


def test_run_cycle_skips_unfinished_or_scoreless(store: _FakeStore) -> None:
    _add_game(store, game_id="scheduled-1", status="scheduled")
    _add_game(store, game_id="missing-score", home_score=None, away_score=None)
    _add_game(store, game_id="missing-team", home_team="", away_team="")

    cr = asyncio.run(elo_scheduler.run_cycle())
    assert cr.games_examined == 0
    assert cr.games_updated == 0
    assert store.history == []


def test_run_cycle_is_idempotent_across_cycles(store: _FakeStore) -> None:
    _add_game(store, game_id="g-once")
    first = asyncio.run(elo_scheduler.run_cycle())
    assert first.games_updated == 1
    rating_after = store.team_elo[("NBA", "Lakers")]["rating"]

    second = asyncio.run(elo_scheduler.run_cycle())
    # SQL filter excludes already-recorded games -> not even examined.
    assert second.games_examined == 0
    assert second.games_updated == 0
    assert store.team_elo[("NBA", "Lakers")]["rating"] == pytest.approx(rating_after)


def test_run_cycle_falls_back_to_sport_when_league_missing(store: _FakeStore) -> None:
    _add_game(store, game_id="g-sport", league=None, sport="basketball_nba")
    cr = asyncio.run(elo_scheduler.run_cycle())
    assert cr.games_updated == 1
    # League stored as the upper-cased sport key.
    assert ("BASKETBALL_NBA", "Lakers") in store.team_elo


def test_scheduler_status_reports_last_cycle(store: _FakeStore) -> None:
    _add_game(store, game_id="g-status")
    sched = elo_scheduler._EloScheduler(cycle_seconds=300.0)
    asyncio.run(sched.run_now())
    s = sched.status()
    assert s["running"] is False  # never started — only run_now was called
    assert s["cycle_seconds"] == 300.0
    last = s["last_cycle"]
    assert last is not None
    assert last["games_updated"] == 1
    assert last["teams_touched"] == 2
    assert last["errors"] == []


def test_scheduler_disable_env_blocks_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_ELO_UPDATER_DISABLED", "1")
    sched = elo_scheduler._EloScheduler(cycle_seconds=0.01)
    asyncio.run(sched.start())
    assert sched.status()["running"] is False
