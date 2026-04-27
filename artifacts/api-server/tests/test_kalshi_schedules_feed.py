"""Tests for the Kalshi schedule synthesizer."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.feeds import kalshi_schedules as ks


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_default_series_map_covers_major_leagues():
    m = ks._series_to_league()
    assert m["KXNFLGAME"] == "NFL"
    assert m["KXNBAGAME"] == "NBA"
    assert m["KXMLBGAME"] == "MLB"
    assert m["KXNHLGAME"] == "NHL"
    assert m["KXEPLGAME"] == "EPL"


def test_series_env_override(monkeypatch):
    monkeypatch.setenv(
        "AXIOM_KALSHI_SCHEDULE_SERIES",
        "KXFOO=FOO_LEAGUE, KXBAR=BAR_LEAGUE",
    )
    m = ks._series_to_league()
    assert m == {"KXFOO": "FOO_LEAGUE", "KXBAR": "BAR_LEAGUE"}


def test_series_env_falls_back_when_garbage(monkeypatch):
    monkeypatch.setenv("AXIOM_KALSHI_SCHEDULE_SERIES", "no-equals-here")
    # Garbage entry → fall back to defaults rather than empty map.
    m = ks._series_to_league()
    assert "KXNFLGAME" in m


def test_cadence_floors_at_60s(monkeypatch):
    monkeypatch.setenv("AXIOM_KALSHI_SCHEDULES_CADENCE_SECS", "1")
    assert ks._cadence_seconds() == 60.0


# ---------------------------------------------------------------------------
# Title parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Eagles vs Bills", ("Eagles", "Bills")),
    ("Eagles @ Bills", ("Eagles", "Bills")),
    ("Eagles VS. Bills", ("Eagles", "Bills")),
    ("Will the Eagles beat the Bills?", ("Eagles", "Bills")),
    ("Will Liverpool beat Manchester City?", ("Liverpool", "Manchester City")),
    ("Eagles vs Bills - Nov 2", ("Eagles", "Bills")),
    ("Eagles @ Bills on Nov 2, 2026", ("Eagles", "Bills")),
])
def test_parse_teams_from_title(title, expected):
    assert ks._parse_teams_from_title(title) == expected


@pytest.mark.parametrize("title", [
    None, "", "BTC closes above 50000", "single token",
    "X vs X",  # same name on both sides → reject
])
def test_parse_teams_from_title_returns_none(title):
    assert ks._parse_teams_from_title(title) is None


def test_strip_trailing_date_handles_dash_and_on():
    assert ks._strip_trailing_date("Bills - Nov 2") == "Bills"
    assert ks._strip_trailing_date("Bills - Nov 2, 2026") == "Bills"
    assert ks._strip_trailing_date("Bills on Nov 2") == "Bills"
    assert ks._strip_trailing_date("Bills?") == "Bills"


# ---------------------------------------------------------------------------
# Market shape helpers
# ---------------------------------------------------------------------------

def test_event_key_prefers_event_ticker():
    assert ks._event_key({"event_ticker": "KXNFLGAME-25NOV02PHIBUF"}) == "KXNFLGAME-25NOV02PHIBUF"
    assert ks._event_key({"ticker": "KXNFLGAME-25NOV02PHIBUF-PHI"}) == "KXNFLGAME-25NOV02PHIBUF-PHI"
    assert ks._event_key({}) is None


def test_parse_close_time_handles_z_and_offset():
    assert ks._parse_close_time("2026-11-02T17:00:00Z") == datetime(
        2026, 11, 2, 17, 0, tzinfo=timezone.utc,
    )
    assert ks._parse_close_time(None) is None


# ---------------------------------------------------------------------------
# Event collapse → schedule row
# ---------------------------------------------------------------------------

def _markets_for_phi_buf() -> list[dict[str, Any]]:
    return [
        {
            "ticker": "KXNFLGAME-25NOV02PHIBUF-PHI",
            "event_ticker": "KXNFLGAME-25NOV02PHIBUF",
            "title": "Will the Eagles beat the Bills on Nov 2?",
            "close_time": "2026-11-02T17:00:00Z",
            "status": "open",
            "venue": "Highmark Stadium",
        },
        {
            "ticker": "KXNFLGAME-25NOV02PHIBUF-BUF",
            "event_ticker": "KXNFLGAME-25NOV02PHIBUF",
            "title": "Will the Bills beat the Eagles on Nov 2?",
            "close_time": "2026-11-02T17:30:00Z",
            "status": "open",
        },
    ]


def test_collapse_event_full_payload():
    row = ks._collapse_event("NFL", "KXNFLGAME", _markets_for_phi_buf())
    assert row is not None
    league, game_id, home, away, scheduled, status, venue, payload = row
    assert league == "NFL"
    assert game_id == "kalshi:KXNFLGAME-25NOV02PHIBUF"
    # _parse_teams_from_title("Will the Eagles beat the Bills?") → (Eagles, Bills)
    # which the collapser stores as (away, home).
    assert away == "Eagles"
    assert home == "Bills"
    # Earliest close_time across the event's markets wins.
    assert scheduled == datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
    assert status == "scheduled"
    assert venue == "Highmark Stadium"
    body = json.loads(payload)
    assert body["source"] == "kalshi"
    assert body["kalshi_event_ticker"] == "KXNFLGAME-25NOV02PHIBUF"
    assert body["kalshi_market_count"] == 2


def test_collapse_event_returns_none_when_no_titles_match():
    markets = [{
        "ticker": "KXFOO-1",
        "event_ticker": "KXFOO",
        "title": "BTC closes above 50000",
        "close_time": "2026-11-02T17:00:00Z",
        "status": "open",
    }]
    assert ks._collapse_event("NFL", "KXFOO", markets) is None


def test_collapse_event_status_settled_when_no_open_markets():
    markets = _markets_for_phi_buf()
    for m in markets:
        m["status"] = "settled"
    row = ks._collapse_event("NFL", "KXNFLGAME", markets)
    assert row is not None
    assert row[5] == "closed"


def test_collapse_event_returns_none_on_empty_event_key():
    markets = [{"title": "Eagles vs Bills", "close_time": "2026-11-02T17:00:00Z"}]
    # No ticker, no event_ticker → no key → skip.
    assert ks._collapse_event("NFL", "KXNFLGAME", markets) is None


# ---------------------------------------------------------------------------
# Bucketing + upsert
# ---------------------------------------------------------------------------

def test_bucket_by_event_groups_two_markets_into_one_bucket():
    buckets = ks._bucket_by_event(_markets_for_phi_buf())
    assert list(buckets.keys()) == ["KXNFLGAME-25NOV02PHIBUF"]
    assert len(buckets["KXNFLGAME-25NOV02PHIBUF"]) == 2


def test_upsert_rows_executes_one_executemany(monkeypatch):
    captured: dict[str, Any] = {}

    class _Conn:
        async def executemany(self, sql, rows):
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

    monkeypatch.setattr(ks.db, "acquire", _Pool().acquire)

    rows = [
        ("NFL", "kalshi:K1", "Bills", "Eagles",
         datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc),
         "scheduled", None, "{}"),
    ]
    n = asyncio.run(ks._upsert_rows(rows))
    assert n == 1
    assert "sportradar_schedules" in captured["sql"]
    assert "ON CONFLICT (league, game_id)" in captured["sql"]


def test_upsert_rows_propagates_db_error(monkeypatch):
    """A failing upsert must raise so the scheduler records the
    failure in FeedStatus — silently returning 0 would let a broken
    writer impersonate a healthy feed."""
    class _Conn:
        async def executemany(self, sql, rows):
            raise RuntimeError("boom")

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

    monkeypatch.setattr(ks.db, "acquire", _Pool().acquire)
    rows = [("NFL", "kalshi:K1", "Bills", "Eagles",
             datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc),
             "scheduled", None, "{}")]
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(ks._upsert_rows(rows))


def test_parse_teams_handles_at_separator():
    """ESPN-style "Eagles at Bills" must parse cleanly without
    confusing team names that *contain* "at"."""
    assert ks._parse_teams_from_title("Eagles at Bills") == ("Eagles", "Bills")
    assert ks._parse_teams_from_title("Atlanta Falcons at New Orleans Saints") == (
        "Atlanta Falcons", "New Orleans Saints",
    )
    # Make sure "at" inside a token doesn't get mistaken for the
    # separator: this title has no real separator → returns None
    assert ks._parse_teams_from_title("Atletico") is None


# ---------------------------------------------------------------------------
# Poll: end-to-end with stubbed Kalshi client
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, by_series: dict[str, list[dict[str, Any]]]):
        self._by_series = by_series

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def list_markets(self, *, limit, status, series_ticker, cursor):
        return {"markets": self._by_series.get(series_ticker, []), "cursor": None}


def test_poll_skips_when_no_kalshi_creds(monkeypatch):
    class _Settings:
        kalshi_api_key_id = None
        kalshi_private_key = None
    monkeypatch.setattr(ks, "get_settings", lambda: _Settings())
    assert asyncio.run(ks.poll_kalshi_schedules()) == 0


def test_poll_synthesizes_schedule_rows(monkeypatch):
    class _Settings:
        kalshi_api_key_id = "id"
        kalshi_private_key = "key"
    monkeypatch.setattr(ks, "get_settings", lambda: _Settings())

    # Limit to a single series so the test is hermetic.
    monkeypatch.setenv("AXIOM_KALSHI_SCHEDULE_SERIES", "KXNFLGAME=NFL")

    fake = _FakeClient({"KXNFLGAME": _markets_for_phi_buf()})
    monkeypatch.setattr(
        ks.kalshi_client_mod, "KalshiClient", lambda *a, **kw: fake,
    )

    captured: list = []

    async def fake_upsert(rows):
        rows_list = list(rows)
        captured.extend(rows_list)
        return len(rows_list)

    monkeypatch.setattr(ks, "_upsert_rows", fake_upsert)

    # Make sure the registry knows about this feed so set_metadata works.
    from app.feeds.registry import Feed, registry as _reg
    if not _reg.status_of("kalshi_schedules"):
        _reg.register(Feed(
            name="kalshi_schedules",
            cadence_seconds=300.0,
            poll=ks.poll_kalshi_schedules,
        ))

    n = asyncio.run(ks.poll_kalshi_schedules())
    assert n == 1
    assert captured[0][0] == "NFL"
    assert captured[0][1] == "kalshi:KXNFLGAME-25NOV02PHIBUF"
    meta = _reg.status_of("kalshi_schedules").metadata
    assert meta["series_polled"] == ["KXNFLGAME"]
    assert meta["series_with_rows"] == ["KXNFLGAME"]


def test_poll_isolates_per_series_failure(monkeypatch):
    class _Settings:
        kalshi_api_key_id = "id"
        kalshi_private_key = "key"
    monkeypatch.setattr(ks, "get_settings", lambda: _Settings())
    monkeypatch.setenv(
        "AXIOM_KALSHI_SCHEDULE_SERIES", "KXNFLGAME=NFL,KXNBAGAME=NBA",
    )

    class _PartialFakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def list_markets(self, *, limit, status, series_ticker, cursor):
            if series_ticker == "KXNBAGAME":
                raise RuntimeError("kalshi 503")
            return {"markets": _markets_for_phi_buf(), "cursor": None}

    monkeypatch.setattr(
        ks.kalshi_client_mod, "KalshiClient",
        lambda *a, **kw: _PartialFakeClient(),
    )

    captured: list = []

    async def fake_upsert(rows):
        rows_list = list(rows)
        captured.extend(rows_list)
        return len(rows_list)

    monkeypatch.setattr(ks, "_upsert_rows", fake_upsert)

    n = asyncio.run(ks.poll_kalshi_schedules())
    # NFL row produced, NBA failure isolated.
    assert n == 1
    assert {r[0] for r in captured} == {"NFL"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_advertises_skipped_when_no_creds(monkeypatch):
    class _Settings:
        kalshi_api_key_id = None
        kalshi_private_key = None
    monkeypatch.setattr(ks, "get_settings", lambda: _Settings())
    # Reset registry state so the test is hermetic.
    from app.feeds.registry import registry as _reg
    _reg._feeds.pop("kalshi_schedules", None)
    _reg._status.pop("kalshi_schedules", None)

    ks.register_kalshi_schedules_feed()
    st = _reg.status_of("kalshi_schedules")
    assert st is not None
    assert st.enabled is False
    assert "KALSHI" in (st.skipped_reason or "")
