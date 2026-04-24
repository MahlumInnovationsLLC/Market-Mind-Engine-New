"""Coverage for the Polymarket prices-history overlay added to
``dashboard.get_trade_detail`` (task #360).

These tests exercise ``_fetch_polymarket_market_price_history`` and the
exchange-aware dispatch in ``get_trade_detail`` so Polymarket trade-
detail pages get the same continuous market-price line as Kalshi.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import dashboard
from app.polymarket import client as polymarket_client


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class _FakePolyClient:
    def __init__(
        self,
        *,
        market: dict[str, Any] | None = None,
        history: list[dict[str, Any]] | None = None,
        get_market_raises: Exception | None = None,
        history_raises: Exception | None = None,
    ) -> None:
        self._market = market
        self._history = history or []
        self._get_market_raises = get_market_raises
        self._history_raises = history_raises
        self.market_calls: list[str] = []
        self.history_calls: list[dict[str, Any]] = []
        self.closed = False

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        self.market_calls.append(condition_id)
        if self._get_market_raises is not None:
            raise self._get_market_raises
        return self._market or {}

    async def get_prices_history(
        self,
        token_id: str,
        *,
        start_ts: int,
        end_ts: int,
        fidelity: int,
    ) -> list[dict[str, Any]]:
        self.history_calls.append(
            {
                "token_id": token_id,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "fidelity": fidelity,
            }
        )
        if self._history_raises is not None:
            raise self._history_raises
        return list(self._history)

    async def aclose(self) -> None:
        self.closed = True


def _patch_client(monkeypatch, fake: _FakePolyClient) -> None:
    monkeypatch.setattr(
        polymarket_client, "PolymarketClient", lambda *a, **kw: fake
    )


# ---------------------------------------------------------------------------
# Token selection
# ---------------------------------------------------------------------------


def test_select_polymarket_token_id_matches_yes_outcome():
    market = {
        "tokens": [
            {"token_id": "111", "outcome": "Yes"},
            {"token_id": "222", "outcome": "No"},
        ]
    }
    assert dashboard._select_polymarket_token_id(market, "yes") == "111"
    assert dashboard._select_polymarket_token_id(market, "YES") == "111"
    assert dashboard._select_polymarket_token_id(market, "no") == "222"


def test_select_polymarket_token_id_falls_back_to_first_token():
    market = {
        "tokens": [
            {"token_id": "aaa", "outcome": "Trump"},
            {"token_id": "bbb", "outcome": "Harris"},
        ]
    }
    # Side doesn't match a Yes/No label → first token wins so the chart
    # still renders for custom-outcome markets.
    assert dashboard._select_polymarket_token_id(market, "yes") == "aaa"


def test_select_polymarket_token_id_empty_market():
    assert dashboard._select_polymarket_token_id(None, "yes") is None
    assert dashboard._select_polymarket_token_id({}, "yes") is None
    assert dashboard._select_polymarket_token_id({"tokens": []}, "yes") is None


# ---------------------------------------------------------------------------
# History → points normalisation
# ---------------------------------------------------------------------------


def test_polymarket_history_to_points_normalises_decimals():
    rows = [
        {"t": 1_700_000_060, "p": 0.47},
        {"t": 1_700_000_120, "p": "0.53"},  # numeric strings parse cleanly
        {"t": 1_700_000_180, "p": None},     # bad price → dropped
        {"t": "bad", "p": 0.6},               # bad ts → dropped
        "not-a-dict",                          # garbage → dropped
        {"t": 1_700_000_240, "p": 0.6},
    ]
    points = dashboard._polymarket_history_to_points(rows)
    assert [p["market_price"] for p in points] == [0.47, 0.53, 0.6]
    assert all(p["model_prob"] is None for p in points)
    assert all(p["t"].endswith("Z") for p in points)


# ---------------------------------------------------------------------------
# _fetch_polymarket_market_price_history
# ---------------------------------------------------------------------------


def test_fetch_polymarket_history_returns_normalised_points(monkeypatch):
    fake = _FakePolyClient(
        market={
            "tokens": [
                {"token_id": "111", "outcome": "Yes"},
                {"token_id": "222", "outcome": "No"},
            ]
        },
        history=[
            {"t": 1_700_000_060, "p": 0.47},
            {"t": 1_700_000_120, "p": 0.53},
        ],
    )
    _patch_client(monkeypatch, fake)

    out = _run(
        dashboard._fetch_polymarket_market_price_history(
            "0xCONDITION",
            trade_side="yes",
            executed_at="2023-11-14T22:14:00Z",
            debate_points=[],
        )
    )

    assert [p["market_price"] for p in out] == [0.47, 0.53]
    assert fake.market_calls == ["0xCONDITION"]
    assert len(fake.history_calls) == 1
    call = fake.history_calls[0]
    assert call["token_id"] == "111"
    # ±60min default window → 120min span → minute fidelity.
    assert call["fidelity"] == 1
    assert fake.closed is True


def test_fetch_polymarket_history_extends_window_to_cover_debates(monkeypatch):
    fake = _FakePolyClient(
        market={"tokens": [{"token_id": "111", "outcome": "Yes"}]},
        history=[],
    )
    _patch_client(monkeypatch, fake)

    _run(
        dashboard._fetch_polymarket_market_price_history(
            "0xCONDITION",
            trade_side="yes",
            executed_at="2023-11-14T22:00:00Z",
            debate_points=[
                {"t": "2023-11-14T18:00:00Z"},
                {"t": "2023-11-14T23:30:00Z"},
            ],
        )
    )
    call = fake.history_calls[0]
    # 5.5h span → hourly fidelity (mirrors Kalshi mapping).
    assert call["fidelity"] == 60


def test_fetch_polymarket_history_swallows_get_market_errors(monkeypatch):
    fake = _FakePolyClient(
        get_market_raises=polymarket_client.PolymarketAPIError(
            404, {"error": "missing"}
        ),
    )
    _patch_client(monkeypatch, fake)

    out = _run(
        dashboard._fetch_polymarket_market_price_history(
            "0xCONDITION",
            trade_side="yes",
            executed_at="2023-11-14T22:00:00Z",
            debate_points=[],
        )
    )
    assert out == []
    assert fake.history_calls == []
    assert fake.closed is True


def test_fetch_polymarket_history_swallows_history_errors(monkeypatch):
    fake = _FakePolyClient(
        market={"tokens": [{"token_id": "111", "outcome": "Yes"}]},
        history_raises=polymarket_client.PolymarketAPIError(
            500, {"error": "boom"}
        ),
    )
    _patch_client(monkeypatch, fake)

    out = _run(
        dashboard._fetch_polymarket_market_price_history(
            "0xCONDITION",
            trade_side="yes",
            executed_at="2023-11-14T22:00:00Z",
            debate_points=[],
        )
    )
    assert out == []
    assert fake.closed is True


def test_fetch_polymarket_history_swallows_construction_errors(monkeypatch):
    """Missing creds shouldn't raise (public endpoint), but we defend
    against a settings-induced construction failure all the same."""
    def _boom(*_a, **_kw):
        raise RuntimeError("settings unavailable")
    monkeypatch.setattr(polymarket_client, "PolymarketClient", _boom)

    out = _run(
        dashboard._fetch_polymarket_market_price_history(
            "0xCONDITION",
            trade_side="yes",
            executed_at="2023-11-14T22:00:00Z",
            debate_points=[],
        )
    )
    assert out == []


def test_fetch_polymarket_history_no_anchor_returns_empty(monkeypatch):
    fake = _FakePolyClient()
    _patch_client(monkeypatch, fake)
    out = _run(
        dashboard._fetch_polymarket_market_price_history(
            "0xCONDITION",
            trade_side="yes",
            executed_at=None,
            debate_points=[],
        )
    )
    assert out == []
    assert fake.market_calls == []
    assert fake.history_calls == []


def test_fetch_polymarket_history_skips_when_market_has_no_tokens(monkeypatch):
    fake = _FakePolyClient(market={"tokens": []})
    _patch_client(monkeypatch, fake)
    out = _run(
        dashboard._fetch_polymarket_market_price_history(
            "0xCONDITION",
            trade_side="yes",
            executed_at="2023-11-14T22:00:00Z",
            debate_points=[],
        )
    )
    assert out == []
    # We made the market lookup but never fired the history call.
    assert fake.market_calls == ["0xCONDITION"]
    assert fake.history_calls == []


# ---------------------------------------------------------------------------
# get_trade_detail dispatch
# ---------------------------------------------------------------------------


class _TradeDetailFakeDB:
    def __init__(self, trade_row: dict[str, Any]) -> None:
        self._trade_row = trade_row

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "FROM trades" in query:
            return self._trade_row
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []


def _stub_titles(monkeypatch):
    async def _noop(_ids):
        return {}
    monkeypatch.setattr(dashboard, "resolve_market_titles", _noop)


def test_get_trade_detail_routes_polymarket_trade_to_polymarket_helper(monkeypatch):
    trade = {
        "id": 9, "trade_id": "poly-1", "market_id": "0xCONDITION",
        "market_title": "Will X happen?", "vertical": "elections",
        "league": None, "side": "yes", "action": "buy",
        "quantity": 10.0, "price": 0.55, "fees": 0.0, "pnl": None,
        "model_prob": 0.6, "model_edge": 0.05, "kelly_fraction": 0.04,
        "trading_mode": "live", "exchange": "polymarket",
        "executed_at": "2025-09-04T12:30:00Z",
    }
    monkeypatch.setattr(dashboard, "db", _TradeDetailFakeDB(trade))
    _stub_titles(monkeypatch)

    poly_calls: list[dict[str, Any]] = []

    async def _fake_poly(condition_id, *, trade_side, executed_at, debate_points):
        poly_calls.append(
            {
                "condition_id": condition_id,
                "trade_side": trade_side,
                "executed_at": executed_at,
            }
        )
        return [
            {"t": "2025-09-04T12:00:00Z", "model_prob": None, "market_price": 0.52},
            {"t": "2025-09-04T12:15:00Z", "model_prob": None, "market_price": 0.54},
        ]

    async def _fake_kalshi(*_a, **_kw):  # must NOT be called for poly trades
        raise AssertionError("Kalshi helper called for a Polymarket trade")

    monkeypatch.setattr(
        dashboard, "_fetch_polymarket_market_price_history", _fake_poly
    )
    monkeypatch.setattr(
        dashboard, "_fetch_market_price_history", _fake_kalshi
    )

    out = _run(dashboard.get_trade_detail("9"))

    assert out is not None
    assert poly_calls == [
        {
            "condition_id": "0xCONDITION",
            "trade_side": "yes",
            "executed_at": "2025-09-04T12:30:00Z",
        }
    ]
    times = [p["t"] for p in out["series"]]
    # Poly history rows + the trade-anchor at executed_at.
    assert times == [
        "2025-09-04T12:00:00Z",
        "2025-09-04T12:15:00Z",
        "2025-09-04T12:30:00Z",
    ]


def test_get_trade_detail_routes_kalshi_trade_to_kalshi_helper(monkeypatch):
    trade = {
        "id": 5, "trade_id": "k-1", "market_id": "KXNFL-25NOV23DETHGB-DET",
        "market_title": "DET vs HGB", "vertical": "sports",
        "league": "NFL", "side": "yes", "action": "buy",
        "quantity": 10.0, "price": 0.55, "fees": 0.0, "pnl": None,
        "model_prob": 0.6, "model_edge": 0.05, "kelly_fraction": 0.04,
        "trading_mode": "live", "exchange": "kalshi",
        "executed_at": "2025-09-04T12:30:00Z",
    }
    monkeypatch.setattr(dashboard, "db", _TradeDetailFakeDB(trade))
    _stub_titles(monkeypatch)

    kalshi_calls: list[str] = []

    async def _fake_kalshi(market_id, *, executed_at, debate_points):
        kalshi_calls.append(market_id)
        return []

    async def _fake_poly(*_a, **_kw):  # must NOT be called for kalshi trades
        raise AssertionError("Polymarket helper called for a Kalshi trade")

    monkeypatch.setattr(
        dashboard, "_fetch_market_price_history", _fake_kalshi
    )
    monkeypatch.setattr(
        dashboard, "_fetch_polymarket_market_price_history", _fake_poly
    )

    out = _run(dashboard.get_trade_detail("5"))
    assert out is not None
    assert kalshi_calls == ["KXNFL-25NOV23DETHGB-DET"]


def test_get_trade_detail_unknown_exchange_falls_back_to_kalshi_path(monkeypatch):
    """Older trades (and any unknown venue) keep the historical Kalshi
    fallback so we don't regress trade-detail rendering for legacy rows
    where the exchange column was never populated."""
    trade = {
        "id": 7, "trade_id": "legacy-1", "market_id": "KXMKT-LEGACY",
        "market_title": None, "vertical": "sports", "league": "NFL",
        "side": "yes", "action": "buy",
        "quantity": 1.0, "price": 0.5, "fees": 0.0, "pnl": None,
        "model_prob": 0.5, "model_edge": 0.0, "kelly_fraction": 0.0,
        "trading_mode": "paper", "exchange": None,
        "executed_at": "2025-09-04T20:00:00Z",
    }
    monkeypatch.setattr(dashboard, "db", _TradeDetailFakeDB(trade))
    _stub_titles(monkeypatch)

    kalshi_calls: list[str] = []

    async def _fake_kalshi(market_id, *, executed_at, debate_points):
        kalshi_calls.append(market_id)
        return []

    async def _fake_poly(*_a, **_kw):
        raise AssertionError("Polymarket helper called for unknown exchange")

    monkeypatch.setattr(
        dashboard, "_fetch_market_price_history", _fake_kalshi
    )
    monkeypatch.setattr(
        dashboard, "_fetch_polymarket_market_price_history", _fake_poly
    )

    out = _run(dashboard.get_trade_detail("7"))
    assert out is not None
    assert kalshi_calls == ["KXMKT-LEGACY"]
