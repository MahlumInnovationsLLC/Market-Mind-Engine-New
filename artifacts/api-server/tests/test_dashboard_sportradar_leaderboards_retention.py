"""Tests for ``dashboard.get_sportradar_leaderboards_retention_status``.

Mirrors the crypto-backtest variant in
``test_dashboard_crypto_backtest_retention`` to pin the rollup logic
the operator console relies on for the
``sportradar_tournament_leaderboards`` history-table GC loop:

* ``seconds_since_last_tick`` — None when the loop has never ticked.
* ``health`` — one of ``ok``, ``stale``, ``error``, ``disabled``.

These tests guard against a future refactor silently turning a
stale/errored loop green on the dashboard.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import dashboard
from app import sportradar_leaderboards_retention as srlr


_ENV_KEYS = (
    "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS",
    "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS",
    "AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS",
    "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED",
)


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    srlr._reset_state_for_tests()
    yield
    srlr._reset_state_for_tests()


def _run(coro):
    return asyncio.run(coro)


def test_status_includes_state_and_derived_fields() -> None:
    snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
    for key in (
        "running",
        "enabled",
        "started_at",
        "last_tick_at",
        "last_error",
        "last_deleted",
        "total_deleted",
        "policy",
        "interval_seconds",
    ):
        assert key in snap, f"missing key {key}"
    assert "min_snapshots" in snap["policy"]
    assert "retention_days" in snap["policy"]
    assert "seconds_since_last_tick" in snap
    assert "health" in snap


def test_health_disabled_when_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED", "0"
    )
    snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
    assert snap["enabled"] is False
    assert snap["health"] == "disabled"


def test_health_error_when_last_error_set() -> None:
    srlr._last_error = "RuntimeError: boom"
    srlr._last_tick_at = time.time()  # fresh tick, but errored
    snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
    assert snap["health"] == "error"
    assert snap["last_error"] == "RuntimeError: boom"


def test_health_ok_when_recent_tick() -> None:
    srlr._last_tick_at = time.time() - 5
    snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
    assert snap["health"] == "ok"
    assert snap["seconds_since_last_tick"] is not None
    assert 0 <= snap["seconds_since_last_tick"] < 60


def test_health_stale_when_last_tick_too_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 5-minute interval -> 10-minute grace window.
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS", "300"
    )
    srlr._last_tick_at = time.time() - 3600  # an hour ago — past 2x grace
    snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
    assert snap["health"] == "stale"
    assert snap["seconds_since_last_tick"] is not None
    assert snap["seconds_since_last_tick"] > 600


def test_health_stale_when_started_long_ago_but_never_ticked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS", "300"
    )
    srlr._started_at = time.time() - 3600
    srlr._last_tick_at = None
    snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
    assert snap["health"] == "stale"
    assert snap["seconds_since_last_tick"] is None


def test_health_ok_during_initial_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop that just started but hasn't ticked yet shouldn't flash
    stale immediately — give it a 2x interval grace window.
    """
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS", "300"
    )
    srlr._started_at = time.time() - 1  # just started

    class _StubTask:
        def done(self) -> bool:  # noqa: D401
            return False

    srlr._task = _StubTask()  # type: ignore[assignment]
    try:
        snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
        assert snap["health"] == "ok"
    finally:
        srlr._task = None


def test_error_takes_precedence_over_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS", "300"
    )
    srlr._last_tick_at = time.time() - 3600
    srlr._last_error = "boom"
    snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
    assert snap["health"] == "error"


def test_disabled_takes_precedence_over_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED", "0"
    )
    srlr._last_error = "boom"
    snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
    assert snap["health"] == "disabled"


def test_policy_reflects_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS", "12"
    )
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS", "30"
    )
    snap = _run(dashboard.get_sportradar_leaderboards_retention_status())
    assert snap["policy"]["min_snapshots"] == 12
    assert snap["policy"]["retention_days"] == 30
