"""Tests for the Kalshi weather (temperature + precipitation) market
discovery feed."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.feeds import kalshi_weather_markets as kwm


def test_parse_close_time_handles_z_and_offset():
    a = kwm._parse_close_time("2026-06-01T20:00:00Z")
    b = kwm._parse_close_time("2026-06-01T20:00:00+00:00")
    assert a == b == datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)


def test_parse_close_time_returns_none_on_garbage():
    assert kwm._parse_close_time(None) is None
    assert kwm._parse_close_time("") is None
    assert kwm._parse_close_time("not-a-date") is None


def test_mid_price_uses_bid_ask_midpoint_in_dollars():
    assert kwm._mid_price_dollars({"yes_bid": 40, "yes_ask": 44}) == pytest.approx(0.42)


def test_mid_price_falls_back_to_last_then_one_sided():
    assert kwm._mid_price_dollars({"last_price": 55}) == pytest.approx(0.55)
    assert kwm._mid_price_dollars({"yes_ask": 60}) == pytest.approx(0.60)
    assert kwm._mid_price_dollars({"yes_bid": 30}) == pytest.approx(0.30)
    assert kwm._mid_price_dollars({}) is None


def test_mid_price_uses_dollars_fields_directly_without_dividing_by_100():
    assert kwm._mid_price_dollars(
        {"yes_bid_dollars": 0.40, "yes_ask_dollars": 0.44}
    ) == pytest.approx(0.42)
    assert kwm._mid_price_dollars({"last_price_dollars": 0.55}) == pytest.approx(0.55)


def test_series_prefixes_cover_temp_metrics_and_precip_per_city():
    series = set(kwm._series_prefixes())
    # Temperature metrics fan across every city code (incl. the NY alias).
    assert "KXHIGHNY" in series
    assert "KXLOWNYC" in series
    assert "KXTEMPAUS" in series
    # Precipitation prefix fans across the same cities.
    assert "KXRAINNY" in series
    assert "KXRAINMIA" in series
    # Exactly (temp_metrics * cities) + (1 precip * cities).
    n_cities = len(kwm._CITY_CODES)
    assert len(kwm._series_prefixes()) == (len(kwm._TEMP_PREFIXES) + 1) * n_cities


def test_candidate_from_market_skips_unparseable_ticker():
    assert kwm._candidate_from_market({"ticker": "FOOBAR"}) is None
    assert kwm._candidate_from_market({}) is None
    # A crypto ticker is neither a temperature nor a precip ticker.
    assert kwm._candidate_from_market({"ticker": "KXBTCD-26APR15-T68000"}) is None


def test_candidate_from_market_builds_temperature_payload():
    market = {
        "ticker": "KXHIGHNY-26JUN01-T80",
        "status": "open",
        "yes_bid": 40,
        "yes_ask": 44,
        "last_price": 42,
        "close_time": "2026-06-01T20:00:00Z",
        "title": "NYC high above 80F?",
    }
    cand = kwm._candidate_from_market(market)
    assert cand is not None
    assert cand["league"] == "WEATHER"
    assert cand["game_id"] == "KXHIGHNY-26JUN01-T80"
    assert cand["kalshi_market_id"] == "KXHIGHNY-26JUN01-T80"
    assert cand["kalshi_price"] == pytest.approx(0.42)
    assert cand["game_time"] == datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
    ctx = cand["metadata"]
    assert ctx["source"] == "kalshi_weather_markets"
    assert ctx["city"] == "NYC"           # NY alias canonicalised
    assert ctx["metric"] == "HIGH"
    assert ctx["threshold"] == 80.0
    assert ctx["title"] == "NYC high above 80F?"


def test_candidate_from_market_builds_precipitation_payload():
    market = {
        "ticker": "KXRAINNY-26JUN01-T0.5",
        "status": "open",
        "yes_bid_dollars": 0.30,
        "yes_ask_dollars": 0.34,
        "close_time": "2026-06-01T20:00:00Z",
        "title": "NYC rain above 0.5in?",
    }
    cand = kwm._candidate_from_market(market)
    assert cand is not None
    assert cand["league"] == "WEATHER"
    assert cand["kalshi_market_id"] == "KXRAINNY-26JUN01-T0.5"
    assert cand["kalshi_price"] == pytest.approx(0.32)
    ctx = cand["metadata"]
    assert ctx["city"] == "NYC"
    assert ctx["metric"] == "RAIN"
    assert ctx["threshold"] == 0.5


def test_upsert_candidates_executes_one_statement_per_row(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    async def fake_execute(sql: str, *args: Any) -> None:
        calls.append((sql, args))

    monkeypatch.setattr(kwm.db, "execute", fake_execute)

    rows = [
        {
            "game_id": "KXHIGHNY-1",
            "league": "WEATHER",
            "kalshi_market_id": "KXHIGHNY-1",
            "kalshi_price": 0.42,
            "game_time": datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
            "metadata": {"city": "NYC"},
        },
        {
            "game_id": "KXRAINNY-1",
            "league": "WEATHER",
            "kalshi_market_id": "KXRAINNY-1",
            "kalshi_price": 0.55,
            "game_time": None,
            "metadata": {"city": "NYC"},
        },
    ]
    n = asyncio.run(kwm._upsert_candidates(rows))
    assert n == 2
    assert len(calls) == 2
    sql, args = calls[0]
    assert "INSERT INTO predictions" in sql
    assert "ON CONFLICT (kalshi_market_id)" in sql
    # metadata arrives as a JSON-encoded string ($6::jsonb in SQL).
    assert json.loads(args[5]) == {"city": "NYC"}


def test_upsert_normalizes_tz_aware_game_time_to_naive_utc(monkeypatch):
    bound: list[Any] = []

    async def fake_execute(sql: str, *args: Any) -> None:
        bound.append(args[4])  # game_time

    monkeypatch.setattr(kwm.db, "execute", fake_execute)
    rows = [{
        "game_id": "KXHIGHNY-tz",
        "league": "WEATHER",
        "kalshi_market_id": "KXHIGHNY-tz",
        "kalshi_price": 0.4,
        "game_time": datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
        "metadata": {},
    }]
    asyncio.run(kwm._upsert_candidates(rows))
    assert bound and bound[0] == datetime(2026, 6, 1, 20, 0)
    assert bound[0].tzinfo is None  # predictions.game_time is tz-naive


def test_upsert_continues_when_one_row_fails(monkeypatch):
    seen: list[str] = []

    async def fake_execute(sql: str, *args: Any) -> None:
        seen.append(args[2])  # kalshi_market_id
        if args[2] == "KXHIGHNY-bad":
            raise RuntimeError("dup violation")

    monkeypatch.setattr(kwm.db, "execute", fake_execute)
    rows = [
        {"game_id": "KXHIGHNY-bad", "league": "WEATHER",
         "kalshi_market_id": "KXHIGHNY-bad", "kalshi_price": 0.4,
         "game_time": None, "metadata": {}},
        {"game_id": "KXHIGHNY-ok", "league": "WEATHER",
         "kalshi_market_id": "KXHIGHNY-ok", "kalshi_price": 0.5,
         "game_time": None, "metadata": {}},
    ]
    n = asyncio.run(kwm._upsert_candidates(rows))
    assert n == 1
    assert seen == ["KXHIGHNY-bad", "KXHIGHNY-ok"]


def test_poll_returns_zero_without_kalshi_creds(monkeypatch):
    class FakeSettings:
        kalshi_api_key_id = ""
        kalshi_private_key = ""

    monkeypatch.setattr(kwm, "get_settings", lambda: FakeSettings())
    assert asyncio.run(kwm.poll_kalshi_weather_markets()) == 0


def test_poll_paginates_filters_and_dedupes(monkeypatch):
    class FakeSettings:
        kalshi_api_key_id = "kid"
        kalshi_private_key = "pk"

    monkeypatch.setattr(kwm, "get_settings", lambda: FakeSettings())

    pages_by_series: dict[str, list[dict[str, Any]]] = {
        "KXHIGHNY": [
            {
                "markets": [
                    {
                        "ticker": "KXHIGHNY-26JUN01-T80",
                        "status": "open",
                        "yes_bid": 40, "yes_ask": 44,
                        "close_time": "2026-06-01T20:00:00Z",
                        "title": "NYC high above 80F?",
                    },
                    {"ticker": "GARBAGE-NOT-WEATHER"},  # filtered out
                ],
                "cursor": "page2",
            },
            {
                "markets": [
                    {
                        "ticker": "KXHIGHNY-26JUN02-T82",
                        "status": "open",
                        "yes_bid": 30, "yes_ask": 32,
                        "close_time": "2026-06-02T20:00:00Z",
                        "title": "NYC high above 82F?",
                    },
                    # Duplicate ticker within the same poll → de-duped.
                    {
                        "ticker": "KXHIGHNY-26JUN01-T80",
                        "status": "open",
                        "yes_bid": 41, "yes_ask": 43,
                        "close_time": "2026-06-01T20:00:00Z",
                    },
                ],
                "cursor": None,
            },
        ],
        "KXRAINNY": [
            {
                "markets": [
                    {
                        "ticker": "KXRAINNY-26JUN01-T0.5",
                        "status": "open",
                        "yes_bid": 50, "yes_ask": 52,
                        "close_time": "2026-06-01T20:00:00Z",
                        "title": "NYC rain above 0.5in?",
                    },
                ],
                "cursor": None,
            },
        ],
    }

    class FakeClient:
        def __init__(self, *a, **k):
            self._cursors: dict[str, int] = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def list_markets(self, *, limit, status, series_ticker, cursor=None):
            assert status == "open"
            assert limit == kwm._PAGE_LIMIT
            idx = self._cursors.get(series_ticker, 0)
            pages = pages_by_series.get(series_ticker, [])
            if idx >= len(pages):
                return {"markets": [], "cursor": None}
            page = pages[idx]
            self._cursors[series_ticker] = idx + 1
            return page

    monkeypatch.setattr(kwm.kalshi_client_mod, "KalshiClient", FakeClient)

    upserts: list[str] = []

    async def fake_execute(sql: str, *args: Any) -> None:
        upserts.append(args[2])  # kalshi_market_id

    monkeypatch.setattr(kwm.db, "execute", fake_execute)

    n = asyncio.run(kwm.poll_kalshi_weather_markets())
    assert n == 3
    assert sorted(upserts) == [
        "KXHIGHNY-26JUN01-T80",
        "KXHIGHNY-26JUN02-T82",
        "KXRAINNY-26JUN01-T0.5",
    ]


def test_register_marks_disabled_when_creds_missing(monkeypatch):
    from app.feeds.registry import registry as _registry

    _registry._feeds.pop("kalshi_weather_markets", None)  # type: ignore[attr-defined]
    _registry._status.pop("kalshi_weather_markets", None)  # type: ignore[attr-defined]

    class FakeSettings:
        kalshi_api_key_id = ""
        kalshi_private_key = ""

    monkeypatch.setattr(kwm, "get_settings", lambda: FakeSettings())
    kwm.register_kalshi_weather_markets_feed()
    status = _registry.status_of("kalshi_weather_markets")
    assert status is not None
    d = status.to_dict()
    assert d["enabled"] is False
    assert "KALSHI_API_KEY_ID" in (d.get("skipped_reason") or "")


def test_register_enabled_when_creds_present(monkeypatch):
    from app.feeds.registry import registry as _registry

    _registry._feeds.pop("kalshi_weather_markets", None)  # type: ignore[attr-defined]
    _registry._status.pop("kalshi_weather_markets", None)  # type: ignore[attr-defined]

    class FakeSettings:
        kalshi_api_key_id = "kid"
        kalshi_private_key = "pk"

    monkeypatch.setattr(kwm, "get_settings", lambda: FakeSettings())
    kwm.register_kalshi_weather_markets_feed()
    status = _registry.status_of("kalshi_weather_markets")
    assert status is not None
    d = status.to_dict()
    assert d["enabled"] is True
    assert d["cadence_seconds"] >= 30.0


def test_cadence_env_override_and_floor(monkeypatch):
    monkeypatch.setenv("AXIOM_KALSHI_WEATHER_MARKETS_CADENCE_SECS", "300")
    assert kwm._cadence_seconds() == 300.0
    # Floor at 30s so an over-eager override can't melt the per-series fan-out.
    monkeypatch.setenv("AXIOM_KALSHI_WEATHER_MARKETS_CADENCE_SECS", "1")
    assert kwm._cadence_seconds() == 30.0
    # Garbage falls back to the default cadence.
    monkeypatch.setenv("AXIOM_KALSHI_WEATHER_MARKETS_CADENCE_SECS", "junk")
    assert kwm._cadence_seconds() == kwm._DEFAULT_CADENCE_SECS
