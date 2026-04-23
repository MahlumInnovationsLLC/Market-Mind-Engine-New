"""Unit tests for `dashboard.search_global`.

Stubs the asyncpg-style `db` accessor so each branch (ticker / market_id
substring, ISO-date filter, numeric cycle id, empty query, empty result)
can be exercised without a live database.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import dashboard


class _FakeDB:
    """Records every db.fetch call so tests can assert routing/params."""

    def __init__(
        self,
        trade_rows: list[dict[str, Any]] | None = None,
        debate_rows: list[dict[str, Any]] | None = None,
        cycle_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.trade_rows = trade_rows or []
        self.debate_rows = debate_rows or []
        self.cycle_rows = cycle_rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        if "FROM trades" in query:
            return self.trade_rows
        if "FROM swarm_debates" in query:
            return self.debate_rows
        if "FROM trading_cycles" in query:
            return self.cycle_rows
        return []

    async def fetchrow(self, query: str, *args: Any) -> Any:  # pragma: no cover
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:  # pragma: no cover
        return None


def _run(coro):
    return asyncio.run(coro)


def test_empty_query_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(dashboard.search_global("   "))
    assert out == {"trades": [], "debates": [], "cycles": []}
    # No SQL fired — short-circuited before touching the db.
    assert fake.calls == []


def test_ticker_substring_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    trade = {
        "id": 1, "trade_id": "t-1", "market_id": "KXNFL-25SEP04SEAARI-SEA",
        "market_title": None, "side": "yes", "action": "buy",
        "price": 0.55, "pnl": 1.23, "executed_at": "2025-09-04T12:00:00Z",
    }
    fake = _FakeDB(trade_rows=[trade])
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("KXNFL", limit=5))

    assert out["trades"] == [trade]
    # First (trades) query must use the ILIKE substring branch with %query%.
    queries = [c[0] for c in fake.calls]
    trade_calls = [c for c in fake.calls if "FROM trades" in c[0]]
    assert trade_calls, "expected a trades query"
    q, args = trade_calls[0]
    assert "ILIKE" in q
    assert "executed_at::date = $1::date" not in q
    assert args[0] == "%KXNFL%"
    assert args[1] == 5
    # Debates and cycles also queried (no exception swallowing).
    assert any("FROM swarm_debates" in q for q in queries)
    assert any("FROM trading_cycles" in q for q in queries)


def test_iso_date_branch_uses_date_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)

    _run(dashboard.search_global("2025-09-04", limit=10))

    # All three queries should take the ::date branch and pass the raw
    # date string (not a %like% pattern).
    for table in ("FROM trades", "FROM swarm_debates", "FROM trading_cycles"):
        matches = [c for c in fake.calls if table in c[0]]
        assert matches, f"expected query against {table}"
        q, args = matches[0]
        assert "::date = $1::date" in q
        assert args[0] == "2025-09-04"
        assert args[1] == 10


def test_numeric_query_hits_cycle_id_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB(cycle_rows=[{"id": 42, "vertical": "sports",
                                "picks_evaluated": 1, "trades_placed": 1,
                                "started_at": "2025-09-04T12:00:00Z",
                                "completed_at": "2025-09-04T12:01:00Z"}])
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("42"))

    cycle_calls = [c for c in fake.calls if "FROM trading_cycles" in c[0]]
    assert cycle_calls
    q, args = cycle_calls[0]
    assert "WHERE id = $1" in q
    assert args[0] == 42  # coerced to int
    assert out["cycles"][0]["id"] == 42


def test_market_id_substring_matches_debate_kalshi_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    debate = {
        "id": 7, "league": "NFL", "matchup": "SEA @ ARI",
        "market_type": "moneyline",
        "consensus_probability": 0.6, "agreement_rate": 0.8, "approved": True,
        "recommended_action": "buy",
        "created_at": "2025-09-04T12:00:00Z",
    }
    fake = _FakeDB(debate_rows=[debate])
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("SEAARI"))
    debate_calls = [c for c in fake.calls if "FROM swarm_debates" in c[0]]
    assert debate_calls
    q, args = debate_calls[0]
    assert "kalshi_market_id" in q  # game_context substring branch
    assert args[0] == "%SEAARI%"
    assert out["debates"] == [debate]


def test_empty_result_set_returns_empty_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB()  # all empty
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(dashboard.search_global("nope-no-match"))
    assert out == {"trades": [], "debates": [], "cycles": []}


def test_limit_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)
    _run(dashboard.search_global("foo", limit=9999))
    # limit arg is clamped to 50 across all sub-queries.
    for _, args in fake.calls:
        assert args[-1] == 50

    fake2 = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake2)
    _run(dashboard.search_global("foo", limit=0))
    for _, args in fake2.calls:
        assert args[-1] == 1
