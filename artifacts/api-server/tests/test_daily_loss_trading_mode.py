"""Tests for the daily-loss circuit-breaker trading-mode scoping
(Task #752 step 2 — Round-11 audit Bug #6).

Pre-#752 the breaker summed ``trades.pnl`` and ``positions.unrealized_pnl``
across both paper and live ledgers, with two equally-bad failure modes:

1. **Paper-wins-mask-live-loss** — a green paper day silently offsets a
   real live drawdown, so the kill switch does not trip when it should.
2. **Paper-loss-trips-live-halt** — a red paper day trips the live kill
   switch on the basis of fake P&L, halting real-money trading on
   simulated noise.

Both directions are exercised here.  We patch ``_resolve_breaker_trading_mode``
to drive the mode the breaker scopes to, and patch ``db.fetchrow`` to
inspect the SQL parameters reaching the aggregate query — proving the
``trading_mode = $3`` filter is propagated end-to-end.

The cache key is also exercised: a paper snapshot followed immediately by
a live snapshot (no TTL elapsed) MUST refetch — otherwise the breaker
would keep returning the wrong ledger after a mode flip.
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import patch

import pytest

from app.risk import daily_loss as _dl


def _isoformat_today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# _fetch_snapshot_inputs — the SQL query carries trading_mode as $3.
# ---------------------------------------------------------------------------


def test_fetch_snapshot_inputs_passes_trading_mode_as_third_arg():
    """The aggregate query takes ``(today, tz_name, trading_mode)`` —
    pin the position so a future signature change is caught here."""
    captured: list[tuple] = []

    async def _spy(query, *args):
        captured.append((query, args))
        return {"realized": 0.0, "unrealized": 0.0, "sod_balance_cents": None}

    from app import db as _db

    with patch.object(_db, "fetchrow", _spy):
        asyncio.run(
            _dl._fetch_snapshot_inputs(date(2026, 5, 1), "UTC", "paper")
        )

    assert len(captured) == 1
    query, args = captured[0]
    assert args == (date(2026, 5, 1), "UTC", "paper")
    # The aggregate SCOPES every CTE by ``trading_mode = $3`` — if any
    # CTE is missing the predicate, this assertion catches it.
    assert query.count("trading_mode = $3") >= 4, (
        "expected the predicate on realized/unrealized/sod_primary/sod_fallback CTEs"
    )


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_fetch_snapshot_inputs_round_trip_for_each_mode(mode):
    """Each call passes only its own mode to the DB; no cross-mode
    bleed in the SQL parameters."""
    captured: list[tuple] = []

    async def _spy(query, *args):
        captured.append(args)
        return {"realized": 0.0, "unrealized": 0.0, "sod_balance_cents": None}

    from app import db as _db

    with patch.object(_db, "fetchrow", _spy):
        asyncio.run(_dl._fetch_snapshot_inputs(date(2026, 5, 1), "UTC", mode))

    assert captured == [(date(2026, 5, 1), "UTC", mode)]


# ---------------------------------------------------------------------------
# Direction 1: paper wins must NOT mask a live drawdown.
# ---------------------------------------------------------------------------


def test_live_breaker_ignores_paper_wins(monkeypatch):
    """Live mode sees only live trades.  A green paper P&L from earlier
    today MUST NOT offset the live drawdown.  We simulate this by
    returning different rows depending on the mode the SQL was scoped to.
    """
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "100")
    monkeypatch.delenv("AXIOM_DAILY_LOSS_HALT_PCT", raising=False)
    _dl.invalidate_cache()

    async def _fake_resolve(overrides):
        return "live"

    async def _fake_fetch(today, tz_name="UTC", trading_mode="live"):
        # Live ledger: -200 realized loss; paper ledger would show
        # +500 wins, but the live breaker must never see them.
        if trading_mode == "live":
            return -200.0, 0.0, 1000.0
        return 500.0, 0.0, 1000.0  # never reached when scoping is correct

    with patch.object(_dl, "_resolve_breaker_trading_mode", _fake_resolve), \
         patch.object(_dl, "_fetch_snapshot_inputs", _fake_fetch):
        snap = asyncio.run(_dl.compute_snapshot(None, use_cache=False))

    assert snap.trading_mode == "live"
    assert snap.realized_pnl_usd == -200.0
    assert snap.loss_usd == 200.0
    assert snap.tripped is True
    assert snap.tripped_abs is True


# ---------------------------------------------------------------------------
# Direction 2: paper losses must NOT trip the live kill switch.
# ---------------------------------------------------------------------------


def test_paper_breaker_ignores_live_state(monkeypatch):
    """Paper-mode operator: today's live P&L (if any) is irrelevant —
    only the paper ledger feeds the breaker.  A red paper day still
    trips the gate, but it never escalates the *live* trading mode
    (the halt persistence + alerting is shared, but the snapshot we
    return is paper-scoped, which the operator UI surfaces clearly).
    """
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "50")
    monkeypatch.delenv("AXIOM_DAILY_LOSS_HALT_PCT", raising=False)
    _dl.invalidate_cache()

    async def _fake_resolve(overrides):
        return "paper"

    async def _fake_fetch(today, tz_name="UTC", trading_mode="live"):
        if trading_mode == "paper":
            return -75.0, 0.0, 500.0
        # If anything *ever* asks for live, we'd return a positive P&L
        # — and the test below would fail because the breaker would
        # not trip.
        return 1000.0, 0.0, 500.0

    with patch.object(_dl, "_resolve_breaker_trading_mode", _fake_resolve), \
         patch.object(_dl, "_fetch_snapshot_inputs", _fake_fetch):
        snap = asyncio.run(_dl.compute_snapshot(None, use_cache=False))

    assert snap.trading_mode == "paper"
    assert snap.loss_usd == 75.0
    assert snap.tripped is True


# ---------------------------------------------------------------------------
# Cache key includes trading_mode — paper↔live flip MUST refetch.
# ---------------------------------------------------------------------------


def test_cache_keys_on_trading_mode(monkeypatch):
    """A cached paper snapshot must NOT be returned when the operator
    has flipped to live.  Pre-#752 the cache key was
    ``(date_local, timezone)``, which would silently serve the wrong
    ledger for up to ``CACHE_TTL_SECONDS`` after a flip — long enough
    for several gate evaluations to make the wrong call.
    """
    monkeypatch.setenv("AXIOM_DAILY_LOSS_HALT_USD", "1000")
    monkeypatch.delenv("AXIOM_DAILY_LOSS_HALT_PCT", raising=False)
    _dl.invalidate_cache()

    fetch_log: list[str] = []
    current_mode = {"v": "paper"}

    async def _fake_resolve(overrides):
        return current_mode["v"]

    async def _fake_fetch(today, tz_name="UTC", trading_mode="live"):
        fetch_log.append(trading_mode)
        # Paper has -10 P&L, live has -20 — the test asserts on which
        # one comes back, not on tripping.
        if trading_mode == "paper":
            return -10.0, 0.0, 100.0
        return -20.0, 0.0, 100.0

    with patch.object(_dl, "_resolve_breaker_trading_mode", _fake_resolve), \
         patch.object(_dl, "_fetch_snapshot_inputs", _fake_fetch):
        snap_paper = asyncio.run(_dl.compute_snapshot(None))
        # Same call, same mode → cache hit, no second fetch.
        snap_paper_cached = asyncio.run(_dl.compute_snapshot(None))
        # Operator flips to live; the *very next* call must refetch.
        current_mode["v"] = "live"
        snap_live = asyncio.run(_dl.compute_snapshot(None))

    assert snap_paper.trading_mode == "paper"
    assert snap_paper.realized_pnl_usd == -10.0
    assert snap_paper_cached is snap_paper, "expected the cached paper snapshot"
    assert snap_live.trading_mode == "live"
    assert snap_live.realized_pnl_usd == -20.0
    assert fetch_log == ["paper", "live"], (
        f"expected exactly one fetch per mode (no cache cross-talk), got {fetch_log}"
    )

    _dl.invalidate_cache()


# ---------------------------------------------------------------------------
# Resolver: operator override > resolved current mode > "live" fallback.
# ---------------------------------------------------------------------------


def test_resolver_honours_overrides_trading_mode():
    """Test harness can pin the breaker mode without touching the DB
    by passing ``overrides={"trading_mode": ...}``."""
    assert (
        asyncio.run(_dl._resolve_breaker_trading_mode({"trading_mode": "paper"}))
        == "paper"
    )
    assert (
        asyncio.run(_dl._resolve_breaker_trading_mode({"trading_mode": "LIVE"}))
        == "live"
    )


def test_resolver_falls_back_to_live_for_halt_or_unknown(monkeypatch):
    """``halt`` (and any other unrecognised mode) → ``"live"`` so the
    breaker still aggregates the right ledger while the system is
    paused.  A halt almost always follows a live drawdown."""
    async def _fake_resolve(env_default=None):
        return "halt"

    import app.risk.trading_mode as _tm

    monkeypatch.setattr(_tm, "resolve_trading_mode", _fake_resolve)
    assert asyncio.run(_dl._resolve_breaker_trading_mode(None)) == "live"


def test_resolver_swallows_db_errors_with_live_default(monkeypatch):
    """A flaky DB on the resolver path MUST NOT block the breaker —
    the snapshot-cache TTL absorbs a brief outage; defaulting to
    ``live`` is the safer path because that's the ledger with real
    money on the line."""
    async def _boom(env_default=None):
        raise RuntimeError("connection refused")

    import app.risk.trading_mode as _tm

    monkeypatch.setattr(_tm, "resolve_trading_mode", _boom)
    assert asyncio.run(_dl._resolve_breaker_trading_mode(None)) == "live"
