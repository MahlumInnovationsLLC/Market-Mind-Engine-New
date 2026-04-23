"""Tests for the Kalshi BTC/ETH market discovery feed."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.feeds import kalshi_crypto_markets as kcm


def test_parse_close_time_handles_z_and_offset():
    a = kcm._parse_close_time("2026-04-15T20:00:00Z")
    b = kcm._parse_close_time("2026-04-15T20:00:00+00:00")
    assert a == b == datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc)


def test_parse_close_time_returns_none_on_garbage():
    assert kcm._parse_close_time(None) is None
    assert kcm._parse_close_time("") is None
    assert kcm._parse_close_time("not-a-date") is None


def test_mid_price_uses_bid_ask_midpoint_in_dollars():
    assert kcm._mid_price_dollars({"yes_bid": 40, "yes_ask": 44}) == pytest.approx(0.42)


def test_mid_price_falls_back_to_last_then_one_sided():
    assert kcm._mid_price_dollars({"last_price": 55}) == pytest.approx(0.55)
    assert kcm._mid_price_dollars({"yes_ask": 60}) == pytest.approx(0.60)
    assert kcm._mid_price_dollars({"yes_bid": 30}) == pytest.approx(0.30)
    assert kcm._mid_price_dollars({}) is None


def test_candidate_from_market_skips_unparseable_ticker():
    assert kcm._candidate_from_market({"ticker": "FOOBAR"}) is None
    assert kcm._candidate_from_market({}) is None


def test_candidate_from_market_builds_full_payload():
    market = {
        "ticker": "KXBTCD-26APR15-T68000",
        "status": "open",
        "yes_bid": 40,
        "yes_ask": 44,
        "last_price": 42,
        "close_time": "2026-04-15T20:00:00Z",
        "title": "BTC closes above 68000?",
    }
    cand = kcm._candidate_from_market(market)
    assert cand is not None
    assert cand["league"] == "CRYPTO"
    assert cand["game_id"] == "KXBTCD-26APR15-T68000"
    assert cand["kalshi_market_id"] == "KXBTCD-26APR15-T68000"
    assert cand["kalshi_price"] == pytest.approx(0.42)
    assert cand["game_time"] == datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc)
    ctx = cand["game_context"]
    assert ctx["symbol"] == "BTC"
    assert ctx["source"] == "kalshi_crypto_markets"
    assert ctx["title"] == "BTC closes above 68000?"


def test_upsert_candidates_executes_one_statement_per_row(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    async def fake_execute(sql: str, *args: Any) -> None:
        calls.append((sql, args))

    monkeypatch.setattr(kcm.db, "execute", fake_execute)

    rows = [
        {
            "game_id": "KXBTCD-1",
            "league": "CRYPTO",
            "kalshi_market_id": "KXBTCD-1",
            "kalshi_price": 0.42,
            "game_time": datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc),
            "game_context": {"symbol": "BTC"},
        },
        {
            "game_id": "KXETHD-1",
            "league": "CRYPTO",
            "kalshi_market_id": "KXETHD-1",
            "kalshi_price": 0.55,
            "game_time": None,
            "game_context": {"symbol": "ETH"},
        },
    ]
    n = asyncio.run(kcm._upsert_candidates(rows))
    assert n == 2
    assert len(calls) == 2
    sql, args = calls[0]
    assert "INSERT INTO predictions" in sql
    assert "ON CONFLICT (kalshi_market_id)" in sql
    # game_context arrives as a JSON-encoded string ($6::jsonb in SQL).
    assert json.loads(args[5]) == {"symbol": "BTC"}


def test_upsert_continues_when_one_row_fails(monkeypatch):
    seen: list[str] = []

    async def fake_execute(sql: str, *args: Any) -> None:
        seen.append(args[2])  # kalshi_market_id
        if args[2] == "KXBTCD-bad":
            raise RuntimeError("dup violation")

    monkeypatch.setattr(kcm.db, "execute", fake_execute)
    rows = [
        {"game_id": "KXBTCD-bad", "league": "CRYPTO",
         "kalshi_market_id": "KXBTCD-bad", "kalshi_price": 0.4,
         "game_time": None, "game_context": {}},
        {"game_id": "KXBTCD-ok", "league": "CRYPTO",
         "kalshi_market_id": "KXBTCD-ok", "kalshi_price": 0.5,
         "game_time": None, "game_context": {}},
    ]
    n = asyncio.run(kcm._upsert_candidates(rows))
    assert n == 1
    assert seen == ["KXBTCD-bad", "KXBTCD-ok"]


def test_poll_returns_zero_without_kalshi_creds(monkeypatch):
    class FakeSettings:
        kalshi_api_key_id = ""
        kalshi_private_key = ""

    monkeypatch.setattr(kcm, "get_settings", lambda: FakeSettings())
    assert asyncio.run(kcm.poll_kalshi_crypto_markets()) == 0


def test_poll_paginates_and_upserts(monkeypatch):
    class FakeSettings:
        kalshi_api_key_id = "kid"
        kalshi_private_key = "pk"

    monkeypatch.setattr(kcm, "get_settings", lambda: FakeSettings())

    pages_by_series: dict[str, list[dict[str, Any]]] = {
        "KXBTC": [
            {
                "markets": [
                    {
                        "ticker": "KXBTCD-26APR15-T68000",
                        "status": "open",
                        "yes_bid": 40, "yes_ask": 44,
                        "close_time": "2026-04-15T20:00:00Z",
                        "title": "BTC above 68000?",
                    },
                    {"ticker": "GARBAGE-NOT-CRYPTO"},  # filtered out
                ],
                "cursor": "page2",
            },
            {
                "markets": [
                    {
                        "ticker": "KXBTCD-26APR16-T70000",
                        "status": "open",
                        "yes_bid": 30, "yes_ask": 32,
                        "close_time": "2026-04-16T20:00:00Z",
                        "title": "BTC above 70000?",
                    },
                ],
                "cursor": None,
            },
        ],
        "KXETH": [
            {
                "markets": [
                    {
                        "ticker": "KXETHD-26APR15-T3500",
                        "status": "open",
                        "yes_bid": 50, "yes_ask": 52,
                        "close_time": "2026-04-15T20:00:00Z",
                        "title": "ETH above 3500?",
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
            assert limit == kcm._PAGE_LIMIT
            idx = self._cursors.get(series_ticker, 0)
            pages = pages_by_series.get(series_ticker, [])
            if idx >= len(pages):
                return {"markets": [], "cursor": None}
            page = pages[idx]
            self._cursors[series_ticker] = idx + 1
            return page

    monkeypatch.setattr(kcm.kalshi_client_mod, "KalshiClient", FakeClient)

    upserts: list[str] = []

    async def fake_execute(sql: str, *args: Any) -> None:
        upserts.append(args[2])  # kalshi_market_id

    monkeypatch.setattr(kcm.db, "execute", fake_execute)

    n = asyncio.run(kcm.poll_kalshi_crypto_markets())
    assert n == 3
    assert sorted(upserts) == [
        "KXBTCD-26APR15-T68000",
        "KXBTCD-26APR16-T70000",
        "KXETHD-26APR15-T3500",
    ]


def test_register_marks_disabled_when_creds_missing(monkeypatch):
    from app.feeds.registry import registry as _registry

    # Wipe any prior registration so the test is independent of order.
    _registry._feeds.pop("kalshi_crypto_markets", None)  # type: ignore[attr-defined]
    _registry._status.pop("kalshi_crypto_markets", None)  # type: ignore[attr-defined]

    class FakeSettings:
        kalshi_api_key_id = ""
        kalshi_private_key = ""

    monkeypatch.setattr(kcm, "get_settings", lambda: FakeSettings())
    kcm.register_kalshi_crypto_markets_feed()
    status = _registry.status_of("kalshi_crypto_markets")
    assert status is not None
    d = status.to_dict()
    assert d["enabled"] is False
    assert "KALSHI_API_KEY_ID" in (d.get("skipped_reason") or "")


def test_register_enabled_when_creds_present(monkeypatch):
    from app.feeds.registry import registry as _registry

    _registry._feeds.pop("kalshi_crypto_markets", None)  # type: ignore[attr-defined]
    _registry._status.pop("kalshi_crypto_markets", None)  # type: ignore[attr-defined]

    class FakeSettings:
        kalshi_api_key_id = "kid"
        kalshi_private_key = "pk"

    monkeypatch.setattr(kcm, "get_settings", lambda: FakeSettings())
    kcm.register_kalshi_crypto_markets_feed()
    status = _registry.status_of("kalshi_crypto_markets")
    assert status is not None
    assert status.to_dict()["enabled"] is True
