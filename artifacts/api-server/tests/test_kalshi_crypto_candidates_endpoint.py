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


def _install_settled_rollup_stub(
    monkeypatch, today: int = 0, yesterday: int = 0,
) -> None:
    """Install a ``db.fetchrow`` stub that answers the settled-today /
    settled-yesterday rollup query the endpoint runs alongside the
    main candidates fetch."""

    async def fake_fetchrow(sql: str, *args: Any) -> dict[str, Any]:
        assert "settled_at" in sql
        assert "league = 'CRYPTO'" in sql
        return {"today": today, "yesterday": yesterday}

    monkeypatch.setattr(db_module, "fetchrow", fake_fetchrow)


def test_handler_returns_count_and_items(monkeypatch):
    close = datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc)
    updated = datetime(2026, 4, 15, 19, 30, tzinfo=timezone.utc)
    settled = datetime(2026, 4, 15, 20, 5, tzinfo=timezone.utc)

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
            # Settled YES — should round-trip with iso timestamp,
            # outcome label, and 1.0 settle_price.
            "settled_at": settled,
            "outcome": "yes",
            "settle_price": 1.0,
        },
        {
            "kalshi_market_id": "KXETHD-26APR15-T3500",
            "kalshi_price": None,
            "game_time": None,
            "game_context": json.dumps({"symbol": "ETH"}),
            "updated_at": updated,
            # Still in flight: all three settlement fields are NULL.
            "settled_at": None,
            "outcome": None,
            "settle_price": None,
        },
    ]

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        assert "ORDER BY updated_at" in sql
        # The endpoint must ask for the settlement triplet so the
        # operator console can render realised outcomes.
        assert "settled_at" in sql
        assert "outcome" in sql
        assert "settle_price" in sql
        # Default limit is 25.
        assert args == (25,)
        return rows

    monkeypatch.setattr(db_module, "fetchval", fake_fetchval)
    monkeypatch.setattr(db_module, "fetch", fake_fetch)
    _install_settled_rollup_stub(monkeypatch, today=4, yesterday=2)

    out = _run(main_module.feeds_kalshi_crypto_candidates())
    assert out["count"] == 7
    assert out["limit"] == 25
    assert "error" not in out
    # Rollup is surfaced so the panel header can show "settled today /
    # yesterday" without a second round-trip.
    assert out["settled"] == {"today": 4, "yesterday": 2}
    items = out["items"]
    assert len(items) == 2
    assert items[0]["ticker"] == "KXBTCD-26APR15-T68000"
    assert items[0]["symbol"] == "BTC"
    assert items[0]["title"] == "BTC above 68000?"
    assert items[0]["mid_price"] == pytest.approx(0.42)
    assert items[0]["close_time"] == close.isoformat()
    assert items[0]["updated_at"] == updated.isoformat()
    assert items[0]["settled_at"] == settled.isoformat()
    assert items[0]["outcome"] == "yes"
    assert items[0]["settle_price"] == pytest.approx(1.0)
    # JSON-stringified game_context is parsed transparently.
    assert items[1]["symbol"] == "ETH"
    assert items[1]["mid_price"] is None
    assert items[1]["close_time"] is None
    # Unsettled row: all three settlement fields are nulled rather
    # than missing, so the React table can render "pending" without a
    # KeyError.
    assert items[1]["settled_at"] is None
    assert items[1]["outcome"] is None
    assert items[1]["settle_price"] is None


def test_handler_clamps_limit_into_valid_range(monkeypatch):
    captured: list[Any] = []

    async def fake_fetchval(sql: str, *args: Any) -> int:
        return 0

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        captured.append(args)
        return []

    monkeypatch.setattr(db_module, "fetchval", fake_fetchval)
    monkeypatch.setattr(db_module, "fetch", fake_fetch)
    _install_settled_rollup_stub(monkeypatch)

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
    monkeypatch.setattr(db_module, "fetchrow", boom)

    out = _run(main_module.feeds_kalshi_crypto_candidates())
    assert out["count"] == 0
    assert out["items"] == []
    assert out["limit"] == 25
    # Rollup degrades to zeroes rather than vanishing so the React
    # panel can always read ``settled.today`` / ``settled.yesterday``.
    assert out["settled"] == {"today": 0, "yesterday": 0}
    assert "predictions" in out["error"]


def test_handler_surfaces_settled_rollup_even_when_fetch_fails(monkeypatch):
    """Rollup query succeeds but the row fetch fails — the operator
    console should still see the settled-today / yesterday counts so a
    transient candidate-fetch error doesn't blank the realised-outcome
    summary."""

    async def fake_fetchval(sql: str, *args: Any) -> int:
        return 9

    async def boom_fetch(*_a, **_k):
        raise RuntimeError("temporary asyncpg error")

    monkeypatch.setattr(db_module, "fetchval", fake_fetchval)
    monkeypatch.setattr(db_module, "fetch", boom_fetch)
    _install_settled_rollup_stub(monkeypatch, today=3, yesterday=5)

    out = _run(main_module.feeds_kalshi_crypto_candidates())
    assert out["count"] == 9
    assert out["items"] == []
    assert out["settled"] == {"today": 3, "yesterday": 5}
    assert "asyncpg" in out["error"]


def test_handler_tolerates_missing_settled_rollup_query(monkeypatch):
    """A fork without the migration may not yet expose ``settled_at`` —
    the rollup query raises but the rest of the response must still
    render so the panel doesn't lose its candidate list."""

    close = datetime(2026, 4, 15, 20, 0, tzinfo=timezone.utc)

    async def fake_fetchval(sql: str, *args: Any) -> int:
        return 1

    async def fake_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        return [
            {
                "kalshi_market_id": "KXBTCD-26APR15-T68000",
                "kalshi_price": 0.5,
                "game_time": close,
                "game_context": {"symbol": "BTC"},
                "updated_at": close,
                "settled_at": None,
                "outcome": None,
                "settle_price": None,
            }
        ]

    async def boom_fetchrow(*_a, **_k):
        raise RuntimeError(
            'column "settled_at" of relation "predictions" does not exist'
        )

    monkeypatch.setattr(db_module, "fetchval", fake_fetchval)
    monkeypatch.setattr(db_module, "fetch", fake_fetch)
    monkeypatch.setattr(db_module, "fetchrow", boom_fetchrow)

    out = _run(main_module.feeds_kalshi_crypto_candidates())
    assert out["count"] == 1
    assert len(out["items"]) == 1
    # Rollup degrades silently to zeroes; the candidate row still renders.
    assert out["settled"] == {"today": 0, "yesterday": 0}
