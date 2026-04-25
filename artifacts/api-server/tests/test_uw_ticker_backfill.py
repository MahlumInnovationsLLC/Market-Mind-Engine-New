"""Tests for the ``uw_signals.ticker`` backfill (task #425).

Two layers:

  * Pure-Python helper (:func:`derive_payload_ticker`) — exercises the
    precedence + whitespace / case rules without touching the DB.
  * SQL pass (:func:`backfill_uw_signals_ticker`) — driven against an
    in-memory fake that mimics asyncpg's ``acquire().transaction()``
    contract and applies the same canonicalisation the real Postgres
    SQL would, so we can assert on idempotency + per-pass row counts.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.feeds import uw_ticker_backfill
from app.feeds.uw_ticker_backfill import (
    BackfillResult,
    backfill_uw_signals_ticker,
    derive_payload_ticker,
)


# ---------------------------------------------------------------------------
# derive_payload_ticker — pure helper
# ---------------------------------------------------------------------------


def test_derive_returns_none_for_missing_payload():
    assert derive_payload_ticker(None) is None
    assert derive_payload_ticker({}) is None
    assert derive_payload_ticker("not a dict") is None  # type: ignore[arg-type]


def test_derive_uses_ticker_field_first():
    assert derive_payload_ticker({"ticker": "btc", "symbol": "ETH"}) == "BTC"


def test_derive_falls_back_through_precedence_chain():
    assert derive_payload_ticker({"symbol": "aapl"}) == "AAPL"
    assert derive_payload_ticker({"underlying": " spy "}) == "SPY"
    assert derive_payload_ticker({"underlying_symbol": "qqq"}) == "QQQ"


def test_derive_skips_empty_or_whitespace_only_values():
    # Empty / whitespace-only values are skipped exactly the way the
    # SQL ``nullif(btrim(...), '')`` chain skips them.
    assert derive_payload_ticker({"ticker": "", "symbol": "AAPL"}) == "AAPL"
    assert derive_payload_ticker({"ticker": "   ", "symbol": "MSFT"}) == "MSFT"
    assert derive_payload_ticker({"ticker": None, "symbol": "TSLA"}) == "TSLA"


def test_derive_uppercases_and_trims():
    assert derive_payload_ticker({"ticker": "  eth  "}) == "ETH"


def test_derive_ignores_unrelated_keys():
    assert derive_payload_ticker({"foo": "BAR"}) is None


# ---------------------------------------------------------------------------
# backfill_uw_signals_ticker — SQL pass
# ---------------------------------------------------------------------------


def _canonicalise(s: str | None) -> str | None:
    """Mirror Postgres ``upper(btrim(...))`` semantics."""
    if s is None:
        return None
    out = s.strip().upper()
    return out or None


class _FakeBackfillConn:
    """Minimal asyncpg-shaped connection that applies the same
    transformations the real SQL would.

    Only implements the two ``WITH updated AS ( UPDATE ... RETURNING 1)
    SELECT count(*) FROM updated`` queries the backfill issues, so any
    drift in the SQL surface area will fail loudly with an
    ``AssertionError`` instead of silently passing.
    """

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows

    @asynccontextmanager
    async def transaction(self):  # noqa: D401 — asyncpg signature
        yield None

    async def fetchval(self, query: str, *args):
        q = " ".join(query.split())
        if "ticker IS NULL" in q and "payload" in q:
            return self._apply_payload_fill()
        if "ticker <> upper(btrim(ticker))" in q:
            return self._apply_normalise()
        raise AssertionError(f"unexpected query: {q[:120]}")

    def _apply_payload_fill(self) -> int:
        n = 0
        for row in self.rows:
            if row["ticker"] is not None:
                continue
            derived = derive_payload_ticker(row.get("payload"))
            if derived is None:
                continue
            row["ticker"] = derived
            n += 1
        return n

    def _apply_normalise(self) -> int:
        n = 0
        for row in self.rows:
            t = row["ticker"]
            if t is None:
                continue
            canon = _canonicalise(t)
            if canon is None:
                # Whitespace-only ticker — predicate excludes it.
                continue
            if t != canon:
                row["ticker"] = canon
                n += 1
        return n


def _install_fake_db(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]):
    fake = _FakeBackfillConn(rows)

    @asynccontextmanager
    async def fake_acquire():
        yield fake

    monkeypatch.setattr(uw_ticker_backfill.db, "acquire", fake_acquire)
    return fake


def test_backfill_fills_null_tickers_from_payload(monkeypatch: pytest.MonkeyPatch):
    rows: list[dict[str, Any]] = [
        # Row 1: NULL ticker, payload has top-level "ticker".
        {"id": 1, "ticker": None, "payload": {"ticker": "btc"}},
        # Row 2: NULL ticker, payload falls back to "underlying".
        {"id": 2, "ticker": None, "payload": {"underlying": " eth "}},
        # Row 3: NULL ticker, payload has nothing usable — stays NULL.
        {"id": 3, "ticker": None, "payload": {"foo": "bar"}},
        # Row 4: already populated, but lowercase — normalised.
        {"id": 4, "ticker": "spy", "payload": {"ticker": "ignored"}},
        # Row 5: already canonical — untouched, doesn't count.
        {"id": 5, "ticker": "AAPL", "payload": {"ticker": "AAPL"}},
        # Row 6: padded with whitespace — normalised.
        {"id": 6, "ticker": "  qqq  ", "payload": {"ticker": "ignored"}},
    ]
    _install_fake_db(monkeypatch, rows)

    result = asyncio.run(backfill_uw_signals_ticker())
    assert isinstance(result, BackfillResult)
    assert result.filled_from_payload == 2  # rows 1 + 2
    assert result.normalised_existing == 2  # rows 4 + 6

    by_id = {r["id"]: r["ticker"] for r in rows}
    assert by_id == {
        1: "BTC",
        2: "ETH",
        3: None,
        4: "SPY",
        5: "AAPL",
        6: "QQQ",
    }


def test_backfill_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    rows: list[dict[str, Any]] = [
        {"id": 1, "ticker": None, "payload": {"symbol": "tsla"}},
        {"id": 2, "ticker": " msft ", "payload": {}},
    ]
    _install_fake_db(monkeypatch, rows)

    first = asyncio.run(backfill_uw_signals_ticker())
    assert first.filled_from_payload == 1
    assert first.normalised_existing == 1

    # Second run sees only canonical data and is a no-op.
    second = asyncio.run(backfill_uw_signals_ticker())
    assert second.filled_from_payload == 0
    assert second.normalised_existing == 0


def test_backfill_respects_payload_precedence(monkeypatch: pytest.MonkeyPatch):
    rows: list[dict[str, Any]] = [
        {
            "id": 1,
            "ticker": None,
            "payload": {
                "ticker": "btc",
                "symbol": "eth",
                "underlying": "sol",
                "underlying_symbol": "xrp",
            },
        },
        {
            "id": 2,
            "ticker": None,
            "payload": {"symbol": "eth", "underlying_symbol": "xrp"},
        },
        {
            "id": 3,
            "ticker": None,
            "payload": {"underlying_symbol": "xrp"},
        },
    ]
    _install_fake_db(monkeypatch, rows)

    asyncio.run(backfill_uw_signals_ticker())
    assert [r["ticker"] for r in rows] == ["BTC", "ETH", "XRP"]


def test_backfill_result_as_dict_is_json_serialisable():
    res = BackfillResult(filled_from_payload=3, normalised_existing=7)
    payload = res.as_dict()
    assert payload == {"filled_from_payload": 3, "normalised_existing": 7}
    # Must round-trip through json (the admin endpoint returns it).
    assert json.loads(json.dumps(payload)) == payload
