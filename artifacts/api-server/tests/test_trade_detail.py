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


# ---------------------------------------------------------------------------
# Task #752 step 1 — trading_mode scoping
# ---------------------------------------------------------------------------


class _ModeAwareFakeDB:
    """Stricter stub that bound-checks the trading_mode parameter so we
    can prove the WHERE clause carries it through to the bind list."""

    def __init__(
        self,
        *,
        trade_row: dict[str, Any] | None,
        expect_mode: str | None,
        ref_kind: str = "id",  # "id" or "trade_id"
        ref_value: Any = None,
    ) -> None:
        self.trade_row = trade_row
        self.expect_mode = expect_mode
        self.ref_kind = ref_kind
        self.ref_value = ref_value
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        if "FROM trades" not in query:
            return None
        if self.expect_mode is None:
            # When no mode is requested the route uses the legacy
            # 1-arg lookup: WHERE id = $1 / WHERE trade_id = $1.
            # ``trading_mode`` itself appears in the SELECT list, so
            # we check the WHERE-clause shape instead.
            assert "trading_mode = $2" not in query
            assert len(args) == 1
        else:
            assert "AND trading_mode = $2" in query
            assert len(args) == 2
            assert args[1] == self.expect_mode
        if args[0] == self.ref_value:
            return self.trade_row
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:  # pragma: no cover
        return None


def _trade(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": 99, "trade_id": "uuid-99", "market_id": "KXNFL-99",
        "market_title": "Title", "vertical": "sports", "league": "NFL",
        "side": "yes", "action": "buy",
        "quantity": 1.0, "price": 0.5, "fees": 0.0, "pnl": 0.0,
        "model_prob": 0.6, "model_edge": 0.05, "kelly_fraction": 0.02,
        "trading_mode": "live", "exchange": "kalshi",
        "executed_at": "2026-04-22T12:00:00Z",
    }
    base.update(overrides)
    return base


def test_trade_detail_with_trading_mode_live_binds_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ModeAwareFakeDB(
        trade_row=_trade(id=42, trading_mode="live"),
        expect_mode="live",
        ref_kind="id", ref_value=42,
    )
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)
    out = _run(dashboard.get_trade_detail("42", trading_mode="live"))
    assert out is not None
    assert out["trade"]["id"] == 42


def test_trade_detail_with_trading_mode_paper_binds_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ModeAwareFakeDB(
        trade_row=_trade(id=7, trading_mode="paper"),
        expect_mode="paper",
        ref_kind="id", ref_value=7,
    )
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)
    out = _run(dashboard.get_trade_detail("7", trading_mode="paper"))
    assert out is not None
    assert out["trade"]["trading_mode"] == "paper"


def test_trade_detail_uuid_branch_with_trading_mode_binds_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ModeAwareFakeDB(
        trade_row=_trade(trade_id="abc-uuid", trading_mode="live"),
        expect_mode="live",
        ref_kind="trade_id", ref_value="abc-uuid",
    )
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)
    out = _run(dashboard.get_trade_detail("abc-uuid", trading_mode="live"))
    assert out is not None


def test_trade_detail_cross_mode_lookup_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Round-11 leak: deep-linking from a paper-mode dashboard into
    a live trade ID must NOT return that trade.

    The stub returns the row only when the WHERE bound id matches —
    but with mode mismatch the route would have to send `live` to find
    it and `paper` to confirm the filter is wired.  Easiest proof:
    request id=42 with trading_mode=paper and have the stub check
    that when mode=paper is bound but the trade is live, no row comes
    back."""
    class _StrictFake:
        calls: list[tuple[Any, ...]] = []
        async def fetchrow(self, query, *args):
            self.calls.append(args)
            assert "trading_mode = $2" in query
            # Only return a row if mode matches — paper ≠ live.
            if args[1] == "live":
                return _trade(id=42, trading_mode="live")
            return None
        async def fetch(self, query, *args):
            return []
        async def fetchval(self, query, *args):
            return None

    fake = _StrictFake()
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)

    # Paper-mode caller asks for id=42 (which is live) → should be None.
    out_paper = _run(dashboard.get_trade_detail("42", trading_mode="paper"))
    assert out_paper is None

    # Same id with live mode → row comes back.
    out_live = _run(dashboard.get_trade_detail("42", trading_mode="live"))
    assert out_live is not None


def test_trade_detail_invalid_trading_mode_falls_back_to_no_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: an invalid trading_mode at the helper level
    (e.g. caller passes a typo) is treated as 'no filter' so the row
    is still found.  The HTTP layer's _resolve_dashboard_trading_mode
    is the real validation point — this test just locks in the
    helper's permissive contract."""
    fake = _ModeAwareFakeDB(
        trade_row=_trade(id=1),
        expect_mode=None,  # helper falls back to no-filter
        ref_kind="id", ref_value=1,
    )
    monkeypatch.setattr(dashboard, "db", fake)
    _stub_titles(monkeypatch)
    out = _run(dashboard.get_trade_detail("1", trading_mode="bogus"))
    assert out is not None


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
