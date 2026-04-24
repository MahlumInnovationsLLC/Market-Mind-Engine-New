"""Tests for the Kalshi market-tick cache helper (task #359).

Exercises the cache layer in isolation — the real Kalshi client and a
real Postgres are both stubbed so the assertions only depend on the
helper's own logic:

* :func:`fetch_window` returns normalised series points and reports
  the missing prefix / suffix the caller must still fetch.
* :func:`upsert_candles` parameterises the bucket key correctly and
  swallows DB failures so a sick cache can never take down a render.
* :func:`refresh_market_window` only calls Kalshi for the missing
  ranges and persists the freshly-fetched candles back into the
  cache.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import db as _db
from app.kalshi import client as kalshi_client
from app.kalshi import tick_cache


def _run(coro):
    return asyncio.run(coro)


class _FakeKalshi:
    def __init__(
        self,
        *,
        candles_by_call: list[list[dict[str, Any]]] | None = None,
        raise_with: Exception | None = None,
    ) -> None:
        self._candles_by_call = candles_by_call or []
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
        if self._candles_by_call:
            return {"candlesticks": self._candles_by_call.pop(0)}
        return {"candlesticks": []}

    async def aclose(self) -> None:
        self.closed = True


def test_candles_to_points_normalises_prices() -> None:
    points = tick_cache.candles_to_points(
        [
            {"end_period_ts": 1_700_000_060, "price": {"mean": 47, "close": 48}},
            {"end_period_ts": 1_700_000_120, "price": {"mean": None, "close": 53}},
            {"end_period_ts": 1_700_000_180, "price": {"mean": None, "close": None}},
            {"end_period_ts": 1_700_000_240, "price": {"mean": 60}},
            {"end_period_ts": "not-an-int", "price": {"mean": 70}},
        ]
    )
    assert [p["market_price"] for p in points] == [0.47, 0.53, 0.60]
    assert all(p["model_prob"] is None for p in points)
    assert all(p["t"].endswith("Z") for p in points)


def test_fetch_window_empty_cache_returns_full_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rows in the cache → caller must fetch the entire window."""
    async def fake_fetch(_q: str, *_a: Any) -> list[Any]:
        return []
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    points, missing = _run(
        tick_cache.fetch_window(
            "KXNFL-X", period_interval=1, start_ts=1000, end_ts=2000
        )
    )
    assert points == []
    assert missing == [(1000, 2000)]


def test_fetch_window_db_error_falls_back_to_full_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sick DB looks the same as a cold cache — we still allow the
    live fetch path to run instead of raising into the request handler."""
    async def boom(_q: str, *_a: Any) -> list[Any]:
        raise RuntimeError("connection reset")
    monkeypatch.setattr(_db, "fetch", boom)

    points, missing = _run(
        tick_cache.fetch_window(
            "KXNFL-X", period_interval=1, start_ts=1000, end_ts=2000
        )
    )
    assert points == []
    assert missing == [(1000, 2000)]


def test_fetch_window_full_coverage_returns_no_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache spans the whole window → no Kalshi fetch needed."""
    captured: list[tuple[Any, ...]] = []

    async def fake_fetch(_q: str, *args: Any) -> list[dict[str, Any]]:
        captured.append(args)
        # Three buckets covering [1000, 1180] inclusive at 60s bars.
        return [
            {"end_period_ts": 1060, "close_cents": 48, "mean_cents": 47},
            {"end_period_ts": 1120, "close_cents": None, "mean_cents": 53},
            {"end_period_ts": 1180, "close_cents": 60, "mean_cents": None},
        ]
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    points, missing = _run(
        tick_cache.fetch_window(
            "KXNFL-X", period_interval=1, start_ts=1000, end_ts=1200
        )
    )
    assert missing == []
    assert [p["market_price"] for p in points] == [0.47, 0.53, 0.60]
    # Bound parameters are (ticker, period_interval, start, end).
    assert captured[0] == ("KXNFL-X", 1, 1000, 1200)


def test_fetch_window_reports_prefix_and_suffix_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache covers the middle but not the head/tail → caller fetches both."""
    async def fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        # Big window: [0, 6000s). Bars are 1m (60s). Cache covers 2000..3000.
        return [
            {"end_period_ts": 2000, "close_cents": 50, "mean_cents": None},
            {"end_period_ts": 3000, "close_cents": 55, "mean_cents": None},
        ]
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    points, missing = _run(
        tick_cache.fetch_window(
            "KXNFL-X", period_interval=1, start_ts=0, end_ts=6000
        )
    )
    # Cached buckets surface as points; missing reports both edges.
    assert [p["market_price"] for p in points] == [0.50, 0.55]
    assert missing == [(0, 2000), (3000, 6000)]


def test_fetch_window_drops_null_priced_buckets_from_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bucket with no price is still cache-covered (so it must not
    show up in ``missing``) but has nothing to render on the chart."""
    async def fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        # Cache spans the entire window; the middle bucket has null price.
        return [
            {"end_period_ts": 1060, "close_cents": 50, "mean_cents": None},
            {"end_period_ts": 1120, "close_cents": None, "mean_cents": None},
            {"end_period_ts": 1180, "close_cents": 55, "mean_cents": None},
        ]
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    points, missing = _run(
        tick_cache.fetch_window(
            "KXNFL-X", period_interval=1, start_ts=1000, end_ts=1200
        )
    )
    assert missing == []
    # Null-priced bucket is silently filtered out of the rendered series.
    assert [p["market_price"] for p in points] == [0.50, 0.55]


def test_fetch_window_short_circuits_on_blank_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ticker or non-positive window returns ``([], [])`` without
    hitting the DB at all."""
    async def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("DB should not be touched")
    monkeypatch.setattr(_db, "fetch", boom)

    assert _run(
        tick_cache.fetch_window("", period_interval=1, start_ts=0, end_ts=1)
    ) == ([], [])
    assert _run(
        tick_cache.fetch_window("KXX", period_interval=1, start_ts=10, end_ts=10)
    ) == ([], [])


def test_upsert_candles_parameterises_per_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, list[tuple[Any, ...]]]] = []

    async def fake_executemany(query: str, args_list: list[tuple[Any, ...]]) -> None:
        captured.append((query, list(args_list)))
    monkeypatch.setattr(_db, "executemany", fake_executemany)

    n = _run(
        tick_cache.upsert_candles(
            "KXNFL-X",
            period_interval=1,
            candles=[
                {
                    "end_period_ts": 1060,
                    "price": {"close": 48, "mean": 47},
                },
                {
                    # Empty bucket — both prices null, still recorded
                    # so the cache covers the bucket.
                    "end_period_ts": 1120,
                    "price": {"close": None, "mean": None},
                },
                {
                    # No end_period_ts → silently skipped.
                    "price": {"close": 99},
                },
            ],
        )
    )
    assert n == 2
    query, rows = captured[0]
    assert "INSERT INTO market_price_ticks" in query
    assert "ON CONFLICT (market_ticker, period_interval, bucket_ts)" in query
    assert rows == [
        ("KXNFL-X", 1, 1060, 48, 47),
        ("KXNFL-X", 1, 1120, None, None),
    ]


def test_upsert_candles_swallows_db_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("connection reset")
    monkeypatch.setattr(_db, "executemany", boom)
    # Returns 0 instead of raising — the live in-memory render still
    # happens with whatever Kalshi just returned.
    n = _run(
        tick_cache.upsert_candles(
            "KXNFL-X",
            period_interval=1,
            candles=[{"end_period_ts": 1060, "price": {"mean": 47}}],
        )
    )
    assert n == 0


def test_refresh_market_window_uses_passed_client_for_missing_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache covers (2000..3000) of (0..6000); refresh hits Kalshi
    twice (one prefix, one suffix) using the passed-in client."""
    async def fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        return [
            {"end_period_ts": 2000, "close_cents": 50, "mean_cents": None},
            {"end_period_ts": 3000, "close_cents": 55, "mean_cents": None},
        ]
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    upsert_calls: list[tuple[str, int, list[dict[str, Any]]]] = []

    async def fake_upsert(ticker: str, *, period_interval: int, candles: Any) -> int:
        upsert_calls.append((ticker, period_interval, list(candles)))
        return len(list(upsert_calls[-1][2]))
    monkeypatch.setattr(tick_cache, "upsert_candles", fake_upsert)

    fake = _FakeKalshi(
        candles_by_call=[
            [{"end_period_ts": 60, "price": {"mean": 30}}],
            [{"end_period_ts": 5000, "price": {"mean": 90}}],
        ]
    )

    written = _run(
        tick_cache.refresh_market_window(
            "KXNFL-X",
            period_interval=1,
            start_ts=0,
            end_ts=6000,
            client=fake,
        )
    )
    assert written >= 1
    assert [c["start_ts"] for c in fake.calls] == [0, 3000]
    assert [c["end_ts"] for c in fake.calls] == [2000, 6000]
    # When a client is passed in we must NOT close it — that's the
    # caller's responsibility (the refresh loop closes it once per
    # sweep across many markets).
    assert fake.closed is False


def test_refresh_market_window_returns_zero_when_cache_already_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        # Cache fully spans (1000..1200) at 60s bars.
        return [
            {"end_period_ts": 1060, "close_cents": 50, "mean_cents": None},
            {"end_period_ts": 1120, "close_cents": 51, "mean_cents": None},
            {"end_period_ts": 1180, "close_cents": 52, "mean_cents": None},
        ]
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    fake = _FakeKalshi()
    n = _run(
        tick_cache.refresh_market_window(
            "KXNFL-X",
            period_interval=1,
            start_ts=1000,
            end_ts=1200,
            client=fake,
        )
    )
    assert n == 0
    # No live Kalshi work because the cache covered the full window.
    assert fake.calls == []


def test_refresh_market_window_swallows_kalshi_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        return []
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    fake = _FakeKalshi(
        raise_with=kalshi_client.KalshiAPIError(503, {"error": "boom"})
    )
    n = _run(
        tick_cache.refresh_market_window(
            "KXNFL-X",
            period_interval=1,
            start_ts=0,
            end_ts=600,
            client=fake,
        )
    )
    assert n == 0
    # We tried once before giving up — the loop is per-range, not retried.
    assert len(fake.calls) == 1
