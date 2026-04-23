"""Coverage for the Kalshi candlestick overlay added to
``dashboard.get_trade_detail`` (task #141).

These tests exercise the small helper ``_fetch_market_price_history``
in isolation so they don't need a real Kalshi cluster — they patch
``KalshiClient`` to a fake whose ``get_market_candlesticks`` returns
fixture rows. The helper owns:
  * picking the bar size from the window span
  * converting cents -> 0..1 fractions
  * silently swallowing transport / auth failures so the
    debate-snapshot fallback still drives the chart.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import dashboard
from app.kalshi import client as kalshi_client


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class _FakeClient:
    def __init__(
        self,
        *,
        candles: list[dict[str, Any]] | None = None,
        raise_with: Exception | None = None,
    ) -> None:
        self._candles = candles or []
        self._raise_with = raise_with
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def get_market_candlesticks(
        self, ticker: str, *, start_ts: int, end_ts: int, period_interval: int
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "ticker": ticker,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            }
        )
        if self._raise_with is not None:
            raise self._raise_with
        return {"candlesticks": list(self._candles)}

    async def aclose(self) -> None:
        self.closed = True


def _patch_client(monkeypatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(
        kalshi_client, "KalshiClient", lambda *a, **kw: fake
    )


def test_select_period_interval_buckets_by_span():
    # short window stays at minute bars
    assert dashboard._select_period_interval(30) == 1
    assert dashboard._select_period_interval(240) == 1
    # multi-day window steps up to hourly
    assert dashboard._select_period_interval(60 * 24) == 60
    # long retro window falls back to daily
    assert dashboard._select_period_interval(60 * 24 * 30) == 1440


def test_fetch_market_price_history_returns_normalised_points(monkeypatch):
    """Cents -> fraction conversion, ISO-Z timestamp formatting, and
    each candle becomes a point with model_prob=None so the front-end
    can keep the model line driven by debate snapshots only."""
    fake = _FakeClient(
        candles=[
            {
                "end_period_ts": 1_700_000_060,
                "price": {"mean": 47, "close": 48},
            },
            {
                "end_period_ts": 1_700_000_120,
                # mean missing -> falls back to close
                "price": {"mean": None, "close": 53},
            },
            {
                "end_period_ts": 1_700_000_180,
                # both missing -> dropped silently
                "price": {"mean": None, "close": None},
            },
            {
                "end_period_ts": 1_700_000_240,
                "price": {"mean": 60},
            },
        ]
    )
    _patch_client(monkeypatch, fake)

    points = _run(
        dashboard._fetch_market_price_history(
            "KXNFL-25NOV23DETHGB-DET",
            executed_at="2023-11-14T22:14:00Z",
            debate_points=[],
        )
    )

    assert [p["market_price"] for p in points] == [0.47, 0.53, 0.60]
    assert all(p["model_prob"] is None for p in points)
    assert points[0]["t"].endswith("Z")
    assert fake.calls and fake.calls[0]["ticker"] == "KXNFL-25NOV23DETHGB-DET"
    # ±60min default window around executed_at => 120min span -> 1m bars
    assert fake.calls[0]["period_interval"] == 1
    assert fake.closed is True


def test_fetch_market_price_history_extends_window_to_cover_debates(monkeypatch):
    fake = _FakeClient(candles=[])
    _patch_client(monkeypatch, fake)

    _run(
        dashboard._fetch_market_price_history(
            "KXNFL-25NOV23DETHGB-DET",
            executed_at="2023-11-14T22:00:00Z",
            debate_points=[
                {"t": "2023-11-14T18:00:00Z"},
                {"t": "2023-11-14T23:30:00Z"},
            ],
        )
    )
    call = fake.calls[0]
    # Earliest debate (18:00) extends start_ts past the default
    # exec-1h (21:00) anchor.
    assert call["start_ts"] <= 1_700_000_000  # any value before exec
    # 5.5h span -> hourly bars
    assert call["period_interval"] == 60


def test_fetch_market_price_history_swallows_kalshi_errors(monkeypatch):
    fake = _FakeClient(
        raise_with=kalshi_client.KalshiAPIError(401, {"error": "auth"})
    )
    _patch_client(monkeypatch, fake)

    out = _run(
        dashboard._fetch_market_price_history(
            "KXNFL-25NOV23DETHGB-DET",
            executed_at="2023-11-14T22:00:00Z",
            debate_points=[],
        )
    )
    assert out == []
    assert fake.closed is True


def test_fetch_market_price_history_swallows_construction_errors(monkeypatch):
    """Missing credentials raise at KalshiClient() — must not blow up
    the trade-detail response."""
    def _boom(*_a, **_kw):
        raise RuntimeError("missing creds")

    monkeypatch.setattr(kalshi_client, "KalshiClient", _boom)
    out = _run(
        dashboard._fetch_market_price_history(
            "KXNFL-25NOV23DETHGB-DET",
            executed_at="2023-11-14T22:00:00Z",
            debate_points=[],
        )
    )
    assert out == []


def test_fetch_market_price_history_no_anchor_returns_empty(monkeypatch):
    """No executed_at and no debate timestamps -> nothing to query."""
    fake = _FakeClient()
    _patch_client(monkeypatch, fake)
    out = _run(
        dashboard._fetch_market_price_history(
            "KXNFL-x-y", executed_at=None, debate_points=[]
        )
    )
    assert out == []
    assert fake.calls == []
