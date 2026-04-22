"""Smoke tests for the Sportradar premium feed module.

These tests are intentionally hermetic: they patch the HTTP client and
the database pool so they never touch the network or Postgres.  The goal
is to verify that the feed module imports cleanly, registers the
expected feeds with sensible defaults when the API key is missing, and
parses both the injuries and live-state payload shapes correctly.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from app.feeds import sportradar
from app.feeds.odds_api import SPORTRADAR_SPORTS, _SPORTRADAR_LEAGUE
from app.feeds.registry import FeedRegistry


def _isolated_registry():
    """Return a fresh FeedRegistry the test can mutate without polluting
    the process-wide singleton."""
    reg = FeedRegistry()
    return reg


def test_sports_constant_covers_six_leagues():
    # Spec calls for NFL/NBA/MLB/NHL/NCAAF/NCAAB across the swarm; make
    # sure every league lands in the schedule + extras registration loop.
    assert set(SPORTRADAR_SPORTS) == {"nba", "nhl", "mlb", "ncaab", "nfl", "ncaaf"}
    assert set(_SPORTRADAR_LEAGUE) == set(SPORTRADAR_SPORTS)


def test_register_extra_feeds_skips_when_key_missing(monkeypatch):
    # Wipe every Sportradar key so we can assert the disabled path.
    for sport in SPORTRADAR_SPORTS:
        monkeypatch.delenv(f"SPORTRADAR_{sport.upper()}_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)

    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    # 6 leagues × 3 feed types (rosters + injuries + live) = 18 feeds.
    snap = reg.snapshot()
    names = {s["name"] for s in snap}
    assert len(snap) == 18
    for sport in SPORTRADAR_SPORTS:
        assert f"sportradar_{sport}_rosters" in names
        assert f"sportradar_{sport}_injuries" in names
        assert f"sportradar_{sport}_live" in names
    assert all(s["enabled"] is False for s in snap)
    assert all(s["skipped_reason"] for s in snap)


def test_register_extra_feeds_enabled_with_global_key(monkeypatch):
    for sport in SPORTRADAR_SPORTS:
        monkeypatch.delenv(f"SPORTRADAR_{sport.upper()}_KEY", raising=False)
    monkeypatch.setenv("SPORTRADAR_API_KEY", "test-key")

    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    snap = reg.snapshot()
    assert all(s["enabled"] is True for s in snap)
    assert all(s["skipped_reason"] is None for s in snap)


def test_register_is_idempotent(monkeypatch):
    monkeypatch.setenv("SPORTRADAR_API_KEY", "test-key")
    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()
    sportradar.register_sportradar_extra_feeds()  # second call must not raise
    assert len(reg.snapshot()) == 18


def test_flatten_injuries_handles_typical_payload():
    payload = {
        "teams": [
            {
                "id": "team-1",
                "name": "Lakers",
                "players": [
                    {
                        "id": "p-1",
                        "full_name": "Player One",
                        "position": "G",
                        "injuries": [
                            {
                                "status": "Out",
                                "desc": "Knee",
                                "update_date": "2026-04-22T12:00:00Z",
                            }
                        ],
                    },
                    {"id": "", "full_name": "Skip me"},  # missing id -> skipped
                ],
            }
        ]
    }
    rows = sportradar._flatten_injuries(payload)
    assert len(rows) == 1
    row = rows[0]
    assert row["player_id"] == "p-1"
    assert row["team_id"] == "team-1"
    assert row["status"] == "Out"
    assert row["description"] == "Knee"


def test_flatten_injuries_empty_payload_safe():
    assert sportradar._flatten_injuries({}) == []
    assert sportradar._flatten_injuries({"teams": []}) == []


def test_extract_games_handles_three_envelope_shapes():
    # Daily-schedule shape (NBA/NHL/MLB/NCAAB/NCAAF).
    assert sportradar._extract_games({"games": [{"id": "g1"}]}) == [{"id": "g1"}]
    # League-wrapped shape.
    assert sportradar._extract_games({"league": {"games": [{"id": "g2"}]}}) == [{"id": "g2"}]
    # NFL current-week shape.
    assert sportradar._extract_games({"week": {"games": [{"id": "g3"}]}}) == [{"id": "g3"}]
    assert sportradar._extract_games({}) == []


def test_parse_ts_round_trips_iso_string():
    dt = sportradar._parse_ts("2026-04-22T12:34:56Z")
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None
    assert sportradar._parse_ts(None) is None
    assert sportradar._parse_ts("not-a-date") is None


def test_extract_games_nfl_week_envelope_used_by_schedule_poller():
    # Regression: NFL's current_week endpoint nests games under {"week": ...}.
    # Both the schedule poller (in odds_api) and the live-state poller
    # must understand this shape.
    from app.feeds import odds_api  # local import to avoid module-load side effects

    # Live-state extractor (sportradar.py).
    assert sportradar._extract_games({"week": {"games": [{"id": "nfl-1"}]}}) == [
        {"id": "nfl-1"}
    ]
    # Schedule extractor (odds_api.py) — this exercises the same logical
    # selector by calling _poll_sportradar with a stubbed HTTP response.
    # We don't touch the network; we only verify the parser can find the
    # week-envelope shape rather than returning 0 games silently.
    games_seen: list[dict] = []

    async def _fake_get(self, url, params=None):  # noqa: ANN001
        class R:
            status_code = 200
            headers: dict = {}
            text = "{}"

            def json(self_inner):  # noqa: ANN001
                return {"week": {"games": [{"id": "nfl-1", "home": {"name": "A"},
                                             "away": {"name": "B"},
                                             "scheduled": "2026-04-22T20:00:00Z",
                                             "status": "scheduled"}]}}
        return R()

    # We don't need to actually persist; replace db.acquire with a context
    # manager whose executemany captures the rows.
    captured: list[tuple] = []

    class _Conn:
        def transaction(self):
            class _Tx:
                async def __aenter__(self_inner):  # noqa: ANN001
                    return None

                async def __aexit__(self_inner, *a):  # noqa: ANN001
                    return None
            return _Tx()

        async def executemany(self_inner, sql, rows):  # noqa: ANN001
            captured.extend(rows)

    class _Pool:
        async def __aenter__(self_inner):  # noqa: ANN001
            return _Conn()

        async def __aexit__(self_inner, *a):  # noqa: ANN001
            return None

    import asyncio
    import httpx as _httpx

    os.environ["SPORTRADAR_API_KEY"] = "test-key"
    try:
        from app import db as _db
        # patch httpx + db
        orig_get = _httpx.AsyncClient.get
        orig_acquire = _db.acquire
        _httpx.AsyncClient.get = _fake_get  # type: ignore[assignment]
        _db.acquire = lambda: _Pool()  # type: ignore[assignment]
        try:
            n = asyncio.run(odds_api._poll_sportradar("nfl"))
        finally:
            _httpx.AsyncClient.get = orig_get  # type: ignore[assignment]
            _db.acquire = orig_acquire  # type: ignore[assignment]
    finally:
        os.environ.pop("SPORTRADAR_API_KEY", None)

    assert n == 1, "NFL week.games envelope should yield 1 row, not 0"
    assert captured and captured[0][0] == "nfl-1"


def test_persistence_failure_propagates(monkeypatch):
    """Architect feedback: a DB failure must surface as a real exception so
    the scheduler records a failure and the health page reflects it,
    instead of being swallowed and reported as a successful 0-row poll."""

    monkeypatch.setenv("SPORTRADAR_API_KEY", "test-key")

    async def _ok_get(provider, url, api_key):  # noqa: ANN001
        return {
            "teams": [
                {
                    "id": "t1",
                    "name": "Team",
                    "players": [{"id": "p1", "full_name": "X", "injuries": [{"status": "Out"}]}],
                }
            ]
        }

    monkeypatch.setattr(sportradar, "_get_json", _ok_get)

    class _BoomConn:
        def transaction(self):
            class _T:
                async def __aenter__(self_inner):
                    return None

                async def __aexit__(self_inner, *a):
                    return None
            return _T()

        async def executemany(self_inner, *a, **k):
            raise RuntimeError("db down")

    class _BoomPool:
        async def __aenter__(self_inner):
            return _BoomConn()

        async def __aexit__(self_inner, *a):
            return None

    monkeypatch.setattr(sportradar.db, "acquire", lambda: _BoomPool())

    import asyncio
    with pytest.raises(RuntimeError, match="db down"):
        asyncio.run(sportradar._poll_injuries("nba"))


def test_poll_rosters_falls_back_to_team_profile(monkeypatch):
    """Hierarchy returns teams with no inline players -> we must hit the
    per-team profile endpoint and still write rows."""
    import asyncio

    monkeypatch.setenv("SPORTRADAR_NBA_KEY", "k")

    hierarchy = {
        "conferences": [
            {"divisions": [
                {"teams": [
                    {"id": "team-1", "name": "Lakers", "market": "LA",  "players": []},
                    {"id": "team-2", "name": "Celtics", "market": "BOS", "players": []},
                ]}
            ]}
        ]
    }
    profile_by_team = {
        "team-1": {"players": [
            {"id": "p1", "full_name": "LeBron", "position": "F", "jersey_number": 23},
            {"id": "p2", "full_name": "AD",     "position": "F", "jersey_number": 3},
        ]},
        "team-2": {"players": [
            {"id": "p3", "full_name": "Tatum",  "position": "F", "jersey_number": 0},
        ]},
    }

    calls: list[str] = []

    async def fake_get(name, url, key):
        calls.append(url)
        if "hierarchy" in url:
            return hierarchy
        for tid, prof in profile_by_team.items():
            if f"/teams/{tid}/" in url:
                return prof
        return None

    monkeypatch.setattr(sportradar, "_get_json", fake_get)

    captured: dict[str, list] = {}

    class FakeConn:
        async def executemany(self, sql, rows):
            captured["rows"] = list(rows)
        def transaction(self):
            class Ctx:
                async def __aenter__(self_inner): return self_inner
                async def __aexit__(self_inner, *a): return False
            return Ctx()

    class FakeAcquire:
        async def __aenter__(self): return FakeConn()
        async def __aexit__(self, *a): return False

    class FakeDB:
        def acquire(self): return FakeAcquire()

    monkeypatch.setattr(sportradar, "db", FakeDB())

    written = asyncio.run(sportradar._poll_rosters("nba"))
    assert written == 3
    assert len(captured["rows"]) == 3
    # 1 hierarchy call + 2 per-team profile calls
    assert sum(1 for u in calls if "hierarchy" in u) == 1
    assert sum(1 for u in calls if "/teams/team-1/" in u) == 1
    assert sum(1 for u in calls if "/teams/team-2/" in u) == 1


def test_walk_teams_handles_nested_hierarchy():
    payload = {
        "conferences": [
            {
                "name": "East",
                "divisions": [
                    {
                        "name": "Atlantic",
                        "teams": [
                            {
                                "id": "t1",
                                "name": "Celtics",
                                "market": "Boston",
                                "players": [
                                    {"id": "p1", "full_name": "Player One",
                                     "position": "G", "jersey_number": 0},
                                    {"id": "p2", "full_name": "Player Two",
                                     "position": "F"},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    teams = sportradar._walk_teams(payload)
    assert len(teams) == 1
    assert teams[0]["id"] == "t1"
    assert len(teams[0]["players"]) == 2


def test_api_key_for_prefers_per_sport(monkeypatch):
    monkeypatch.setenv("SPORTRADAR_API_KEY", "global")
    monkeypatch.setenv("SPORTRADAR_NBA_KEY", "nba-specific")
    assert sportradar._api_key_for("nba") == "nba-specific"
    assert sportradar._api_key_for("nhl") == "global"
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_NBA_KEY", raising=False)
    assert sportradar._api_key_for("nba") is None
