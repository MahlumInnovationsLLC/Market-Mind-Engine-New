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


def test_mid_price_uses_dollars_fields_directly_without_dividing_by_100():
    # The live Kalshi GET /markets response now ships YES book under the
    # ``*_dollars`` suffixed field names, already on the 0..1 dollar
    # scale.  Don't divide by 100 — these are the exact final price.
    assert kcm._mid_price_dollars(
        {"yes_bid_dollars": 0.40, "yes_ask_dollars": 0.44}
    ) == pytest.approx(0.42)
    assert kcm._mid_price_dollars(
        {"last_price_dollars": 0.55}
    ) == pytest.approx(0.55)
    assert kcm._mid_price_dollars(
        {"yes_ask_dollars": 0.60}
    ) == pytest.approx(0.60)
    assert kcm._mid_price_dollars(
        {"yes_bid_dollars": 0.30}
    ) == pytest.approx(0.30)


def test_mid_price_prefers_dollars_field_when_both_keys_present():
    # If a fixture happens to carry both names (e.g. transitional
    # API), the new ``*_dollars`` field wins so the live path stays
    # correct even if some intermediary added a legacy alias.
    assert kcm._mid_price_dollars(
        {"yes_bid_dollars": 0.40, "yes_ask_dollars": 0.44,
         "yes_bid": 99, "yes_ask": 99}
    ) == pytest.approx(0.42)


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
    ctx = cand["metadata"]
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
            "metadata": {"symbol": "BTC"},
        },
        {
            "game_id": "KXETHD-1",
            "league": "CRYPTO",
            "kalshi_market_id": "KXETHD-1",
            "kalshi_price": 0.55,
            "game_time": None,
            "metadata": {"symbol": "ETH"},
        },
    ]
    n = asyncio.run(kcm._upsert_candidates(rows))
    assert n == 2
    assert len(calls) == 2
    sql, args = calls[0]
    assert "INSERT INTO predictions" in sql
    assert "ON CONFLICT (kalshi_market_id)" in sql
    # metadata arrives as a JSON-encoded string ($6::jsonb in SQL).
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
         "game_time": None, "metadata": {}},
        {"game_id": "KXBTCD-ok", "league": "CRYPTO",
         "kalshi_market_id": "KXBTCD-ok", "kalshi_price": 0.5,
         "game_time": None, "metadata": {}},
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


# ---------------------------------------------------------------------------
# Settler tests (task #424)
# ---------------------------------------------------------------------------


def test_settled_outcome_returns_none_until_resolved():
    # No status / result yet — open market.
    assert kcm._settled_outcome({"status": "open"}) is None
    # Status looks settled but no result attached → still pre-stamp.
    assert kcm._settled_outcome({"status": "settled"}) is None
    # Result present but status nowhere near settled → don't stamp.
    assert kcm._settled_outcome({"status": "open", "result": "yes"}) is None


def test_settled_outcome_handles_yes_no_void_and_alt_keys():
    assert kcm._settled_outcome({"status": "settled", "result": "yes"}) == "yes"
    assert kcm._settled_outcome({"status": "finalized", "result": "NO"}) == "no"
    assert kcm._settled_outcome({"status": "determined", "result": "void"}) == "void"
    # Older payload uses ``settlement`` instead of ``result``.
    assert kcm._settled_outcome(
        {"status": "settled", "settlement": "yes"}
    ) == "yes"
    # ``closed`` + populated result is the brief resolution-sweep window.
    assert kcm._settled_outcome({"status": "closed", "result": "no"}) == "no"
    # Garbage outcome value rejected.
    assert kcm._settled_outcome({"status": "settled", "result": "maybe"}) is None


def test_settle_price_dollars():
    assert kcm._settle_price_dollars("yes") == 1.0
    assert kcm._settle_price_dollars("no") == 0.0
    assert kcm._settle_price_dollars("void") is None


def test_settled_at_for_prefers_explicit_timestamp_then_falls_back():
    # Explicit settled_time wins.
    assert kcm._settled_at_for(
        {"settled_time": "2026-04-15T20:00:00Z",
         "close_time": "2026-04-15T19:00:00Z"}
    ) == datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc)
    # Falls back to close_time when no settlement timestamp is published.
    assert kcm._settled_at_for(
        {"close_time": "2026-04-15T19:00:00Z"}
    ) == datetime(2026, 4, 15, 19, 0, tzinfo=timezone.utc)
    # Last-resort fallback: now() — just check it's UTC-aware.
    fallback = kcm._settled_at_for({})
    assert fallback.tzinfo is not None


def test_record_settlement_returns_true_on_update(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_execute(sql: str, *args: Any) -> str:
        captured["sql"] = sql
        captured["args"] = args
        return "UPDATE 1"

    monkeypatch.setattr(kcm.db, "execute", fake_execute)
    ok = asyncio.run(kcm._record_settlement(
        "KXBTCD-X", "yes",
        datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc),
        1.0,
    ))
    assert ok is True
    assert "UPDATE predictions" in captured["sql"]
    assert "settled_at IS NULL" in captured["sql"]  # idempotency guard
    assert captured["args"] == (
        "KXBTCD-X",
        datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc),
        "yes",
        1.0,
    )


def test_record_settlement_handles_zero_rowcount(monkeypatch):
    async def fake_execute(sql: str, *args: Any) -> str:
        return "UPDATE 0"

    monkeypatch.setattr(kcm.db, "execute", fake_execute)
    ok = asyncio.run(kcm._record_settlement(
        "KXBTCD-X", "yes",
        datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc),
        1.0,
    ))
    assert ok is False


def test_record_settlement_swallows_db_errors(monkeypatch):
    async def fake_execute(sql: str, *args: Any) -> str:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(kcm.db, "execute", fake_execute)
    ok = asyncio.run(kcm._record_settlement(
        "KXBTCD-X", "no",
        datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc),
        0.0,
    ))
    assert ok is False


def test_settle_poll_returns_zero_without_creds(monkeypatch):
    class FakeSettings:
        kalshi_api_key_id = ""
        kalshi_private_key = ""

    monkeypatch.setattr(kcm, "get_settings", lambda: FakeSettings())
    assert asyncio.run(kcm.poll_kalshi_crypto_settlements()) == 0


def test_settle_poll_stamps_only_resolved_markets(monkeypatch):
    class FakeSettings:
        kalshi_api_key_id = "kid"
        kalshi_private_key = "pk"

    monkeypatch.setattr(kcm, "get_settings", lambda: FakeSettings())

    candidates = [
        {"id": 1, "kalshi_market_id": "KXBTCD-DONE-YES", "game_time": None},
        {"id": 2, "kalshi_market_id": "KXBTCD-OPEN", "game_time": None},
        {"id": 3, "kalshi_market_id": "KXBTCD-VOID", "game_time": None},
        {"id": 4, "kalshi_market_id": "KXBTCD-NETWORK-ERR", "game_time": None},
    ]

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM predictions" in sql
        assert args[0] == "CRYPTO"
        return candidates

    monkeypatch.setattr(kcm.db, "fetch", fake_fetch)

    market_responses: dict[str, dict[str, Any]] = {
        "KXBTCD-DONE-YES": {
            "market": {
                "ticker": "KXBTCD-DONE-YES", "status": "settled",
                "result": "yes",
                "settled_time": "2026-04-15T20:00:00Z",
            },
        },
        "KXBTCD-OPEN": {
            "market": {
                "ticker": "KXBTCD-OPEN", "status": "open",
            },
        },
        "KXBTCD-VOID": {
            "market": {
                "ticker": "KXBTCD-VOID", "status": "finalized",
                "result": "void",
            },
        },
    }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_market(self, ticker: str) -> dict[str, Any]:
            if ticker == "KXBTCD-NETWORK-ERR":
                raise RuntimeError("boom")
            return market_responses[ticker]

    monkeypatch.setattr(kcm.kalshi_client_mod, "KalshiClient", FakeClient)

    updates: list[tuple[str, str, float | None]] = []

    async def fake_execute(sql: str, *args: Any) -> str:
        # _record_settlement uses execute(); mark every UPDATE as 1 row
        # touched so the settler increments its return counter.
        updates.append((args[0], args[2], args[3]))
        return "UPDATE 1"

    monkeypatch.setattr(kcm.db, "execute", fake_execute)

    n = asyncio.run(kcm.poll_kalshi_crypto_settlements())
    # Two settled markets (yes + void); the open one is skipped, the
    # network error short-circuits without raising.
    assert n == 2
    seen = sorted((t, o, p) for (t, o, p) in updates)
    assert seen == [
        ("KXBTCD-DONE-YES", "yes", 1.0),
        ("KXBTCD-VOID", "void", None),
    ]


def test_register_settlements_marks_disabled_when_creds_missing(monkeypatch):
    from app.feeds.registry import registry as _registry

    _registry._feeds.pop("kalshi_crypto_settlements", None)  # type: ignore[attr-defined]
    _registry._status.pop("kalshi_crypto_settlements", None)  # type: ignore[attr-defined]

    class FakeSettings:
        kalshi_api_key_id = ""
        kalshi_private_key = ""

    monkeypatch.setattr(kcm, "get_settings", lambda: FakeSettings())
    kcm.register_kalshi_crypto_settlements_feed()
    status = _registry.status_of("kalshi_crypto_settlements")
    assert status is not None
    d = status.to_dict()
    assert d["enabled"] is False
    assert "KALSHI_API_KEY_ID" in (d.get("skipped_reason") or "")


def test_register_settlements_enabled_when_creds_present(monkeypatch):
    from app.feeds.registry import registry as _registry

    _registry._feeds.pop("kalshi_crypto_settlements", None)  # type: ignore[attr-defined]
    _registry._status.pop("kalshi_crypto_settlements", None)  # type: ignore[attr-defined]

    class FakeSettings:
        kalshi_api_key_id = "kid"
        kalshi_private_key = "pk"

    monkeypatch.setattr(kcm, "get_settings", lambda: FakeSettings())
    kcm.register_kalshi_crypto_settlements_feed()
    status = _registry.status_of("kalshi_crypto_settlements")
    assert status is not None
    d = status.to_dict()
    assert d["enabled"] is True
    # Sanity-check the cadence honours the settler default (much slower
    # than the discovery feed).
    assert d["cadence_seconds"] >= 30.0


def test_settle_cadence_and_batch_env_overrides(monkeypatch):
    monkeypatch.setenv("AXIOM_KALSHI_CRYPTO_SETTLEMENTS_CADENCE_SECS", "120")
    monkeypatch.setenv("AXIOM_KALSHI_CRYPTO_SETTLEMENTS_BATCH", "10")
    assert kcm._settle_cadence_seconds() == 120.0
    assert kcm._settle_batch_size() == 10
    # Floor at 30s so an over-eager override can't melt the API.
    monkeypatch.setenv("AXIOM_KALSHI_CRYPTO_SETTLEMENTS_CADENCE_SECS", "1")
    assert kcm._settle_cadence_seconds() == 30.0
    # Garbage falls back to the default.
    monkeypatch.setenv("AXIOM_KALSHI_CRYPTO_SETTLEMENTS_BATCH", "junk")
    assert kcm._settle_batch_size() == kcm._SETTLE_DEFAULT_BATCH
