"""Tests for the GET /api/feeds/kalshi-crypto-candidates endpoint.

The endpoint surfaces the latest crypto-binary candidates the
``kalshi_crypto_markets`` feed has upserted into ``predictions`` so the
operator console can verify discovery is feeding the cycle before it
fires.

These tests call the route handler directly with stubbed
``db.fetchval`` / ``db.fetch`` so we don't need a live Postgres or the
Clerk auth stack: the auth layer is exercised by the auth-gate
inventory tests already.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app import db as db_module
from app import main as main_module


def _run(coro):
    return asyncio.run(coro)


def test_handler_returns_count_and_items(monkeypatch):
    close = datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc)
    updated = datetime(2026, 4, 15, 19, 30, tzinfo=timezone.utc)

    async def fake_fetchval(sql: str, *args: Any) -> int:
        assert "FROM predictions" in sql
        assert "league = 'CRYPTO'" in sql
        return 7

    rows = [
        {
            "kalshi_market_id": "KXBTCD-26APR15-T68000",
            "kalshi_price": 0.42,
            "game_time": close,
            "game_context": {"symbol": "BTC", "title": "BTC above 68000?"},
            "updated_at": updated,
        },
        {
            "kalshi_market_id": "KXETHD-26APR15-T3500",
            "kalshi_price": None,
            "game_time": None,
            "game_context": json.dumps({"symbol": "ETH"}),
            "updated_at": updated,
        },
    ]

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        assert "ORDER BY updated_at" in sql
        # Default limit is 25.
        assert args == (25,)
        return rows

    monkeypatch.setattr(db_module, "fetchval", fake_fetchval)
    monkeypatch.setattr(db_module, "fetch", fake_fetch)

    out = _run(main_module.feeds_kalshi_crypto_candidates())
    assert out["count"] == 7
    assert out["limit"] == 25
    assert "error" not in out
    items = out["items"]
    assert len(items) == 2
    assert items[0]["ticker"] == "KXBTCD-26APR15-T68000"
    assert items[0]["symbol"] == "BTC"
    assert items[0]["title"] == "BTC above 68000?"
    assert items[0]["mid_price"] == pytest.approx(0.42)
    assert items[0]["close_time"] == close.isoformat()
    assert items[0]["updated_at"] == updated.isoformat()
    # JSON-stringified game_context is parsed transparently.
    assert items[1]["symbol"] == "ETH"
    assert items[1]["mid_price"] is None
    assert items[1]["close_time"] is None


def test_handler_clamps_limit_into_valid_range(monkeypatch):
    captured: list[Any] = []

    async def fake_fetchval(sql: str, *args: Any) -> int:
        return 0

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        captured.append(args)
        return []

    monkeypatch.setattr(db_module, "fetchval", fake_fetchval)
    monkeypatch.setattr(db_module, "fetch", fake_fetch)

    # Above the cap clamps to 100; below 1 clamps up to 1.
    out_hi = _run(main_module.feeds_kalshi_crypto_candidates(limit=10_000))
    out_lo = _run(main_module.feeds_kalshi_crypto_candidates(limit=0))

    assert out_hi["limit"] == 100
    assert out_lo["limit"] == 1
    assert captured[0] == (100,)
    assert captured[1] == (1,)


def test_handler_soft_fails_when_predictions_table_missing(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError('relation "predictions" does not exist')

    monkeypatch.setattr(db_module, "fetchval", boom)
    monkeypatch.setattr(db_module, "fetch", boom)

    out = _run(main_module.feeds_kalshi_crypto_candidates())
    assert out["count"] == 0
    assert out["items"] == []
    assert out["limit"] == 25
    assert "predictions" in out["error"]
