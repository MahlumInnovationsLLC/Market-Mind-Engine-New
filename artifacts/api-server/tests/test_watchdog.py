"""Tests for the L2 watchdog rate-limiter and probe accounting."""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest


_WATCHDOG_PATH = Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
_spec = importlib.util.spec_from_file_location("axiom_watchdog", _WATCHDOG_PATH)
assert _spec and _spec.loader
watchdog = importlib.util.module_from_spec(_spec)
sys.modules["axiom_watchdog"] = watchdog
_spec.loader.exec_module(watchdog)  # type: ignore[attr-defined]


def _state(**overrides) -> "watchdog.WatchdogState":
    defaults = dict(
        target="http://localhost:1/healthz",
        threshold=3,
        interval=1.0,
        max_restarts=3,
        restart_window=60.0,
        backoff_initial=1.0,
        backoff_cap=30.0,
        supervise=True,
    )
    defaults.update(overrides)
    return watchdog.WatchdogState(**defaults)


def test_probe_accounting_counts_consecutive_failures() -> None:
    s = _state()
    for _ in range(2):
        s.record_probe(False, status=502, error="HTTP 502", latency_ms=4.2)
    assert s.consecutive_failures == 2
    assert s.consecutive_successes == 0
    assert s.total_failures == 2

    s.record_probe(True, status=200, error=None, latency_ms=1.0)
    assert s.consecutive_failures == 0
    assert s.consecutive_successes == 1
    assert s.total_probes == 3


def test_can_restart_enforces_window_budget() -> None:
    s = _state(max_restarts=2, restart_window=60.0, backoff_initial=0.0)
    ok, _ = s.can_restart()
    assert ok
    s.record_restart("trip 1")
    ok, _ = s.can_restart()
    assert ok
    s.record_restart("trip 2")
    ok, why = s.can_restart()
    assert not ok
    assert "budget exhausted" in (why or "")
    assert s.cooldown_until is not None


def test_can_restart_recovers_after_window_slides() -> None:
    s = _state(max_restarts=1, restart_window=0.05, backoff_initial=0.0)
    s.record_restart("trip 1")
    ok, _ = s.can_restart()
    assert not ok
    time.sleep(0.08)
    ok, _ = s.can_restart()
    assert ok, "window should slide forward and free the budget"


def test_backoff_grows_between_restarts() -> None:
    s = _state(max_restarts=10, backoff_initial=2.0, backoff_cap=100.0)
    s.record_restart("first")
    eligible_after_first = s.next_restart_eligible_at - s.last_restart_at
    s.record_restart("second")
    eligible_after_second = s.next_restart_eligible_at - s.last_restart_at
    assert eligible_after_second > eligible_after_first
    # Cap respected.
    for i in range(20):
        s.record_restart(f"trip {i}")
    eligible_capped = s.next_restart_eligible_at - s.last_restart_at
    assert eligible_capped <= 100.0 + 1e-6


def test_snapshot_includes_required_fields() -> None:
    s = _state()
    s.record_probe(True, status=200, error=None, latency_ms=2.0)
    snap = s.snapshot()
    for key in (
        "watchdog", "supervise", "target", "uptime_seconds",
        "consecutive_failures", "total_probes", "total_restarts",
        "restarts_in_window", "last_probe_at", "last_probe_ok",
        "next_restart_eligible_in",
    ):
        assert key in snap, f"missing snapshot key: {key}"


def test_probe_once_handles_unreachable_target() -> None:
    # Port 1 is reserved/closed in CI containers — connection refused.
    ok, status, err, latency = watchdog.probe_once(
        "http://127.0.0.1:1/healthz", timeout=0.5,
    )
    assert ok is False
    assert status is None
    assert err is not None
    assert latency >= 0.0


def test_observe_only_alert_uses_restart_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """In observe-only mode the watchdog still consumes the restart budget so
    we don't spam an alert every tick. The threshold breach should raise an
    alert, then suppress further alerts until cooldown."""
    s = _state(threshold=2, max_restarts=1, restart_window=60.0, backoff_initial=0.0, supervise=False)
    # Two failed probes -> threshold met, alert fires once and consumes budget.
    s.record_probe(False, status=None, error="EOF", latency_ms=1.0)
    s.record_probe(False, status=None, error="EOF", latency_ms=1.0)
    ok, _ = s.can_restart()
    assert ok
    s.record_restart("observe-only alert: synthetic")
    ok, why = s.can_restart()
    assert not ok
    assert why is not None
