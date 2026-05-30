"""Tests for the settlement scheduler's staggered heavy-maintenance gates.

The settlement loop settles trades every tick, but the two heaviest
maintenance steps — the online-retrain buffer drain + isotonic refit and
the per-league margin-regressor refit — are staggered onto their own
(longer, env-tunable) cadence so they don't run on every tick and starve
the main app port (which previously triggered platform health-check
restarts / recurring outages).

These tests cover the pure cadence helpers and the per-instance "is it
due yet?" gates, without driving the infinite ``_loop``.
"""
from __future__ import annotations

import importlib

import pytest

# ``app.settlement`` re-exports the ``scheduler`` *instance*, which shadows
# the submodule attribute on the package — so a plain ``import ... as`` would
# bind to the instance.  Pull the real module out of ``sys.modules`` instead.
_sched = importlib.import_module("app.settlement.scheduler")


# ---------------------------------------------------------------------------
# Interval resolution from the environment
# ---------------------------------------------------------------------------


def test_retrain_interval_defaults_to_15_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AXIOM_RETRAIN_INTERVAL_SECONDS", raising=False)
    assert _sched._retrain_interval_seconds() == 900.0


def test_margin_refit_interval_defaults_to_15_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AXIOM_MARGIN_REFIT_INTERVAL_SECONDS", raising=False)
    assert _sched._margin_refit_interval_seconds() == 900.0


def test_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_RETRAIN_INTERVAL_SECONDS", "120")
    assert _sched._retrain_interval_seconds() == 120.0
    monkeypatch.setenv("AXIOM_MARGIN_REFIT_INTERVAL_SECONDS", "300")
    assert _sched._margin_refit_interval_seconds() == 300.0


def test_interval_zero_means_every_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """``0`` is the documented opt-out: revert to running every tick."""
    monkeypatch.setenv("AXIOM_RETRAIN_INTERVAL_SECONDS", "0")
    assert _sched._retrain_interval_seconds() == 0.0


def test_interval_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_RETRAIN_INTERVAL_SECONDS", "not-a-number")
    assert _sched._retrain_interval_seconds() == 900.0


def test_interval_negative_is_clamped_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_MARGIN_REFIT_INTERVAL_SECONDS", "-50")
    assert _sched._margin_refit_interval_seconds() == 0.0


# ---------------------------------------------------------------------------
# Per-instance cadence gates
# ---------------------------------------------------------------------------


def test_fresh_scheduler_runs_both_on_first_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold start (``_last_*_at == 0.0``) must warm both steps once."""
    monkeypatch.setenv("AXIOM_RETRAIN_INTERVAL_SECONDS", "900")
    monkeypatch.setenv("AXIOM_MARGIN_REFIT_INTERVAL_SECONDS", "900")
    s = _sched._SettlementScheduler()
    # A realistic monotonic "now" well above the interval.
    now = 10_000.0
    assert s._should_run_retrain(now) is True
    assert s._should_run_margin_refit(now) is True


def test_cold_host_warms_even_when_uptime_below_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A freshly recreated VM has a low ``monotonic()`` (host uptime).

    The first tick must still warm both steps even though
    ``now_mono < interval`` — the ``_last_*_at == 0.0`` sentinel guarantees
    this regardless of how recently the host booted.
    """
    monkeypatch.setenv("AXIOM_RETRAIN_INTERVAL_SECONDS", "900")
    monkeypatch.setenv("AXIOM_MARGIN_REFIT_INTERVAL_SECONDS", "900")
    s = _sched._SettlementScheduler()
    now = 12.0  # only 12s of host uptime, far below the 900s interval
    assert s._should_run_retrain(now) is True
    assert s._should_run_margin_refit(now) is True


def test_gate_blocks_within_interval_and_reopens_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_RETRAIN_INTERVAL_SECONDS", "900")
    s = _sched._SettlementScheduler()
    now = 10_000.0
    # Simulate having just run a retrain.
    s._last_retrain_at = now
    # A settlement tick a few minutes later is still inside the window.
    assert s._should_run_retrain(now + 180.0) is False
    assert s._should_run_retrain(now + 899.0) is False
    # Once the full interval elapses, the gate reopens.
    assert s._should_run_retrain(now + 900.0) is True
    assert s._should_run_retrain(now + 1_000.0) is True


def test_margin_gate_independent_of_retrain_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_RETRAIN_INTERVAL_SECONDS", "900")
    monkeypatch.setenv("AXIOM_MARGIN_REFIT_INTERVAL_SECONDS", "900")
    s = _sched._SettlementScheduler()
    now = 10_000.0
    s._last_retrain_at = now  # retrain just ran
    # Margin refit hasn't run (still 0.0) so it remains due even though
    # retrain is now blocked — the two gates are independent.
    assert s._should_run_retrain(now + 10.0) is False
    assert s._should_run_margin_refit(now + 10.0) is True


def test_zero_interval_keeps_gate_open_every_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_RETRAIN_INTERVAL_SECONDS", "0")
    s = _sched._SettlementScheduler()
    now = 10_000.0
    s._last_retrain_at = now
    # With a zero interval, even the same instant is "due".
    assert s._should_run_retrain(now) is True
