"""Tests for the cycle-start stale-resting-order sweep (task #270).

The orchestrator cancels resting orders older than
``AXIOM_RESTING_ORDER_MAX_AGE_SECS`` (with per-vertical override support)
before evaluating new candidates. Cancel failures must be swallowed so a
flaky venue cannot park the orchestrator, and the ``stale_resting_cancelled``
counter must surface on ``/api/health/snapshot``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.kalshi import lifecycle as kalshi_lifecycle
from app.orchestrator import cycle as cycle_mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    kalshi_lifecycle._reset_stale_resting_state()
    monkeypatch.delenv("AXIOM_RESTING_ORDER_MAX_AGE_SECS", raising=False)
    yield
    kalshi_lifecycle._reset_stale_resting_state()


def _make_record(order_id: str, age_secs: float, vertical: str = "sports"):
    req = SimpleNamespace(
        vertical=vertical,
        limit_price_cents=50,
    )
    return SimpleNamespace(
        order_id=order_id,
        request=req,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_secs),
    )


def _install_resting(monkeypatch, records, cancel_side_effect=None):
    """Monkeypatch list_resting_orders + cancel_order to use `records`."""
    cancelled: list[str] = []
    store = {r.order_id: r for r in records}

    async def _fake_list():
        return list(store.values())

    async def _fake_cancel(order_id: str):
        if cancel_side_effect is not None:
            effect = cancel_side_effect(order_id)
            if isinstance(effect, Exception):
                raise effect
        store.pop(order_id, None)
        cancelled.append(order_id)
        return {"order_id": order_id, "state": "canceled"}

    monkeypatch.setattr(kalshi_lifecycle, "list_resting_orders", _fake_list)
    monkeypatch.setattr(kalshi_lifecycle, "cancel_order", _fake_cancel)
    return cancelled


# ---------------------------------------------------------------------------
# _resolve_resting_max_age precedence
# ---------------------------------------------------------------------------

def test_resolve_age_default(monkeypatch):
    monkeypatch.delenv("AXIOM_RESTING_ORDER_MAX_AGE_SECS", raising=False)
    assert cycle_mod._resolve_resting_max_age("sports", {}) == float(
        cycle_mod.DEFAULT_RESTING_ORDER_MAX_AGE_SECS
    )


def test_resolve_age_env(monkeypatch):
    monkeypatch.setenv("AXIOM_RESTING_ORDER_MAX_AGE_SECS", "90")
    assert cycle_mod._resolve_resting_max_age("sports", {}) == 90.0


def test_resolve_age_global_override_beats_env(monkeypatch):
    monkeypatch.setenv("AXIOM_RESTING_ORDER_MAX_AGE_SECS", "90")
    ov = {"resting_order_max_age_secs": 45}
    assert cycle_mod._resolve_resting_max_age("sports", ov) == 45.0


def test_resolve_age_per_vertical_override_beats_global():
    ov = {
        "resting_order_max_age_secs": 45,
        "resting_order_max_age_secs.sports": 300,
    }
    assert cycle_mod._resolve_resting_max_age("sports", ov) == 300.0
    # Other verticals still see the global value.
    assert cycle_mod._resolve_resting_max_age("crypto", ov) == 45.0


# ---------------------------------------------------------------------------
# Sweep behavior
# ---------------------------------------------------------------------------

def test_sweep_clean_cycle(monkeypatch):
    """No resting orders -> sweep is a no-op but still counts the scan."""
    cancelled = _install_resting(monkeypatch, [])
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", {}))
    assert stats == {"scanned": 0, "cancelled": 0, "failed": 0, "max_age": 30}
    assert cancelled == []
    state = kalshi_lifecycle.stale_resting_cancelled_state()
    assert state["scans"] == 1
    assert state["cancelled"] == 0


def test_sweep_cancels_stale_keeps_fresh(monkeypatch):
    """One stale, one fresh -> only the stale order is cancelled."""
    stale = _make_record("stale-1", age_secs=120)
    fresh = _make_record("fresh-1", age_secs=5)
    cancelled = _install_resting(monkeypatch, [stale, fresh])
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", {}))
    assert cancelled == ["stale-1"]
    assert stats["scanned"] == 2
    assert stats["cancelled"] == 1
    assert stats["failed"] == 0
    state = kalshi_lifecycle.stale_resting_cancelled_state()
    assert state["cancelled"] == 1
    assert state["cancel_failed"] == 0


def test_sweep_swallows_cancel_failure(monkeypatch):
    """A flaky venue raises on cancel -> counter records failure, cycle continues."""
    stale_a = _make_record("flaky-a", age_secs=120)
    stale_b = _make_record("ok-b", age_secs=120)

    def _effect(order_id: str):
        if order_id == "flaky-a":
            return RuntimeError("HTTP 503 from venue")
        return None

    cancelled = _install_resting(monkeypatch, [stale_a, stale_b], cancel_side_effect=_effect)
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", {}))
    # ok-b still got cancelled — flaky-a's failure must not short-circuit the sweep.
    assert cancelled == ["ok-b"]
    assert stats["scanned"] == 2
    assert stats["cancelled"] == 1
    assert stats["failed"] == 1
    state = kalshi_lifecycle.stale_resting_cancelled_state()
    assert state["cancelled"] == 1
    assert state["cancel_failed"] == 1
    assert "flaky-a" in (state["last_error"] or "")


def test_sweep_respects_per_vertical_override(monkeypatch):
    """Per-vertical TTL override keeps older orders resting in that vertical."""
    old = _make_record("old-sports", age_secs=180)  # 3 minutes old
    cancelled = _install_resting(monkeypatch, [old])
    # Per-vertical override: sports orders may live up to 10 minutes.
    overrides = {"resting_order_max_age_secs.sports": 600}
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", overrides))
    assert cancelled == []
    assert stats["scanned"] == 1
    assert stats["cancelled"] == 0
    assert stats["max_age"] == 600


def test_sweep_only_touches_matching_vertical(monkeypatch):
    """Orders belonging to another vertical are skipped by this vertical's sweep."""
    sports_stale = _make_record("sports-stale", age_secs=120, vertical="sports")
    crypto_stale = _make_record("crypto-stale", age_secs=120, vertical="crypto")
    cancelled = _install_resting(monkeypatch, [sports_stale, crypto_stale])
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", {}))
    assert cancelled == ["sports-stale"]
    assert stats["scanned"] == 1  # crypto order was filtered out before counting


def test_sweep_disabled_when_max_age_zero(monkeypatch):
    """max_age <= 0 disables the sweep (opt-out)."""
    stale = _make_record("stale", age_secs=9999)
    cancelled = _install_resting(monkeypatch, [stale])
    stats = _run(cycle_mod._cancel_stale_resting_orders(
        "sports", {"resting_order_max_age_secs": 0}
    ))
    assert cancelled == []
    assert stats["cancelled"] == 0
    # Scan still recorded so operators see the sweep ran.
    assert kalshi_lifecycle.stale_resting_cancelled_state()["scans"] == 1


# ---------------------------------------------------------------------------
# Sweep runs even when the cycle is halted
# ---------------------------------------------------------------------------

def test_sweep_runs_before_halt_early_return(monkeypatch):
    """When trading_mode=halt the cycle skips placement, but existing
    resting orders must still be cancelled — otherwise halting the loop
    leaves stale orders live against an arbitrarily old thesis."""
    from app.orchestrator import cycle as cycle_mod_local
    from app import db as db_mod

    stale = _make_record("halt-stale", age_secs=120)
    cancelled = _install_resting(monkeypatch, [stale])

    # Stub every DB path the halted branch touches — most call sites
    # already wrap DB access in try/except so silent failures are safe,
    # but we want the test to exercise the real control flow.
    async def _noop_fetch(*a, **kw): return []
    async def _noop_fetchval(*a, **kw): return None
    async def _noop_execute(*a, **kw): return None
    monkeypatch.setattr(db_mod, "fetch", _noop_fetch)
    monkeypatch.setattr(db_mod, "fetchval", _noop_fetchval)
    monkeypatch.setattr(db_mod, "execute", _noop_execute)

    # trading_mode=halt forces the early return — the sweep must still run.
    settings = SimpleNamespace(
        trading_mode="halt",
        cycle_interval_seconds=60,
        max_fresh_debates_per_cycle=0,
        openai_api_key=None,
        anthropic_api_key=None,
    )
    monkeypatch.setattr(cycle_mod_local, "get_settings", lambda: settings)

    async def _noop_get_agg(): return "balanced"
    monkeypatch.setattr(cycle_mod_local, "get_aggression_mode", _noop_get_agg)

    monkeypatch.setattr(
        cycle_mod_local.predictor, "active_model_info",
        lambda: {"model_name": "test"},
    )

    result = _run(cycle_mod_local.run_cycle("sports"))
    assert any("trading_mode=halt" in e for e in result.errors), result.errors
    # The sweep must have cancelled the stale order before the halt return.
    assert cancelled == ["halt-stale"]
    state = kalshi_lifecycle.stale_resting_cancelled_state()
    assert state["scans"] == 1
    assert state["cancelled"] == 1


# ---------------------------------------------------------------------------
# Health snapshot exposure
# ---------------------------------------------------------------------------

def test_health_snapshot_exposes_counter():
    from app import health as health_mod
    kalshi_lifecycle._reset_stale_resting_state()
    kalshi_lifecycle.record_stale_resting_scan()
    kalshi_lifecycle.record_stale_resting_cancelled("abc")
    card = health_mod._probe_stale_resting_cancelled()
    assert card["key"] == "stale_resting_cancelled"
    assert card["category"] == "passive"
    assert card["status"] == "ok"
    assert card["extras"]["stale_resting_cancelled"] == 1
    assert card["extras"]["cancelled"] == 1


def test_health_snapshot_degrades_on_failure():
    from app import health as health_mod
    kalshi_lifecycle._reset_stale_resting_state()
    kalshi_lifecycle.record_stale_resting_cancel_failed("xyz", "HTTP 503")
    card = health_mod._probe_stale_resting_cancelled()
    assert card["status"] == "degraded"
    assert "HTTP 503" in card["detail"]
