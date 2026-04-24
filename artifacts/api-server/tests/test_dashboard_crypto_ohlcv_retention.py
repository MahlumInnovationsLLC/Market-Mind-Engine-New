"""Tests for ``dashboard.get_crypto_ohlcv_retention_status``.

The dashboard helper wraps ``app.crypto_ohlcv_retention.state()`` and
adds two derived fields the operator console relies on to flag a
misbehaving sweep:

* ``seconds_since_last_tick`` — None when the loop has never ticked.
* ``health`` — one of ``ok``, ``stale``, ``error``, ``disabled``.

These tests pin the rollup logic so a future refactor can't silently
turn a stale/errored loop green on the dashboard.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import crypto_ohlcv_retention as cor
from app import dashboard


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


def _run(coro):
    return asyncio.run(coro)


def test_status_includes_state_and_derived_fields() -> None:
    snap = _run(dashboard.get_crypto_ohlcv_retention_status())
    # All raw state() keys are passed through.
    for key in (
        "running",
        "enabled",
        "started_at",
        "last_tick_at",
        "last_error",
        "last_deleted",
        "total_deleted",
        "last_deleted_by_granularity",
        "retention_days",
        "interval_seconds",
    ):
        assert key in snap, f"missing key {key}"
    # Derived fields are always present.
    assert "seconds_since_last_tick" in snap
    assert "health" in snap


def test_health_disabled_when_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED", "0")
    snap = _run(dashboard.get_crypto_ohlcv_retention_status())
    assert snap["enabled"] is False
    assert snap["health"] == "disabled"


def test_health_error_when_last_error_set() -> None:
    cor._last_error = "RuntimeError: boom"
    cor._last_tick_at = time.time()  # fresh tick, but errored
    snap = _run(dashboard.get_crypto_ohlcv_retention_status())
    assert snap["health"] == "error"
    assert snap["last_error"] == "RuntimeError: boom"


def test_health_ok_when_recent_tick() -> None:
    cor._last_tick_at = time.time() - 5
    snap = _run(dashboard.get_crypto_ohlcv_retention_status())
    assert snap["health"] == "ok"
    assert snap["seconds_since_last_tick"] is not None
    assert 0 <= snap["seconds_since_last_tick"] < 60


def test_health_stale_when_last_tick_too_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 5-minute interval -> 10-minute grace window.
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_CLEANUP_INTERVAL_SECONDS", "300")
    cor._last_tick_at = time.time() - 3600  # an hour ago — well past 2x grace
    snap = _run(dashboard.get_crypto_ohlcv_retention_status())
    assert snap["health"] == "stale"
    assert snap["seconds_since_last_tick"] is not None
    assert snap["seconds_since_last_tick"] > 600


def test_health_stale_when_started_long_ago_but_never_ticked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_CLEANUP_INTERVAL_SECONDS", "300")
    cor._started_at = time.time() - 3600
    cor._last_tick_at = None
    snap = _run(dashboard.get_crypto_ohlcv_retention_status())
    assert snap["health"] == "stale"
    assert snap["seconds_since_last_tick"] is None


def test_health_ok_during_initial_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop that just started but hasn't ticked yet shouldn't flash
    stale immediately — give it a 2x interval grace window.
    """
    # Pretend the loop is alive so the "never started" branch doesn't
    # fire.  We can't actually spawn the asyncio task here without a
    # running loop, so just stub the bookkeeping.
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_CLEANUP_INTERVAL_SECONDS", "300")
    cor._started_at = time.time() - 1  # just started

    class _StubTask:
        def done(self) -> bool:  # noqa: D401
            return False

    cor._task = _StubTask()  # type: ignore[assignment]
    try:
        snap = _run(dashboard.get_crypto_ohlcv_retention_status())
        assert snap["health"] == "ok"
    finally:
        cor._task = None


def test_error_takes_precedence_over_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_CLEANUP_INTERVAL_SECONDS", "300")
    cor._last_tick_at = time.time() - 3600
    cor._last_error = "boom"
    snap = _run(dashboard.get_crypto_ohlcv_retention_status())
    assert snap["health"] == "error"


def test_disabled_takes_precedence_over_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_OHLCV_RETENTION_ENABLED", "0")
    cor._last_error = "boom"
    snap = _run(dashboard.get_crypto_ohlcv_retention_status())
    assert snap["health"] == "disabled"
