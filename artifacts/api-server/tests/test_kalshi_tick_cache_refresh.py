"""Tests for the periodic Kalshi tick-cache refresh loop (task #359).

Asserts that the loop:

* Resolves env-var-tunable knobs with sane floors.
* Picks the most recently-traded Kalshi tickers and asks the cache
  helper to top each one up.
* Continues across a per-market failure so one bad ticker can't kill
  the sweep.
* Honours ``AXIOM_TICK_CACHE_REFRESH_ENABLED=0`` and shuts down
  cleanly via ``stop()``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import db as _db
from app.kalshi import client as kalshi_client
from app.kalshi import tick_cache
from app.kalshi import tick_cache_refresh as refresh


_ENV_KEYS = (
    "AXIOM_TICK_CACHE_REFRESH_INTERVAL_SECONDS",
    "AXIOM_TICK_CACHE_LOOKBACK_HOURS",
    "AXIOM_TICK_CACHE_WINDOW_HOURS",
    "AXIOM_TICK_CACHE_PERIOD_INTERVAL",
    "AXIOM_TICK_CACHE_MAX_MARKETS",
    "AXIOM_TICK_CACHE_REFRESH_ENABLED",
)


@pytest.fixture(autouse=True)
def _reset_env_and_state(monkeypatch: pytest.MonkeyPatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    refresh._reset_state_for_tests()
    yield
    refresh._reset_state_for_tests()


class _FakeKalshi:
    """Minimal Kalshi client stand-in for the refresh loop."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_defaults_match_spec() -> None:
    s = refresh.state()
    assert s["interval_seconds"] == float(refresh.DEFAULT_INTERVAL_SECONDS)
    assert s["lookback_hours"] == refresh.DEFAULT_LOOKBACK_HOURS
    assert s["window_hours"] == refresh.DEFAULT_WINDOW_HOURS
    assert s["period_interval"] == refresh.DEFAULT_PERIOD_INTERVAL
    assert s["max_markets"] == refresh.DEFAULT_MAX_MARKETS


def test_interval_floor_protects_against_typo(monkeypatch: pytest.MonkeyPatch) -> None:
    # 60s floor — an aggressive '5' must be clamped so we never hammer Kalshi.
    monkeypatch.setenv("AXIOM_TICK_CACHE_REFRESH_INTERVAL_SECONDS", "5")
    assert refresh._interval_seconds() == 60.0


def test_period_interval_snaps_to_kalshi_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_TICK_CACHE_PERIOD_INTERVAL", "30")
    # 30 isn't a real Kalshi bar size — fall back to the default (1m).
    assert refresh._period_interval() == refresh.DEFAULT_PERIOD_INTERVAL
    monkeypatch.setenv("AXIOM_TICK_CACHE_PERIOD_INTERVAL", "60")
    assert refresh._period_interval() == 60


def test_refresh_once_with_no_active_markets_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(_q: str, *_a: Any) -> list[Any]:
        return []
    monkeypatch.setattr(_db, "fetch", fake_fetch)
    monkeypatch.setattr(
        kalshi_client, "KalshiClient",
        lambda *_a, **_kw: pytest.fail("KalshiClient should not be opened"),
    )

    n = asyncio.run(refresh.refresh_once())
    assert n == 0
    s = refresh.state()
    assert s["last_markets"] == 0
    assert s["last_buckets_written"] == 0


def test_refresh_once_calls_cache_helper_for_each_active_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        return [
            {"market_id": "KX-A", "last_traded": "2025-01-01T00:00:00Z"},
            {"market_id": "KX-B", "last_traded": "2025-01-01T00:00:00Z"},
        ]
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    fake = _FakeKalshi()
    monkeypatch.setattr(
        kalshi_client, "KalshiClient", lambda *_a, **_kw: fake,
    )

    seen_tickers: list[str] = []

    async def fake_refresh(
        ticker: str, *, period_interval: int, start_ts: int, end_ts: int, client: Any
    ) -> int:
        seen_tickers.append(ticker)
        # Same client object is reused across markets.
        assert client is fake
        # Per-market window matches the configured lookback.
        assert end_ts > start_ts
        assert period_interval == refresh._period_interval()
        return 3
    monkeypatch.setattr(tick_cache, "refresh_market_window", fake_refresh)

    n = asyncio.run(refresh.refresh_once())
    assert n == 6  # 3 buckets * 2 markets
    assert seen_tickers == ["KX-A", "KX-B"]
    s = refresh.state()
    assert s["last_markets"] == 2
    assert s["last_buckets_written"] == 6
    assert s["total_buckets_written"] == 6
    # Loop closes the client itself — the per-market helper must NOT.
    assert fake.closed is True


def test_refresh_once_continues_after_per_market_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        return [
            {"market_id": "KX-OK", "last_traded": "2025-01-01T00:00:00Z"},
            {"market_id": "KX-BAD", "last_traded": "2025-01-01T00:00:00Z"},
        ]
    monkeypatch.setattr(_db, "fetch", fake_fetch)
    monkeypatch.setattr(
        kalshi_client, "KalshiClient", lambda *_a, **_kw: _FakeKalshi(),
    )

    async def fake_refresh(ticker: str, **_kw: Any) -> int:
        if ticker == "KX-BAD":
            raise RuntimeError("kalshi 5xx")
        return 2
    monkeypatch.setattr(tick_cache, "refresh_market_window", fake_refresh)

    n = asyncio.run(refresh.refresh_once())
    # KX-OK contributed 2, KX-BAD contributed 0 — the sweep didn't abort.
    assert n == 2
    s = refresh.state()
    assert s["last_markets"] == 2
    assert "kalshi 5xx" in (s["last_error"] or "")


def test_refresh_once_handles_kalshi_client_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        return [{"market_id": "KX-A", "last_traded": "2025-01-01T00:00:00Z"}]
    monkeypatch.setattr(_db, "fetch", fake_fetch)

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("missing creds")
    monkeypatch.setattr(kalshi_client, "KalshiClient", boom)

    n = asyncio.run(refresh.refresh_once())
    assert n == 0
    s = refresh.state()
    assert s["last_markets"] == 1
    assert "missing creds" in (s["last_error"] or "")


def test_start_runs_loop_and_stop_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def fake_refresh_once() -> int:
        calls.append(1)
        return 0
    monkeypatch.setattr(refresh, "refresh_once", fake_refresh_once)
    monkeypatch.setenv("AXIOM_TICK_CACHE_REFRESH_INTERVAL_SECONDS", "300")

    async def go() -> None:
        await refresh.start()
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls, "expected refresh_once to have run at least once"
        assert refresh.state()["running"] is True
        await refresh.stop()
        assert refresh.state()["running"] is False

    asyncio.run(go())


def test_start_respects_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_TICK_CACHE_REFRESH_ENABLED", "0")

    async def boom() -> int:
        raise AssertionError("refresh_once must not run when disabled")
    monkeypatch.setattr(refresh, "refresh_once", boom)

    async def go() -> None:
        await refresh.start()
        assert refresh.state()["running"] is False
        await refresh.stop()

    asyncio.run(go())
