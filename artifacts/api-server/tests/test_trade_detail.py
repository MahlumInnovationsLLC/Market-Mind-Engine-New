"""Unit tests for `dashboard.get_trade_detail`.

Covers:
* numeric ``id`` lookup vs UUID-ish ``trade_id`` lookup
* missing-row → ``None``
* empty / blank ref short-circuit
* time-series assembly from ``swarm_debates.game_context`` rows
* anchor-point fallback when no debate rows exist for the market
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import dashboard


class _FakeDB:
    """Routes asyncpg-style accessors based on query substring."""

    def __init__(
        self,
        *,
        trade_row: dict[str, Any] | None = None,
        match_on: str = "id",  # "id" or "trade_id"
        match_value: Any = None,
        series_rows: list[dict[str, Any]] | None = None,
        raise_lookup: bool = False,
    ) -> None:
        self.trade_row = trade_row
        self.match_on = match_on
        self.match_value = match_value
        self.series_rows = series_rows or []
        self.raise_lookup = raise_lookup
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        if self.raise_lookup:
            raise RuntimeError("boom")
        if "FROM trades" in query:
            wants_int = "WHERE id = $1" in query
            if wants_int and self.match_on == "id" and args[0] == self.match_value:
                return self.trade_row
            if (not wants_int) and self.match_on == "trade_id" and args[0] == self.match_value:
                return self.trade_row
            return None
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        if "FROM swarm_debates" in query:
            return self.series_rows
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:  # pragma: no cover
        return None


def _run(coro):
    return asyncio.run(coro)


def _stub_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(_ids: Any) -> dict[str, str]:
        return {}
    monkeypatch.setattr(dashboard, "resolve_market_titles", _noop)


def test_blank_ref_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)
    assert _run(dashboard.get_trade_detail("")) is None
    assert _run(dashboard.get_trade_detail("   ")) is None
    # Never touched the db.
    assert fake.calls == []


def test_missing_row_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB(trade_row=None, match_on="id", match_value=999)
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)
    out = _run(dashboard.get_trade_detail("123"))
    assert out is None


def test_lookup_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB(raise_lookup=True)
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)
    assert _run(dashboard.get_trade_detail("123")) is None


def test_numeric_ref_uses_id_branch_and_assembles_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trade = {
        "id": 42, "trade_id": "uuid-1", "market_id": "KXNFL-X",
        "market_title": "Seattle vs Arizona",
        "vertical": "sports", "league": "NFL",
        "side": "yes", "action": "buy",
        "quantity": 10.0, "price": 0.55, "fees": 0.1, "pnl": 1.5,
        "model_prob": 0.62, "model_edge": 0.07, "kelly_fraction": 0.05,
        "trading_mode": "live", "exchange": "kalshi",
        "executed_at": "2025-09-04T12:30:00Z",
    }
    series_rows = [
        {"t": "2025-09-04T11:00:00Z", "model_prob": 0.6, "market_price": 0.52},
        {"t": "2025-09-04T12:00:00Z", "model_prob": 0.61, "market_price": 0.54},
    ]
    fake = _FakeDB(
        trade_row=trade, match_on="id", match_value=42, series_rows=series_rows,
    )
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)

    out = _run(dashboard.get_trade_detail("42"))

    assert out is not None
    assert out["trade"]["id"] == 42
    # Lookup query bound id=42 (int).
    lookup = next(c for c in fake.calls if "FROM trades" in c[0])
    assert lookup[1] == (42,)
    # Series query bound the trade's market_id.
    series_call = next(c for c in fake.calls if "FROM swarm_debates" in c[0])
    assert series_call[1] == ("KXNFL-X",)
    # Two debate points + the trade-anchor at executed_at.
    times = [p["t"] for p in out["series"]]
    assert times == [
        "2025-09-04T11:00:00Z",
        "2025-09-04T12:00:00Z",
        "2025-09-04T12:30:00Z",
    ]
    anchor = out["series"][-1]
    assert anchor["model_prob"] == 0.62
    assert anchor["market_price"] == 0.55  # trade.price


def test_uuid_ref_uses_trade_id_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    trade = {
        "id": 7, "trade_id": "abc-uuid", "market_id": "KXNFL-Y",
        "market_title": "X", "vertical": "sports", "league": "NFL",
        "side": "no", "action": "sell",
        "quantity": 5.0, "price": 0.4, "fees": 0.0, "pnl": None,
        "model_prob": 0.5, "model_edge": 0.0, "kelly_fraction": 0.0,
        "trading_mode": "paper", "exchange": "kalshi",
        "executed_at": "2025-09-04T15:00:00Z",
    }
    fake = _FakeDB(
        trade_row=trade, match_on="trade_id", match_value="abc-uuid",
    )
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)

    out = _run(dashboard.get_trade_detail("abc-uuid"))
    assert out is not None
    assert out["trade"]["trade_id"] == "abc-uuid"
    lookup = next(c for c in fake.calls if "FROM trades" in c[0])
    assert "WHERE trade_id = $1" in lookup[0]
    assert lookup[1] == ("abc-uuid",)


def test_series_anchor_added_when_no_debate_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    trade = {
        "id": 1, "trade_id": "t-1", "market_id": "KXMKT-Z",
        "market_title": "Title", "vertical": "sports", "league": "NFL",
        "side": "yes", "action": "buy",
        "quantity": 1.0, "price": 0.7, "fees": 0.0, "pnl": 0.0,
        "model_prob": 0.65, "model_edge": 0.0, "kelly_fraction": 0.0,
        "trading_mode": "live", "exchange": "kalshi",
        "executed_at": "2025-09-04T20:00:00Z",
    }
    fake = _FakeDB(
        trade_row=trade, match_on="id", match_value=1, series_rows=[],
    )
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)

    out = _run(dashboard.get_trade_detail("1"))
    assert out is not None
    # Empty debate rows → anchor is the only point on the chart.
    assert out["series"] == [
        {
            "t": "2025-09-04T20:00:00Z",
            "model_prob": 0.65,
            "market_price": 0.7,
        }
    ]


def test_anchor_skipped_if_executed_at_already_in_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trade = {
        "id": 2, "trade_id": "t-2", "market_id": "KXMKT-A",
        "market_title": "Title", "vertical": "sports", "league": "NFL",
        "side": "yes", "action": "buy",
        "quantity": 1.0, "price": 0.5, "fees": 0.0, "pnl": 0.0,
        "model_prob": 0.5, "model_edge": 0.0, "kelly_fraction": 0.0,
        "trading_mode": "live", "exchange": "kalshi",
        "executed_at": "2025-09-04T20:00:00Z",
    }
    series_rows = [
        {"t": "2025-09-04T20:00:00Z", "model_prob": 0.49, "market_price": 0.51},
    ]
    fake = _FakeDB(
        trade_row=trade, match_on="id", match_value=2, series_rows=series_rows,
    )
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)

    out = _run(dashboard.get_trade_detail("2"))
    assert out is not None
    # Anchor must not double-up — debate row at the same timestamp wins.
    assert len(out["series"]) == 1
    assert out["series"][0]["model_prob"] == 0.49
