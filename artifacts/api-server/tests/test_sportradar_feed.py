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


def test_sports_constant_covers_every_supported_league():
    # NA sports (task #195) + international soccer (task #196) — every
    # league must appear in both the schedule and league-code maps so
    # the registration loop wires the correct upper-cased league name.
    expected = {
        "nba", "nhl", "mlb", "ncaab", "nfl", "ncaaf",
        "epl", "laliga", "seriea", "bundes", "ligue1", "mls", "ucl",
        # Combat & individual sports (task #214) — every league must
        # be registered in both maps so the registration loop wires
        # the correct upper-cased league code into the feed name.
        "ufc", "atp", "wta", "f1", "pga",
    }
    assert expected.issubset(set(SPORTRADAR_SPORTS))
    assert set(_SPORTRADAR_LEAGUE) == set(SPORTRADAR_SPORTS)


def test_register_extra_feeds_skips_when_key_missing(monkeypatch):
    # Wipe every Sportradar key so we can assert the disabled path.
    for sport in SPORTRADAR_SPORTS:
        monkeypatch.delenv(f"SPORTRADAR_{sport.upper()}_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)

    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    # N leagues × 3 feed types (rosters + injuries + live), plus the
    # cross-sport tournament-leaderboard refresher (task #449) and the
    # tournament auto-replay sweep (task #707).  Derived from
    # SPORTRADAR_SPORTS so adding a league (task #196 added 7 soccer
    # competitions) doesn't require fixing this assertion.
    snap = reg.snapshot()
    names = {s["name"] for s in snap}
    assert len(snap) == 3 * len(SPORTRADAR_SPORTS) + 2
    for sport in SPORTRADAR_SPORTS:
        assert f"sportradar_{sport}_rosters" in names
        assert f"sportradar_{sport}_injuries" in names
        assert f"sportradar_{sport}_live" in names
    assert "sportradar_tournament_leaderboards" in names
    assert "sportradar_tournament_auto_replay" in names
    # The auto-replay feed is enabled even without a Sportradar API key
    # (it walks already-persisted snapshots), so the "everything else
    # disabled" invariant only applies to the per-sport pollers + the
    # leaderboard refresher.
    other = [s for s in snap if s["name"] != "sportradar_tournament_auto_replay"]
    assert all(s["enabled"] is False for s in other)
    assert all(s["skipped_reason"] for s in other)


def test_register_extra_feeds_enabled_with_global_key(monkeypatch):
    for sport in SPORTRADAR_SPORTS:
        monkeypatch.delenv(f"SPORTRADAR_{sport.upper()}_KEY", raising=False)
    monkeypatch.setenv("SPORTRADAR_API_KEY", "test-key")

    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    # Feeds whose endpoint paths are wired (NA leagues) become enabled;
    # leagues whose path templates aren't yet implemented (soccer, task
    # #196) are registered but kept disabled with a clear
    # `skipped_reason` instead of crashing at poll time on KeyError.
    snap = reg.snapshot()
    enabled_feeds = [s for s in snap if s["enabled"]]
    disabled_feeds = [s for s in snap if not s["enabled"]]
    assert enabled_feeds, "expected at least the NA leagues to be enabled"
    assert all(s["skipped_reason"] is None for s in enabled_feeds)
    # Task #211: soccer endpoints are now wired alongside the NA leagues,
    # so every soccer feed must be enabled (with no skipped_reason) when
    # the global key is set.  Other not-yet-wired sports (combat /
    # individual) may still register as disabled with a clear reason.
    enabled_names = {s["name"] for s in enabled_feeds}
    for sport in ("epl", "laliga", "seriea", "bundes", "ligue1", "mls", "ucl"):
        assert f"sportradar_{sport}_rosters" in enabled_names
        assert f"sportradar_{sport}_injuries" in enabled_names
        assert f"sportradar_{sport}_live" in enabled_names
    for s in disabled_feeds:
        assert s["skipped_reason"] and "endpoint not wired" in s["skipped_reason"]


def test_register_is_idempotent(monkeypatch):
    monkeypatch.setenv("SPORTRADAR_API_KEY", "test-key")
    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()
    sportradar.register_sportradar_extra_feeds()  # second call must not raise
    # Per-sport (rosters + injuries + live) plus the cross-sport
    # tournament-leaderboard refresher (task #449) and the tournament
    # auto-replay sweep (task #707) -> +2.
    assert len(reg.snapshot()) == 3 * len(SPORTRADAR_SPORTS) + 2


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


# ---------------------------------------------------------------------------
# Soccer-extended fixtures (task #211)
# ---------------------------------------------------------------------------


SOCCER_LEAGUES = ("epl", "laliga", "seriea", "bundes", "ligue1", "mls", "ucl")


def _soccer_tournament_info_fixture(season_id: str = "sr:season:118689") -> dict:
    return {
        "season": {
            "id": season_id,
            "name": "2025/26",
            "start_date": "2025-08-01",
            "end_date": "2026-05-31",
        },
        "tournament": {"id": "sr:tournament:17", "name": "Premier League"},
    }


def _soccer_competitors_fixture() -> dict:
    return {
        "season_competitors": [
            {"id": "sr:competitor:1", "name": "Arsenal"},
            {"id": "sr:competitor:2", "name": "Chelsea"},
        ]
    }


def _soccer_competitor_profile_fixture(team_id: str, team_name: str) -> dict:
    return {
        "competitor": {"id": team_id, "name": team_name, "country": "England"},
        "players": [
            {
                "id": f"{team_id}:p1",
                "name": "Striker One",
                "type": "forward",
                "jersey_number": 9,
                "height": 185,
                "weight": 78,
            },
            {
                "id": f"{team_id}:p2",
                "name": "Keeper One",
                "type": "goalkeeper",
                "jersey_number": 1,
            },
            # No id -> skipped silently.
            {"name": "ghost"},
        ],
    }


def _soccer_missing_players_fixture() -> dict:
    return {
        "competitors": [
            {
                "id": "sr:competitor:1",
                "name": "Arsenal",
                "players": [
                    {
                        "id": "sr:player:101",
                        "name": "Bukayo Saka",
                        "type": "missing",
                        "status": "out",
                        "reason": "knee injury",
                        "start_date": "2026-04-01",
                    },
                    {
                        "id": "sr:player:102",
                        "name": "Martin Odegaard",
                        "type": "doubtful",
                        "status": "questionable",
                        "reason": "ankle",
                    },
                ],
            },
            {
                "id": "sr:competitor:2",
                "name": "Chelsea",
                "players": [
                    {
                        "id": "sr:player:201",
                        "name": "Cole Palmer",
                        "status": "out",
                        "reason": "groin",
                    }
                ],
            },
        ]
    }


def _soccer_live_schedule_fixture() -> dict:
    return {
        "schedules": [
            {
                "sport_event": {
                    "id": "sr:sport_event:111",
                    "start_time": "2026-04-23T19:00:00+00:00",
                    "tournament": {"id": "sr:tournament:17", "name": "Premier League"},
                    "competitors": [
                        {"id": "sr:competitor:1", "name": "Arsenal", "qualifier": "home"},
                        {"id": "sr:competitor:2", "name": "Chelsea", "qualifier": "away"},
                    ],
                },
                "sport_event_status": {
                    "status": "live",
                    "match_status": "1st_half",
                    "home_score": 1,
                    "away_score": 0,
                    "clock": "23:45",
                },
            },
            {
                "sport_event": {
                    "id": "sr:sport_event:222",
                    "start_time": "2026-04-23T16:00:00+00:00",
                    "competitors": [
                        {"name": "A", "qualifier": "home"},
                        {"name": "B", "qualifier": "away"},
                    ],
                },
                "sport_event_status": {
                    "status": "closed",
                    "match_status": "ended",
                    "home_score": 2,
                    "away_score": 2,
                },
            },
            {
                # Skipped: not yet started.
                "sport_event": {"id": "sr:sport_event:333"},
                "sport_event_status": {"status": "not_started"},
            },
        ]
    }


class _CapturingDB:
    """Stand-in for `app.feeds.sportradar.db` that records every batch
    of rows handed to executemany so tests can assert on them."""

    def __init__(self) -> None:
        self.batches: list[list[tuple]] = []

    def acquire(self):
        outer = self

        class _Conn:
            def transaction(self):
                class _Tx:
                    async def __aenter__(self_inner):
                        return None

                    async def __aexit__(self_inner, *a):
                        return None

                return _Tx()

            async def executemany(self_inner, sql, rows):
                outer.batches.append(list(rows))

        class _Pool:
            async def __aenter__(self_inner):
                return _Conn()

            async def __aexit__(self_inner, *a):
                return None

        return _Pool()


def _install_soccer_http(monkeypatch, *, info, competitors, profile_by_id, missing, live):
    async def fake_get(name, url, key, **_kw):
        if "/tournaments/" in url and "/info.json" in url:
            return info
        if "/competitors.json" in url:
            return competitors
        if "/missing_players.json" in url:
            return missing
        if "/competitors/" in url and "/profile.json" in url:
            for tid, prof in profile_by_id.items():
                if f"/competitors/{tid}/" in url:
                    return prof
            return None
        if "/schedules/" in url and "schedule.json" in url:
            return live
        return None

    monkeypatch.setattr(sportradar, "_get_json", fake_get)


@pytest.fixture(autouse=True)
def _clear_soccer_season_cache():
    sportradar._SOCCER_SEASON_CACHE.clear()
    sportradar._SOCCER_SEASON_RESOLVED_AT.clear()
    yield
    sportradar._SOCCER_SEASON_CACHE.clear()
    sportradar._SOCCER_SEASON_RESOLVED_AT.clear()


def test_soccer_endpoints_registered_in_path_dicts():
    # All seven soccer leagues must be present in each of the three URL
    # maps so the registration loop and the `has_*_path` checks succeed.
    for sport in SOCCER_LEAGUES:
        assert sport in sportradar._INJURY_PATHS, sport
        assert sport in sportradar._ROSTER_PATHS, sport
        assert sport in sportradar._TEAM_PROFILE_PATHS, sport
        assert sport in sportradar._DAILY_SCHEDULE_PATHS, sport
        assert "soccer-extended" in sportradar._INJURY_PATHS[sport]
        assert "soccer-extended" in sportradar._ROSTER_PATHS[sport]
        assert "soccer-extended" in sportradar._TEAM_PROFILE_PATHS[sport]
        assert "soccer-extended" in sportradar._DAILY_SCHEDULE_PATHS[sport]


def test_flatten_soccer_injuries_handles_missing_players_payload():
    rows = sportradar._flatten_soccer_injuries(_soccer_missing_players_fixture())
    assert len(rows) == 3
    by_id = {r["player_id"]: r for r in rows}
    saka = by_id["sr:player:101"]
    assert saka["team_id"] == "sr:competitor:1"
    assert saka["team_name"] == "Arsenal"
    assert saka["status"] == "out"
    assert "knee" in saka["description"]
    assert by_id["sr:player:201"]["team_name"] == "Chelsea"


def test_flatten_soccer_injuries_empty_safe():
    assert sportradar._flatten_soccer_injuries({}) == []
    assert sportradar._flatten_soccer_injuries({"competitors": []}) == []


def test_flatten_soccer_competitor_profile_pulls_team_and_players():
    prof = _soccer_competitor_profile_fixture("sr:competitor:1", "Arsenal")
    tid, name, players = sportradar._flatten_soccer_competitor_profile(prof)
    assert tid == "sr:competitor:1"
    assert name == "Arsenal"
    assert len(players) == 3  # un-id'd entries are filtered later in the poller


def test_extract_soccer_live_rows_filters_to_live_or_finished():
    rows = sportradar._extract_soccer_live_rows("EPL", _soccer_live_schedule_fixture())
    # 2 live/closed entries kept, the not_started one is dropped.
    assert len(rows) == 2
    by_id = {r[1]: r for r in rows}
    live = by_id["sr:sport_event:111"]
    # (league, gid, status, home_score, away_score, period, clock, scheduled, payload)
    assert live[0] == "EPL"
    assert live[2] == "live"
    assert live[3] == 1 and live[4] == 0
    assert live[5] == "1st_half"
    assert live[6] == "23:45"
    closed = by_id["sr:sport_event:222"]
    assert closed[2] == "closed"
    assert closed[3] == 2 and closed[4] == 2


@pytest.mark.parametrize("sport", SOCCER_LEAGUES)
def test_poll_soccer_injuries_writes_nonzero_rows(monkeypatch, sport):
    import asyncio
    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")
    _install_soccer_http(
        monkeypatch,
        info=_soccer_tournament_info_fixture(),
        competitors=_soccer_competitors_fixture(),
        profile_by_id={},
        missing=_soccer_missing_players_fixture(),
        live=_soccer_live_schedule_fixture(),
    )
    fake_db = _CapturingDB()
    monkeypatch.setattr(sportradar, "db", fake_db)

    n = asyncio.run(sportradar._poll_injuries(sport))
    assert n == 3, f"{sport}: expected 3 missing-player rows"
    assert fake_db.batches and len(fake_db.batches[0]) == 3
    league = sportradar._SPORTRADAR_LEAGUE[sport]
    assert all(row[0] == league for row in fake_db.batches[0])


@pytest.mark.parametrize("sport", SOCCER_LEAGUES)
def test_poll_soccer_rosters_writes_nonzero_rows(monkeypatch, sport):
    import asyncio
    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")
    profile_by_id = {
        "sr:competitor:1": _soccer_competitor_profile_fixture("sr:competitor:1", "Arsenal"),
        "sr:competitor:2": _soccer_competitor_profile_fixture("sr:competitor:2", "Chelsea"),
    }
    _install_soccer_http(
        monkeypatch,
        info=_soccer_tournament_info_fixture(),
        competitors=_soccer_competitors_fixture(),
        profile_by_id=profile_by_id,
        missing=_soccer_missing_players_fixture(),
        live=_soccer_live_schedule_fixture(),
    )
    fake_db = _CapturingDB()
    monkeypatch.setattr(sportradar, "db", fake_db)

    n = asyncio.run(sportradar._poll_rosters(sport))
    # 2 teams × 2 valid players each (the un-id'd "ghost" entry is filtered).
    assert n == 4, f"{sport}: expected 4 player rows from 2 teams"
    assert fake_db.batches and len(fake_db.batches[0]) == 4
    league = sportradar._SPORTRADAR_LEAGUE[sport]
    assert all(row[0] == league for row in fake_db.batches[0])
    teams = {row[1] for row in fake_db.batches[0]}
    assert teams == {"sr:competitor:1", "sr:competitor:2"}


@pytest.mark.parametrize("sport", SOCCER_LEAGUES)
def test_poll_soccer_live_state_writes_nonzero_rows(monkeypatch, sport):
    import asyncio
    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")
    _install_soccer_http(
        monkeypatch,
        info=_soccer_tournament_info_fixture(),
        competitors=_soccer_competitors_fixture(),
        profile_by_id={},
        missing=_soccer_missing_players_fixture(),
        live=_soccer_live_schedule_fixture(),
    )
    fake_db = _CapturingDB()
    monkeypatch.setattr(sportradar, "db", fake_db)

    n = asyncio.run(sportradar._poll_live_state(sport))
    assert n == 2, f"{sport}: expected 2 live/closed rows"
    assert fake_db.batches and len(fake_db.batches[0]) == 2
    league = sportradar._SPORTRADAR_LEAGUE[sport]
    assert all(row[0] == league for row in fake_db.batches[0])


def test_resolve_soccer_season_id_caches_per_tournament(monkeypatch):
    import asyncio
    calls: list[str] = []

    async def fake_get(name, url, key, **_kw):
        calls.append(url)
        return _soccer_tournament_info_fixture(season_id="sr:season:abc")

    monkeypatch.setattr(sportradar, "_get_json", fake_get)
    sid1 = asyncio.run(sportradar._resolve_soccer_season_id("epl", "k"))
    sid2 = asyncio.run(sportradar._resolve_soccer_season_id("epl", "k"))
    assert sid1 == "sr:season:abc"
    assert sid2 == "sr:season:abc"
    # Cached after first lookup.
    assert len(calls) == 1


def test_soccer_season_refreshes_on_404_after_rollover(monkeypatch):
    """When a soccer competition rolls into a new season the cached
    season id starts 404'ing on the seasons/{id}/missing_players (and
    competitors) endpoints.  The pollers must drop the cached id,
    re-resolve via tournaments/{tid}/info.json, and retry once with the
    fresh id — otherwise injuries/rosters silently flatline to zero
    rows until the API server restarts.

    First poll: cached old id, succeeds.
    Second poll: cached id 404s, helper re-resolves and the retry with
    the new id succeeds.
    """
    import asyncio
    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")

    season_state = {"current": "sr:season:OLD"}
    info_urls: list[str] = []
    missing_urls: list[str] = []

    async def fake_get(name, url, key, *, distinguish_404=False, **_kw):
        if "/tournaments/" in url and "/info.json" in url:
            info_urls.append(url)
            return _soccer_tournament_info_fixture(
                season_id=season_state["current"]
            )
        if "/missing_players.json" in url:
            missing_urls.append(url)
            if f"/seasons/{season_state['current']}/" in url:
                return _soccer_missing_players_fixture()
            # Stale season id -> 404.
            return sportradar._NOT_FOUND if distinguish_404 else None
        return None

    monkeypatch.setattr(sportradar, "_get_json", fake_get)
    fake_db = _CapturingDB()
    monkeypatch.setattr(sportradar, "db", fake_db)

    # First poll seeds the cache with OLD and writes 3 rows.
    n1 = asyncio.run(sportradar._poll_soccer_injuries("epl", "k"))
    assert n1 == 3
    tid = sportradar._SOCCER_TOURNAMENTS["epl"]
    assert sportradar._SOCCER_SEASON_CACHE[tid] == "sr:season:OLD"
    assert len(info_urls) == 1
    assert len(missing_urls) == 1
    assert "sr:season:OLD" in missing_urls[0]

    # Season rolls over at the data source.
    season_state["current"] = "sr:season:NEW"

    # Second poll: cached OLD id 404s -> re-resolve picks up NEW ->
    # retry with NEW succeeds.  The helper records all three of
    # "first call cached, second call 404s, third succeeds with new id"
    # against the missing_players endpoint.
    n2 = asyncio.run(sportradar._poll_soccer_injuries("epl", "k"))
    assert n2 == 3, "post-rollover poll should still write rows after refresh"
    assert sportradar._SOCCER_SEASON_CACHE[tid] == "sr:season:NEW"
    assert len(missing_urls) == 3
    assert "sr:season:OLD" in missing_urls[0]
    assert "sr:season:OLD" in missing_urls[1]  # the 404 attempt
    assert "sr:season:NEW" in missing_urls[2]  # the successful retry
    # Exactly one extra tournament-info refresh was performed.
    assert len(info_urls) == 2


def test_soccer_rosters_also_refresh_on_404(monkeypatch):
    """Same season-rollover behaviour must apply to the rosters poller —
    its season-keyed competitors.json call has the same failure mode as
    missing_players.json."""
    import asyncio
    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")

    season_state = {"current": "sr:season:OLD"}
    competitors_urls: list[str] = []

    async def fake_get(name, url, key, *, distinguish_404=False, **_kw):
        if "/tournaments/" in url and "/info.json" in url:
            return _soccer_tournament_info_fixture(
                season_id=season_state["current"]
            )
        if "/competitors.json" in url:
            competitors_urls.append(url)
            if f"/seasons/{season_state['current']}/" in url:
                return _soccer_competitors_fixture()
            return sportradar._NOT_FOUND if distinguish_404 else None
        if "/competitors/" in url and "/profile.json" in url:
            # Pull team id out of the url path.
            for tid in ("sr:competitor:1", "sr:competitor:2"):
                if f"/competitors/{tid}/" in url:
                    name_ = "Arsenal" if tid.endswith("1") else "Chelsea"
                    return _soccer_competitor_profile_fixture(tid, name_)
            return None
        return None

    monkeypatch.setattr(sportradar, "_get_json", fake_get)
    fake_db = _CapturingDB()
    monkeypatch.setattr(sportradar, "db", fake_db)

    # Seed the cache with OLD via the first poll.
    n1 = asyncio.run(sportradar._poll_soccer_rosters("epl", "k"))
    assert n1 == 4

    # Rollover.
    season_state["current"] = "sr:season:NEW"

    n2 = asyncio.run(sportradar._poll_soccer_rosters("epl", "k"))
    assert n2 == 4
    tid = sportradar._SOCCER_TOURNAMENTS["epl"]
    assert sportradar._SOCCER_SEASON_CACHE[tid] == "sr:season:NEW"
    # OLD success, OLD 404, NEW success.
    assert len(competitors_urls) == 3
    assert "sr:season:OLD" in competitors_urls[0]
    assert "sr:season:OLD" in competitors_urls[1]
    assert "sr:season:NEW" in competitors_urls[2]


def test_soccer_seasoned_helper_no_thrash_when_refresh_yields_same_id(monkeypatch):
    """If the cached id 404s but tournament info returns the same id (or
    nothing), the helper must give up immediately rather than thrashing
    the API on every poll."""
    import asyncio
    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")

    info_calls = 0
    missing_calls = 0

    async def fake_get(name, url, key, *, distinguish_404=False, **_kw):
        nonlocal info_calls, missing_calls
        if "/tournaments/" in url and "/info.json" in url:
            info_calls += 1
            return _soccer_tournament_info_fixture(season_id="sr:season:STUCK")
        if "/missing_players.json" in url:
            missing_calls += 1
            return sportradar._NOT_FOUND if distinguish_404 else None
        return None

    monkeypatch.setattr(sportradar, "_get_json", fake_get)
    fake_db = _CapturingDB()
    monkeypatch.setattr(sportradar, "db", fake_db)

    n = asyncio.run(sportradar._poll_soccer_injuries("epl", "k"))
    assert n == 0
    # We attempt missing_players exactly once (the refresh resolved the
    # same id, so we don't retry).
    assert missing_calls == 1
    # One initial info lookup + one forced refresh.
    assert info_calls == 2


def test_soccer_pollers_record_season_metadata_on_clean_poll(monkeypatch):
    """A clean (no rollover) soccer poll must stamp the cached season id
    onto the feed's `metadata` so the operator console can see *which*
    season the poller is pinned to even before a 404 ever happens."""
    import asyncio

    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")
    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    profile_by_id = {
        "sr:competitor:1": _soccer_competitor_profile_fixture("sr:competitor:1", "Arsenal"),
        "sr:competitor:2": _soccer_competitor_profile_fixture("sr:competitor:2", "Chelsea"),
    }
    _install_soccer_http(
        monkeypatch,
        info=_soccer_tournament_info_fixture(season_id="sr:season:CLEAN"),
        competitors=_soccer_competitors_fixture(),
        profile_by_id=profile_by_id,
        missing=_soccer_missing_players_fixture(),
        live=_soccer_live_schedule_fixture(),
    )
    fake_db = _CapturingDB()
    monkeypatch.setattr(sportradar, "db", fake_db)

    asyncio.run(sportradar._poll_injuries("epl"))
    asyncio.run(sportradar._poll_rosters("epl"))

    inj = reg.status_of("sportradar_epl_injuries")
    ros = reg.status_of("sportradar_epl_rosters")
    assert inj is not None and ros is not None
    for st in (inj, ros):
        assert st.metadata.get("season_id") == "sr:season:CLEAN"
        assert st.metadata.get("season_stuck") is False
        assert st.metadata.get("tournament_id") == sportradar._SOCCER_TOURNAMENTS["epl"]
        assert st.metadata.get("season_id_resolved_at"), (
            "resolved_at must be populated so the UI can show *when* "
            "the cached id was first picked up"
        )


def test_soccer_poll_metadata_records_rotation_after_404(monkeypatch):
    """When the cached season id 404s and the refresh yields a *new* id,
    the feed's metadata must record the rotation outcome + previous id
    so the operator console can show a "rolled over" badge."""
    import asyncio

    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")
    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    season_state = {"current": "sr:season:OLD"}

    async def fake_get(name, url, key, *, distinguish_404=False, **_kw):
        if "/tournaments/" in url and "/info.json" in url:
            return _soccer_tournament_info_fixture(
                season_id=season_state["current"]
            )
        if "/missing_players.json" in url:
            if f"/seasons/{season_state['current']}/" in url:
                return _soccer_missing_players_fixture()
            return sportradar._NOT_FOUND if distinguish_404 else None
        return None

    monkeypatch.setattr(sportradar, "_get_json", fake_get)
    fake_db = _CapturingDB()
    monkeypatch.setattr(sportradar, "db", fake_db)

    # First poll establishes OLD as the cached id.
    asyncio.run(sportradar._poll_injuries("epl"))
    inj = reg.status_of("sportradar_epl_injuries")
    assert inj is not None
    assert inj.metadata.get("season_id") == "sr:season:OLD"
    assert inj.metadata.get("season_stuck") is False
    # No refresh has happened yet so the rotation fields stay unset.
    assert inj.metadata.get("season_last_refresh_at") is None

    # Sportradar rolls over.
    season_state["current"] = "sr:season:NEW"
    asyncio.run(sportradar._poll_injuries("epl"))
    assert inj.metadata.get("season_id") == "sr:season:NEW"
    assert inj.metadata.get("season_previous_id") == "sr:season:OLD"
    assert inj.metadata.get("season_last_refresh_outcome") == "rotated"
    assert inj.metadata.get("season_stuck") is False
    assert inj.metadata.get("season_last_refresh_at"), (
        "rotation must be timestamped so the UI can decide when to "
        "stop showing the rolled-over badge"
    )


def test_soccer_poll_metadata_flags_stuck_when_refresh_yields_no_new_id(monkeypatch):
    """If the cached season id 404s and the refresh re-resolves to the
    same (still-stale) id, the feed's metadata must flip
    `season_stuck=True` so the operator console can render a clear
    warning instead of letting the poll look like a healthy zero-row
    success."""
    import asyncio

    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")
    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    async def fake_get(name, url, key, *, distinguish_404=False, **_kw):
        if "/tournaments/" in url and "/info.json" in url:
            return _soccer_tournament_info_fixture(season_id="sr:season:STUCK")
        if "/missing_players.json" in url:
            return sportradar._NOT_FOUND if distinguish_404 else None
        return None

    monkeypatch.setattr(sportradar, "_get_json", fake_get)
    fake_db = _CapturingDB()
    monkeypatch.setattr(sportradar, "db", fake_db)

    n = asyncio.run(sportradar._poll_injuries("epl"))
    assert n == 0
    inj = reg.status_of("sportradar_epl_injuries")
    assert inj is not None
    assert inj.metadata.get("season_stuck") is True
    assert inj.metadata.get("season_last_refresh_outcome") == "same_id"
    assert inj.metadata.get("season_id") == "sr:season:STUCK"


def test_soccer_season_history_is_bounded(monkeypatch):
    """The per-feed `season_history` ring buffer must hard-cap at
    :data:`sportradar._SEASON_HISTORY_MAX` entries so a feed that's
    been hammering the rotation path for weeks doesn't grow the
    metadata payload without bound (it ships with every health
    snapshot)."""
    import asyncio

    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")
    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    # Force every poll into the "same_id" branch — the cached id 404s
    # on the seasoned endpoint, the refresh re-resolves to the same id,
    # so each poll appends exactly one history entry.
    async def fake_get(name, url, key, *, distinguish_404=False, **_kw):
        if "/tournaments/" in url and "/info.json" in url:
            return _soccer_tournament_info_fixture(season_id="sr:season:STUCK")
        if "/missing_players.json" in url:
            return sportradar._NOT_FOUND if distinguish_404 else None
        return None

    monkeypatch.setattr(sportradar, "_get_json", fake_get)
    monkeypatch.setattr(sportradar, "db", _CapturingDB())

    n_polls = sportradar._SEASON_HISTORY_MAX + 5
    for _ in range(n_polls):
        asyncio.run(sportradar._poll_injuries("epl"))

    inj = reg.status_of("sportradar_epl_injuries")
    assert inj is not None
    history = inj.metadata.get("season_history")
    assert isinstance(history, list)
    # Hard cap respected — older entries fall off, newest stay.
    assert len(history) == sportradar._SEASON_HISTORY_MAX, (
        f"history grew unbounded: {len(history)} entries after {n_polls} polls"
    )
    # Every entry must have the documented shape so the React UI's
    # narrowing doesn't silently drop legitimate rows.
    for entry in history:
        assert set(entry.keys()) == {"at", "from_id", "to_id", "outcome"}
        assert entry["outcome"] == "same_id"
        assert entry["from_id"] == "sr:season:STUCK"
        assert entry["to_id"] == "sr:season:STUCK"
        assert isinstance(entry["at"], str) and entry["at"]


def test_soccer_season_history_records_rotation_outcome(monkeypatch):
    """A successful 404→re-resolve→new id rotation must append exactly
    one ``rotated`` history entry capturing the from/to ids so the
    operator console can show "X rolled into Y at <ts>" without diff'ing
    successive snapshots."""
    import asyncio

    monkeypatch.setenv("SPORTRADAR_API_KEY", "k")
    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    season_state = {"current": "sr:season:OLD"}

    async def fake_get(name, url, key, *, distinguish_404=False, **_kw):
        if "/tournaments/" in url and "/info.json" in url:
            return _soccer_tournament_info_fixture(
                season_id=season_state["current"]
            )
        if "/missing_players.json" in url:
            if f"/seasons/{season_state['current']}/" in url:
                return _soccer_missing_players_fixture()
            return sportradar._NOT_FOUND if distinguish_404 else None
        return None

    monkeypatch.setattr(sportradar, "_get_json", fake_get)
    monkeypatch.setattr(sportradar, "db", _CapturingDB())

    # First poll caches OLD without triggering a refresh — no history
    # entry is appended because nothing has actually rotated.
    asyncio.run(sportradar._poll_injuries("epl"))
    inj = reg.status_of("sportradar_epl_injuries")
    assert inj is not None
    assert inj.metadata.get("season_history", []) == []

    # Sportradar rolls over.  The cached OLD now 404s; the refresh
    # picks up NEW and the seasoned endpoint succeeds.
    season_state["current"] = "sr:season:NEW"
    asyncio.run(sportradar._poll_injuries("epl"))

    history = inj.metadata.get("season_history")
    assert isinstance(history, list) and len(history) == 1
    rotation = history[0]
    assert rotation["outcome"] == "rotated"
    assert rotation["from_id"] == "sr:season:OLD"
    assert rotation["to_id"] == "sr:season:NEW"
    assert rotation["at"]


def test_health_probe_renders_stuck_season_as_degraded(monkeypatch):
    """`_probe_feeds()` must promote a soccer feed whose metadata says
    `season_stuck=True` to status=degraded with a clear "season id
    stuck" detail, even if the underlying poll counters look healthy."""
    from app import health as health_mod
    from app.feeds.registry import Feed

    reg = _isolated_registry()

    async def _ok():
        return 0

    reg.register(
        Feed(
            name="sportradar_epl_injuries",
            cadence_seconds=900.0,
            poll=_ok,
            enabled=True,
        )
    )
    st = reg._status["sportradar_epl_injuries"]  # type: ignore[attr-defined]
    # Simulate a healthy-looking poll (one success, no error) but with
    # the stuck flag set by the soccer helper.
    st.poll_count = 1
    st.success_count = 1
    st.last_ok_at = 1_700_000_100.0
    st.last_finished_at = 1_700_000_100.0
    st.metadata = {
        "season_id": "sr:season:STUCK",
        "season_stuck": True,
        "season_last_refresh_outcome": "same_id",
        "season_last_refresh_at": "2026-04-26T00:00:00+00:00",
    }

    monkeypatch.setattr(health_mod, "feed_registry", reg)
    cards = health_mod._probe_feeds()
    assert len(cards) == 1
    card = cards[0]
    assert card["status"] == "degraded"
    assert "season id stuck" in card["detail"]
    assert "sr:season:STUCK" in card["detail"]
    # Metadata must round-trip through the card extras so the React
    # CardTile can render the "rolled over" / "stuck" badges.
    extras = card.get("extras") or {}
    meta = extras.get("metadata") or {}
    assert meta.get("season_stuck") is True
    assert meta.get("season_id") == "sr:season:STUCK"


def test_api_key_for_prefers_per_sport(monkeypatch):
    monkeypatch.setenv("SPORTRADAR_API_KEY", "global")
    monkeypatch.setenv("SPORTRADAR_NBA_KEY", "nba-specific")
    assert sportradar._api_key_for("nba") == "nba-specific"
    assert sportradar._api_key_for("nhl") == "global"
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_NBA_KEY", raising=False)
    assert sportradar._api_key_for("nba") is None


# ---------------------------------------------------------------------------
# Combat & individual sport schedule normalization (task #214).
#
# Sportradar's MMA v2, tennis v3, F1 v2 summaries and PGA v3 schedule
# endpoints each ship a different envelope shape; without these tests
# the new pollers would silently ingest zero rows because the previous
# normalizer only knew the NA/soccer envelopes.
# ---------------------------------------------------------------------------


def _norm(league_code: str, payload: dict) -> list[dict]:
    from app.feeds.odds_api import _normalize_sportradar_payload
    return _normalize_sportradar_payload(league_code.lower(), league_code, payload)


def test_normalize_ufc_fight_card_aggregates_to_one_row():
    """A UFC numbered card lists every bout in `sport_events`.  Each
    bout shares `sport_event_context.competition.id` (the card), so we
    expect exactly one game row whose id == the card id and whose
    competitor field contains the union of fighters."""
    payload = {
        "sport_events": [
            {
                "id": "sr:sport_event:bout-1",
                "start_time": "2026-04-25T22:00:00Z",
                "sport_event_context": {
                    "competition": {"id": "sr:competition:ufc-300", "name": "UFC 300"},
                    "season": {"id": "sr:season:mma-2026", "name": "MMA 2026"},
                },
                "competitors": [
                    {"id": "c1", "name": "Fighter A"},
                    {"id": "c2", "name": "Fighter B"},
                ],
            },
            {
                "id": "sr:sport_event:bout-2",
                "start_time": "2026-04-25T23:30:00Z",
                "sport_event_context": {
                    "competition": {"id": "sr:competition:ufc-300", "name": "UFC 300"},
                    "season": {"id": "sr:season:mma-2026"},
                },
                "competitors": [
                    {"id": "c3", "name": "Fighter C"},
                    {"id": "c4", "name": "Fighter D"},
                ],
            },
            {
                "id": "sr:sport_event:bout-3",
                "start_time": "2026-04-25T21:00:00Z",  # earliest bout
                "sport_event_context": {
                    "competition": {"id": "sr:competition:ufc-300", "name": "UFC 300"},
                },
                "sport_event_status": {"status": "inprogress"},
                "competitors": [
                    {"id": "c5", "name": "Fighter E"},
                    {"id": "c6", "name": "Fighter F"},
                ],
            },
        ]
    }
    rows = _norm("UFC", payload)
    assert len(rows) == 1, "all bouts should collapse into one fight-card row"
    row = rows[0]
    assert row["id"] == "sr:competition:ufc-300"
    assert row["home"]["name"] == "UFC 300"
    assert row["away"]["name"] == "FIELD"
    # Earliest scheduled bout becomes the card kickoff.
    assert row["scheduled"] == "2026-04-25T21:00:00Z"
    # Live child promotes the parent so event_in_progress fires.
    assert row["status"] == "inprogress"
    # 6 unique fighters across 3 bouts -> Plackett-Luce field metadata.
    assert "n_way_field" in row
    assert {c["name"] for c in row["n_way_field"]} == {
        "Fighter A", "Fighter B", "Fighter C", "Fighter D", "Fighter E", "Fighter F"
    }


def test_normalize_atp_tournament_aggregates_matches():
    """Tennis daily schedule lists individual matches; we want one row
    per tournament so the per-event cap gates the whole bracket."""
    payload = {
        "sport_events": [
            {
                "id": "sr:sport_event:m1",
                "start_time": "2026-05-25T12:00:00Z",
                "sport_event_context": {
                    "competition": {"id": "sr:competition:roland-garros", "name": "Roland Garros"},
                },
                "competitors": [
                    {"id": "p1", "name": "Player One"},
                    {"id": "p2", "name": "Player Two"},
                ],
            },
            {
                "id": "sr:sport_event:m2",
                "start_time": "2026-05-25T14:00:00Z",
                "sport_event_context": {
                    "competition": {"id": "sr:competition:roland-garros", "name": "Roland Garros"},
                },
                "competitors": [
                    {"id": "p3", "name": "Player Three"},
                    {"id": "p1", "name": "Player One"},  # duplicate id dedupes
                ],
            },
        ]
    }
    rows = _norm("ATP", payload)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "sr:competition:roland-garros"
    assert row["home"]["name"] == "Roland Garros"
    # 3 unique players across both matches.
    assert len(row["n_way_field"]) == 3


def test_normalize_f1_summaries_aggregates_sessions():
    """F1 summaries return one entry per session (FP1/FP2/FP3/Q/Race);
    we want a single row per race weekend so betting on the GP is
    treated as one tradable event."""
    payload = {
        "summaries": [
            {
                "sport_event": {
                    "id": "sr:sport_event:fp1",
                    "start_time": "2026-04-24T12:30:00Z",
                    "sport_event_context": {
                        "stage": {"id": "sr:stage:bahrain-2026",
                                  "description": "Bahrain Grand Prix"},
                        "season": {"id": "sr:season:f1-2026"},
                    },
                    "competitors": [
                        {"id": d, "name": f"Driver {d}"} for d in ("d1", "d2", "d3")
                    ],
                },
                "sport_event_status": {"status": "closed"},
            },
            {
                "sport_event": {
                    "id": "sr:sport_event:race",
                    "start_time": "2026-04-26T15:00:00Z",
                    "sport_event_context": {
                        "stage": {"id": "sr:stage:bahrain-2026",
                                  "description": "Bahrain Grand Prix"},
                    },
                    "competitors": [
                        {"id": d, "name": f"Driver {d}"}
                        for d in ("d1", "d2", "d3", "d4", "d5")
                    ],
                },
                "sport_event_status": {"status": "scheduled"},
            },
        ]
    }
    rows = _norm("F1", payload)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "sr:stage:bahrain-2026"
    assert row["home"]["name"] == "Bahrain Grand Prix"
    assert row["scheduled"] == "2026-04-24T12:30:00Z"  # earliest session
    # 5 unique drivers across both sessions -> Plackett-Luce grid.
    assert len(row["n_way_field"]) == 5


def test_normalize_pga_tournament_schedule_emits_one_row_per_event():
    """PGA schedule.json returns `{tournaments: [...]}` with one entry
    per tournament; each is independently tradable as an outright."""
    payload = {
        "tournaments": [
            {
                "id": "sr:tournament:masters-2026",
                "name": "The Masters",
                "start_date": "2026-04-09",
                "status": "scheduled",
            },
            {
                "id": "sr:tournament:rbc-2026",
                "name": "RBC Heritage",
                "start_date": "2026-04-16",
                "status": "scheduled",
            },
            {"id": "", "name": "Skip me — missing id"},
        ]
    }
    rows = _norm("PGA", payload)
    assert len(rows) == 2
    ids = [r["id"] for r in rows]
    assert "sr:tournament:masters-2026" in ids
    assert "sr:tournament:rbc-2026" in ids
    assert all(r["away"]["name"] == "FIELD" for r in rows)


def test_poll_tournament_leaderboards_records_per_cycle_metadata(monkeypatch):
    """Task #624: each cycle stashes the live-events count, the most
    recent refresh count, and the timestamp on feed metadata so the
    operator console can render them next to the other Sportradar
    entries.  When the refresher writes 0 rows while events are live,
    `zero_rows_since` is set so the console can fire a stale badge
    after >10 minutes.  A non-zero refresh clears the streak.
    """
    monkeypatch.setenv("SPORTRADAR_PGA_KEY", "pga-test-key")
    monkeypatch.delenv("SPORTRADAR_F1_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)

    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    async def _live_ids(sport: str) -> list[str]:
        return ["sr:tournament:masters-2026"] if sport == "PGA" else []

    refresh_returns = iter([0, 7])

    async def _refresh_pga(api_key, tid):  # noqa: ANN001
        return next(refresh_returns)

    monkeypatch.setattr(sportradar, "_live_leaderboard_event_ids", _live_ids)
    monkeypatch.setattr(sportradar, "_refresh_pga_leaderboard", _refresh_pga)

    import asyncio as _asyncio
    # First cycle: 0 rows refreshed but a live event ⇒ streak starts.
    written = _asyncio.run(sportradar._poll_tournament_leaderboards())
    assert written == 0
    st = reg.status_of(sportradar.TOURNAMENT_LEADERBOARDS_FEED_NAME)
    assert st is not None
    meta = st.metadata
    assert meta.get("events_live") == 1
    assert meta.get("last_refresh_count") == 0
    assert meta.get("zero_rows_since")
    streak_started = meta["zero_rows_since"]
    assert isinstance(streak_started, str)
    assert isinstance(meta.get("per_event"), list) and len(meta["per_event"]) == 1
    assert meta["per_event"][0]["sport"] == "PGA"
    assert meta["per_event"][0]["entrants"] == 0

    # Second cycle: same event, 7 entrants refreshed ⇒ streak cleared,
    # last_refresh_count updated.
    written = _asyncio.run(sportradar._poll_tournament_leaderboards())
    assert written == 7
    meta = st.metadata
    assert meta.get("last_refresh_count") == 7
    assert meta.get("events_live") == 1
    assert meta.get("zero_rows_since") is None
    assert meta["per_event"][0]["entrants"] == 7


def test_poll_tournament_leaderboards_streak_persists_across_zero_cycles(monkeypatch):
    """The zero-row streak's `since` timestamp must NOT advance on
    subsequent zero-row cycles — otherwise the >10min badge would
    never fire on a poller that ticks more often than every 10
    minutes.
    """
    monkeypatch.setenv("SPORTRADAR_PGA_KEY", "pga-test-key")
    monkeypatch.delenv("SPORTRADAR_F1_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)

    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    async def _live_ids(sport: str) -> list[str]:
        return ["sr:tournament:masters-2026"] if sport == "PGA" else []

    async def _refresh_pga(api_key, tid):  # noqa: ANN001
        return 0

    monkeypatch.setattr(sportradar, "_live_leaderboard_event_ids", _live_ids)
    monkeypatch.setattr(sportradar, "_refresh_pga_leaderboard", _refresh_pga)

    import asyncio as _asyncio
    _asyncio.run(sportradar._poll_tournament_leaderboards())
    st = reg.status_of(sportradar.TOURNAMENT_LEADERBOARDS_FEED_NAME)
    assert st is not None
    first_since = st.metadata.get("zero_rows_since")
    assert first_since
    # Second poll a fraction later — `zero_rows_since` must stay pinned
    # to the original timestamp so the >10min check stays accurate.
    _asyncio.run(sportradar._poll_tournament_leaderboards())
    assert st.metadata.get("zero_rows_since") == first_since


def test_poll_tournament_leaderboards_clears_streak_when_no_events_live(monkeypatch):
    """If events drop off (no live tournaments) the zero-row streak
    must clear so the badge doesn't keep firing in the off-season."""
    monkeypatch.setenv("SPORTRADAR_PGA_KEY", "pga-test-key")
    monkeypatch.delenv("SPORTRADAR_F1_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)

    reg = _isolated_registry()
    monkeypatch.setattr(sportradar, "registry", reg)
    sportradar.register_sportradar_extra_feeds()

    live_ids: list[str] = ["sr:tournament:masters-2026"]

    async def _live(sport: str) -> list[str]:
        return list(live_ids) if sport == "PGA" else []

    async def _refresh_pga(api_key, tid):  # noqa: ANN001
        return 0

    monkeypatch.setattr(sportradar, "_live_leaderboard_event_ids", _live)
    monkeypatch.setattr(sportradar, "_refresh_pga_leaderboard", _refresh_pga)

    import asyncio as _asyncio
    _asyncio.run(sportradar._poll_tournament_leaderboards())
    st = reg.status_of(sportradar.TOURNAMENT_LEADERBOARDS_FEED_NAME)
    assert st is not None
    assert st.metadata.get("zero_rows_since")

    # Tournament wraps up — no more live events ⇒ streak clears.
    live_ids.clear()
    _asyncio.run(sportradar._poll_tournament_leaderboards())
    assert st.metadata.get("zero_rows_since") is None
    assert st.metadata.get("events_live") == 0
    assert st.metadata.get("last_refresh_count") == 0


def test_normalize_combat_payload_skips_entries_without_grouping_id_safely():
    """Defensive guard: an entry missing every grouping key falls back
    to its own id rather than silently dropping out."""
    payload = {
        "sport_events": [
            {
                "id": "sr:sport_event:loose-bout",
                "start_time": "2026-04-25T22:00:00Z",
                "competitors": [
                    {"id": "c1", "name": "A"}, {"id": "c2", "name": "B"},
                ],
            },
        ]
    }
    rows = _norm("UFC", payload)
    assert len(rows) == 1
    assert rows[0]["id"] == "sr:sport_event:loose-bout"
