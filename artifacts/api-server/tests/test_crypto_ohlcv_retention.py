"""Tests for the `crypto_ohlcv` bar-table retention sweep (task #175).

These verify:
  * `cleanup_once` issues an age-bounded DELETE per granularity and
    counts the rows the DB reports back.
  * Per-granularity env vars override the retention windows, with
    safety floors so a typo can't immediately wipe the table.
  * `start`/`stop` spin a single background task that calls
    `cleanup_once` at least once and shuts down cleanly.
  * `AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED=0` short-circuits start.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import crypto_ohlcv_retention as cor
from app import db as _db


_ENV_KEYS = (
    "AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_1M",
    "AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_5M",
    "AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_15M",
    "AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_1H",
    "AXIOM_CRYPTO_OHLCV_CLEANUP_INTERVAL_SECONDS",
    "AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED",
)


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    cor._reset_state_for_tests()
    yield
    cor._reset_state_for_tests()


def test_defaults_match_spec() -> None:
    assert cor._retention_days("1m") == 180
    assert cor._retention_days("5m") == 365
    assert cor._retention_days("15m") == 730
    assert cor._retention_days("1h") == 1825
    assert cor._interval_seconds() == float(cor.DEFAULT_INTERVAL_SECONDS)


def test_retention_days_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_1M", "0")
    assert cor._retention_days("1m") == 1
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_1M", "-7")
    assert cor._retention_days("1m") == 1


def test_retention_days_handles_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_1H", "not-a-number")
    assert cor._retention_days("1h") == 1825


def test_retention_days_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_1M", "30")
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_RETENTION_DAYS_5M", "60")
    windows = cor.retention_windows()
    assert windows["1m"] == 30
    assert windows["5m"] == 60
    assert windows["15m"] == 730
    assert windows["1h"] == 1825


def test_interval_seconds_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_CLEANUP_INTERVAL_SECONDS", "10")
    # 5-minute floor so a misconfigured deploy can't hammer the DB.
    assert cor._interval_seconds() == 300.0


def test_parse_delete_status() -> None:
    assert cor._parse_delete_status("DELETE 0") == 0
    assert cor._parse_delete_status("DELETE 42") == 42
    assert cor._parse_delete_status("") == 0
    assert cor._parse_delete_status("UPDATE 3") == 0


def test_cleanup_once_sweeps_every_granularity(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, tuple[Any, ...]]] = []
    counts = {"1m": 4, "5m": 3, "15m": 2, "1h": 1}

    async def fake_execute(query: str, *args: Any) -> str:
        captured.append((query, args))
        gran = args[0]
        return f"DELETE {counts[gran]}"

    monkeypatch.setattr(_db, "execute", fake_execute)

    total = asyncio.run(cor.cleanup_once())

    assert total == sum(counts.values())
    assert len(captured) == 4
    for query, args in captured:
        assert "DELETE FROM crypto_ohlcv" in query
        assert "granularity = $1" in query
        assert "ts <" in query
        assert "INTERVAL '1 day'" in query
        gran, days = args
        assert gran in counts
        assert isinstance(days, int) and days >= 1
    s = cor.state()
    assert s["last_deleted"] == sum(counts.values())
    assert s["total_deleted"] == sum(counts.values())
    assert s["last_deleted_by_granularity"] == counts
    assert s["last_error"] is None


def test_cleanup_once_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_execute(query: str, *args: Any) -> str:
        if args[0] == "5m":
            raise RuntimeError("connection reset")
        return "DELETE 1"

    monkeypatch.setattr(_db, "execute", fake_execute)

    total = asyncio.run(cor.cleanup_once())
    # Three of four granularities succeed with 1 row each; failing one
    # contributes 0 but doesn't kill the sweep.
    assert total == 3
    s = cor.state()
    assert "connection reset" in (s["last_error"] or "")
    assert s["last_deleted_by_granularity"]["5m"] == 0


def test_total_deleted_accumulates(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"n": 0}

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        state["n"] += 1
        # 1 row per granularity per sweep — 4 per call.
        return "DELETE 1"

    monkeypatch.setattr(_db, "execute", fake_execute)
    asyncio.run(cor.cleanup_once())
    asyncio.run(cor.cleanup_once())
    s = cor.state()
    assert s["total_deleted"] == 8
    assert s["last_deleted"] == 4


def test_start_runs_loop_and_stop_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        calls.append(1)
        return "DELETE 0"

    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_CLEANUP_INTERVAL_SECONDS", "300")

    async def go() -> None:
        await cor.start()
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls, "expected cleanup_once to have run at least once"
        s = cor.state()
        assert s["running"] is True
        await cor.stop()
        assert cor.state()["running"] is False

    asyncio.run(go())


def test_start_respects_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED", "0")

    async def boom(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("DB must not be touched when disabled")

    monkeypatch.setattr(_db, "execute", boom)

    async def go() -> None:
        await cor.start()
        assert cor.state()["running"] is False
        await cor.stop()

    asyncio.run(go())
