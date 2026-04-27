"""Tests for the ESPN public-scoreboard schedule poller."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.feeds import espn_scoreboard as esp


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_scoreboard_url_returns_known_path():
    assert esp._scoreboard_url("NFL") == (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    )
    assert esp._scoreboard_url("nba") == (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    )


def test_scoreboard_url_unknown_league_returns_none():
    assert esp._scoreboard_url("XFL") is None
    assert esp._scoreboard_url("") is None


def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("AXIOM_ESPN_SCOREBOARD_DISABLED", raising=False)
    assert esp._is_enabled() is True


@pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "on"])
def test_is_enabled_respects_disable_flag(monkeypatch, v):
    monkeypatch.setenv("AXIOM_ESPN_SCOREBOARD_DISABLED", v)
    assert esp._is_enabled() is False


def test_cadence_floor_at_60s(monkeypatch):
    monkeypatch.setenv("AXIOM_ESPN_SCOREBOARD_CADENCE_SECS", "5")
    assert esp._cadence_seconds() == 60.0


def test_cadence_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("AXIOM_ESPN_SCOREBOARD_CADENCE_SECS", "not-a-number")
    assert esp._cadence_seconds() == esp._DEFAULT_CADENCE_SECS


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_parse_iso8601_handles_z_and_offset():
    a = esp._parse_iso8601("2026-04-15T20:00:00Z")
    b = esp._parse_iso8601("2026-04-15T20:00:00+00:00")
    assert a == b == datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc)
    assert esp._parse_iso8601(None) is None
    assert esp._parse_iso8601("garbage") is None


def test_competitor_team_name_prefers_displayName():
    assert esp._competitor_team_name(
        {"team": {"displayName": "Philadelphia Eagles", "name": "Eagles"}}
    ) == "Philadelphia Eagles"
    assert esp._competitor_team_name({"team": {"name": "Eagles"}}) == "Eagles"
    assert esp._competitor_team_name({"team": {}}) is None
    assert esp._competitor_team_name({}) is None


def test_competition_status_falls_back_to_event():
    comp = {"status": {"type": {}}}
    event = {"status": {"type": {"state": "PRE"}}}
    assert esp._competition_status(comp, event) == "pre"


def test_venue_name_handles_full_and_short():
    assert esp._venue_name({"venue": {"fullName": "Lincoln Financial"}}) == "Lincoln Financial"
    assert esp._venue_name({"venue": {"name": "Linc"}}) == "Linc"
    assert esp._venue_name({}) is None


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------

def _sample_event() -> dict[str, Any]:
    return {
        "id": "401547401",
        "date": "2026-11-02T17:00:00Z",
        "name": "Philadelphia Eagles at Buffalo Bills",
        "shortName": "PHI @ BUF",
        "status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED"}},
        "competitions": [{
            "venue": {"fullName": "Highmark Stadium"},
            "status": {"type": {"state": "pre"}},
            "competitors": [
                {"homeAway": "home",
                 "team": {"displayName": "Buffalo Bills"}},
                {"homeAway": "away",
                 "team": {"displayName": "Philadelphia Eagles"}},
            ],
        }],
    }


def test_row_from_event_full_payload():
    row = esp._row_from_event("NFL", _sample_event())
    assert row is not None
    league, game_id, home, away, scheduled, status, venue, payload = row
    assert league == "NFL"
    assert game_id == "espn:nfl:401547401"
    assert home == "Buffalo Bills"
    assert away == "Philadelphia Eagles"
    assert scheduled == datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
    assert status == "pre"
    assert venue == "Highmark Stadium"
    body = json.loads(payload)
    assert body["source"] == "espn"
    assert body["espn_event_id"] == "401547401"
    assert body["league"] == "NFL"


def test_row_from_event_falls_back_to_short_name_when_homeAway_missing():
    ev = _sample_event()
    # Strip homeAway flags so the parser has to use shortName.
    for c in ev["competitions"][0]["competitors"]:
        c.pop("homeAway", None)
    row = esp._row_from_event("NFL", ev)
    assert row is not None
    _, _, home, away, *_ = row
    # shortName "PHI @ BUF" → away=PHI, home=BUF
    assert away == "PHI"
    assert home == "BUF"


def test_row_from_event_returns_none_when_no_id():
    ev = _sample_event()
    ev["id"] = ""
    assert esp._row_from_event("NFL", ev) is None


def test_row_from_event_returns_none_when_no_teams():
    ev = _sample_event()
    ev["competitions"][0]["competitors"] = []
    ev["shortName"] = ""
    assert esp._row_from_event("NFL", ev) is None


def test_row_from_event_returns_none_when_no_date():
    ev = _sample_event()
    ev["date"] = None
    ev["competitions"][0].pop("date", None)
    assert esp._row_from_event("NFL", ev) is None


def test_rows_from_scoreboard_skips_bad_events():
    body = {
        "events": [
            _sample_event(),
            {"id": "missing-everything"},  # no teams, no date
            "garbage",  # not even a dict
        ],
    }
    rows = esp._rows_from_scoreboard("NFL", body)
    assert len(rows) == 1
    assert rows[0][1] == "espn:nfl:401547401"


# ---------------------------------------------------------------------------
# Upsert + poll integration (with stubbed db / httpx)
# ---------------------------------------------------------------------------

def test_upsert_rows_executes_one_executemany(monkeypatch):
    captured: dict[str, Any] = {}

    class _Conn:
        async def executemany(self, sql: str, rows):
            captured["sql"] = sql
            captured["rows"] = rows

        def transaction(self):
            class _Tx:
                async def __aenter__(self_inner): return None
                async def __aexit__(self_inner, *a): return False
            return _Tx()

    class _Pool:
        def acquire(self):
            class _Cm:
                async def __aenter__(self_inner): return _Conn()
                async def __aexit__(self_inner, *a): return False
            return _Cm()

    monkeypatch.setattr(esp.db, "acquire", _Pool().acquire)

    rows = [
        ("NFL", "espn:nfl:1", "Bills", "Eagles",
         datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc),
         "pre", "Highmark", "{}"),
    ]
    n = asyncio.run(esp._upsert_rows(rows))
    assert n == 1
    assert "INSERT INTO sportradar_schedules" in captured["sql"]
    assert "ON CONFLICT (league, game_id)" in captured["sql"]
    assert captured["rows"] == rows


def test_upsert_rows_propagates_db_error(monkeypatch):
    """A failing upsert must raise so the scheduler records the
    failure in FeedStatus — silently returning 0 would let a broken
    writer impersonate a healthy feed."""
    class _Conn:
        async def executemany(self, sql, rows):
            raise RuntimeError("connection lost")

        def transaction(self):
            class _Tx:
                async def __aenter__(self_inner): return None
                async def __aexit__(self_inner, *a): return False
            return _Tx()

    class _Pool:
        def acquire(self):
            class _Cm:
                async def __aenter__(self_inner): return _Conn()
                async def __aexit__(self_inner, *a): return False
            return _Cm()

    monkeypatch.setattr(esp.db, "acquire", _Pool().acquire)
    rows = [("NFL", "espn:nfl:1", "Bills", "Eagles",
             datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc),
             "pre", "Highmark", "{}")]
    with pytest.raises(RuntimeError, match="connection lost"):
        asyncio.run(esp._upsert_rows(rows))


def test_fetch_scoreboard_raises_ratelimited_on_429(monkeypatch):
    """A 429 must raise RateLimited(retry_after=...) so the scheduler
    enforces a real cooldown instead of hammering the endpoint and
    spamming the log every 10 minutes."""
    from app.feeds.scheduler import RateLimited

    class _Resp:
        status_code = 429
        headers = {"Retry-After": "120"}

        def raise_for_status(self):  # pragma: no cover - not reached
            raise AssertionError("raise_for_status should not run on 429 path")

        def json(self):  # pragma: no cover - not reached
            return {}

    class _Client:
        async def get(self, url):
            return _Resp()

    with pytest.raises(RateLimited) as ei:
        asyncio.run(esp._fetch_scoreboard(_Client(), "NFL"))
    assert ei.value.retry_after == 120.0


def test_fetch_scoreboard_429_uses_default_when_retry_after_missing(monkeypatch):
    from app.feeds.scheduler import RateLimited

    class _Resp:
        status_code = 429
        headers: dict[str, str] = {}

        def raise_for_status(self):  # pragma: no cover - not reached
            raise AssertionError("not reached")

        def json(self):  # pragma: no cover
            return {}

    class _Client:
        async def get(self, url):
            return _Resp()

    with pytest.raises(RateLimited) as ei:
        asyncio.run(esp._fetch_scoreboard(_Client(), "NBA"))
    # Default floor of 60s, also bounded by max(30, ...) safety floor
    assert ei.value.retry_after >= 30.0


def test_upsert_rows_noop_on_empty():
    assert asyncio.run(esp._upsert_rows([])) == 0


def test_poll_skips_when_disabled(monkeypatch):
    monkeypatch.setenv("AXIOM_ESPN_SCOREBOARD_DISABLED", "1")
    assert asyncio.run(esp.poll_espn_scoreboard()) == 0


def test_poll_writes_rows_for_each_league(monkeypatch):
    # Stub the per-league fetch to return one parseable event per league.
    async def fake_fetch(client, league):
        ev = _sample_event()
        ev["id"] = f"{league}-1"
        return {"events": [ev]}

    captured: list = []

    async def fake_upsert(rows):
        rows_list = list(rows)
        captured.extend(rows_list)
        return len(rows_list)

    monkeypatch.delenv("AXIOM_ESPN_SCOREBOARD_DISABLED", raising=False)
    monkeypatch.setattr(esp, "_fetch_scoreboard", fake_fetch)
    monkeypatch.setattr(esp, "_upsert_rows", fake_upsert)
    # Make sure the feed registry has a status to write metadata into.
    from app.feeds.registry import Feed, registry as _reg
    if not _reg.status_of("espn_scoreboard"):
        _reg.register(Feed(
            name="espn_scoreboard",
            cadence_seconds=600.0,
            poll=esp.poll_espn_scoreboard,
        ))

    n = asyncio.run(esp.poll_espn_scoreboard())
    assert n == len(esp._ESPN_LEAGUES)
    leagues = sorted({r[0] for r in captured})
    assert leagues == sorted(esp._ESPN_LEAGUES.keys())
    # Metadata should record per-league fan-out for the operator console.
    meta = _reg.status_of("espn_scoreboard").metadata
    assert sorted(meta["leagues_polled"]) == sorted(esp._ESPN_LEAGUES.keys())
    assert sorted(meta["leagues_with_rows"]) == sorted(esp._ESPN_LEAGUES.keys())


def test_poll_isolated_per_league_failure(monkeypatch):
    # NHL fetch fails; the other three leagues still produce rows.
    async def fake_fetch(client, league):
        if league == "NHL":
            return None
        ev = _sample_event()
        ev["id"] = f"{league}-1"
        return {"events": [ev]}

    captured: list = []

    async def fake_upsert(rows):
        rows_list = list(rows)
        captured.extend(rows_list)
        return len(rows_list)

    monkeypatch.delenv("AXIOM_ESPN_SCOREBOARD_DISABLED", raising=False)
    monkeypatch.setattr(esp, "_fetch_scoreboard", fake_fetch)
    monkeypatch.setattr(esp, "_upsert_rows", fake_upsert)

    n = asyncio.run(esp.poll_espn_scoreboard())
    leagues = {r[0] for r in captured}
    assert "NHL" not in leagues
    assert {"NFL", "NBA", "MLB"}.issubset(leagues)
    assert n == 3
