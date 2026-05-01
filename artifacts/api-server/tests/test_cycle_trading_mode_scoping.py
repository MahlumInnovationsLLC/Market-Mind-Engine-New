"""Tests for the cycle-helper trading_mode scoping (Task #752 step 1
follow-up — Round-11 audit, second pass).

The first cut of #752 only scoped the **dashboard read endpoints** by
``trading_mode``.  An architect review caught that three internal
helpers in :mod:`app.orchestrator.cycle` were still reading the union
of paper+live rows, so paper bankroll deltas / open positions /
intraday trade churn could:

* Trip a live cycle's bankroll-floor gate (false positive)
* Inflate a live cycle's per-league exposure cap (false positive)
* Trip a live cycle's daily-trade cap (false positive)

…and inverse for paper.  These tests lock in that the helpers thread
the requested ``trading_mode`` into their SQL ``WHERE`` clauses with
the right bind parameters, default safely to live for legacy callers,
and return mode-isolated results from a stub DB seeded with both
ledgers.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.orchestrator import cycle as cycle_mod


# ---------------------------------------------------------------------------
# _latest_bankroll
# ---------------------------------------------------------------------------


def test_latest_bankroll_binds_live_by_default(monkeypatch: pytest.MonkeyPatch):
    """Default arg is "live" — preserves the legacy single-ledger
    semantics for any caller that hasn't been threaded yet."""
    bound: list[tuple[str, ...]] = []

    async def _fetchval(query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT balance_cents FROM bankroll_events"):
            assert "trading_mode = $1" in q
            bound.append(args)
            return 12_300  # $123.00
        return None

    monkeypatch.setattr(cycle_mod.db, "fetchval", _fetchval)
    out = asyncio.run(cycle_mod._latest_bankroll())
    assert out == pytest.approx(123.0)
    assert bound == [("live",)]


def test_latest_bankroll_binds_paper_when_requested(monkeypatch: pytest.MonkeyPatch):
    bound: list[tuple[str, ...]] = []

    async def _fetchval(query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT balance_cents FROM bankroll_events"):
            bound.append(args)
            return 99_900  # $999.00
        return None

    monkeypatch.setattr(cycle_mod.db, "fetchval", _fetchval)
    out = asyncio.run(cycle_mod._latest_bankroll(trading_mode="paper"))
    assert out == pytest.approx(999.0)
    assert bound == [("paper",)]


def test_latest_bankroll_isolates_modes(monkeypatch: pytest.MonkeyPatch):
    """Two calls with different modes get different numbers — proves
    the WHERE filter is honoured by the route, not just bound."""
    rows = {"live": 50_000, "paper": 100_000_000}

    async def _fetchval(query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT balance_cents FROM bankroll_events"):
            return rows[args[0]]
        return None

    monkeypatch.setattr(cycle_mod.db, "fetchval", _fetchval)
    live = asyncio.run(cycle_mod._latest_bankroll(trading_mode="live"))
    paper = asyncio.run(cycle_mod._latest_bankroll(trading_mode="paper"))
    assert live == pytest.approx(500.0)
    assert paper == pytest.approx(1_000_000.0)
    # Sanity: huge gap proves the filter is in effect — the legacy
    # union would have averaged or clobbered.
    assert paper > live * 100


def test_latest_bankroll_paper_skips_legacy_snapshot_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    """``bankroll_snapshots`` is a live-only legacy table.  When paper
    mode finds no events, returning the live snapshot would re-create
    the exact bug we're fixing — so paper short-circuits to None
    instead of hitting the fallback."""
    calls: list[str] = []

    async def _fetchval(query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT balance_cents FROM bankroll_events"):
            calls.append("events")
            return None
        if q.startswith("SELECT balance::float FROM bankroll_snapshots"):
            calls.append("snapshots")
            return 9_999.0
        return None

    monkeypatch.setattr(cycle_mod.db, "fetchval", _fetchval)
    out = asyncio.run(cycle_mod._latest_bankroll(trading_mode="paper"))
    assert out is None
    # Paper must NOT fall back to the live snapshot.
    assert calls == ["events"]


def test_latest_bankroll_live_uses_legacy_snapshot_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    """Inverse: the live path keeps the legacy fallback so first-boot
    behaviour (events table empty before sync ticks once) doesn't
    regress.  This locks down the asymmetry."""
    calls: list[str] = []

    async def _fetchval(query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT balance_cents FROM bankroll_events"):
            calls.append("events")
            return None
        if q.startswith("SELECT balance::float FROM bankroll_snapshots"):
            calls.append("snapshots")
            return 421.50
        return None

    monkeypatch.setattr(cycle_mod.db, "fetchval", _fetchval)
    out = asyncio.run(cycle_mod._latest_bankroll(trading_mode="live"))
    assert out == pytest.approx(421.50)
    assert calls == ["events", "snapshots"]


def test_latest_bankroll_unknown_mode_defaults_to_live(monkeypatch: pytest.MonkeyPatch):
    """Defence in depth: garbage mode is coerced to live, not silently
    dropped or unioned."""
    bound: list[tuple[str, ...]] = []

    async def _fetchval(query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT balance_cents FROM bankroll_events"):
            bound.append(args)
            return 1
        return None

    monkeypatch.setattr(cycle_mod.db, "fetchval", _fetchval)
    asyncio.run(cycle_mod._latest_bankroll(trading_mode="halt"))
    asyncio.run(cycle_mod._latest_bankroll(trading_mode=""))
    asyncio.run(cycle_mod._latest_bankroll(trading_mode="bogus"))
    assert bound == [("live",), ("live",), ("live",)]


# ---------------------------------------------------------------------------
# _open_position_aggregates
# ---------------------------------------------------------------------------


def test_open_position_aggregates_binds_trading_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    bound: list[tuple[str, ...]] = []

    async def _fetch(query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM positions p" in q and "GROUP BY p.vertical" in q:
            assert "p.trading_mode = $1" in q
            bound.append(args)
            return [
                {"vertical": "sports", "n": 3, "notional": 300.0},
            ]
        if "FROM positions p" in q and "LEFT JOIN swarm_debates" in q:
            assert "p.trading_mode = $1" in q
            return []
        return []

    monkeypatch.setattr(cycle_mod.db, "fetch", _fetch)

    n, notional, per_v, *_ = asyncio.run(
        cycle_mod._open_position_aggregates(trading_mode="live")
    )
    assert n == 3
    assert notional == 300.0
    assert per_v == {"sports": 300.0}
    assert ("live",) in bound


def test_open_position_aggregates_isolates_modes(
    monkeypatch: pytest.MonkeyPatch,
):
    """A live caller must not see paper-mode positions and vice versa."""
    seeded = {
        "live": [
            {"vertical": "sports", "n": 1, "notional": 100.0},
        ],
        "paper": [
            {"vertical": "sports", "n": 50, "notional": 5_000.0},
        ],
    }

    async def _fetch(query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM positions p" in q and "GROUP BY p.vertical" in q:
            return seeded[args[0]]
        return []

    monkeypatch.setattr(cycle_mod.db, "fetch", _fetch)

    live_n, live_notional, *_ = asyncio.run(
        cycle_mod._open_position_aggregates(trading_mode="live")
    )
    paper_n, paper_notional, *_ = asyncio.run(
        cycle_mod._open_position_aggregates(trading_mode="paper")
    )
    assert (live_n, live_notional) == (1, 100.0)
    assert (paper_n, paper_notional) == (50, 5_000.0)


def test_open_position_aggregates_unknown_mode_defaults_to_live(
    monkeypatch: pytest.MonkeyPatch,
):
    bound: list[tuple[str, ...]] = []

    async def _fetch(query: str, *args: Any):
        if "FROM positions p" in query and "GROUP BY p.vertical" in query:
            bound.append(args)
            return []
        return []

    monkeypatch.setattr(cycle_mod.db, "fetch", _fetch)
    asyncio.run(cycle_mod._open_position_aggregates(trading_mode="halt"))
    assert bound[0] == ("live",)


# ---------------------------------------------------------------------------
# _today_trade_counts_per_league
# ---------------------------------------------------------------------------


def test_today_trade_counts_binds_trading_mode(monkeypatch: pytest.MonkeyPatch):
    bound: list[tuple[str, ...]] = []

    async def _fetch(query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM trades t" in q and "GROUP BY upper" in q:
            assert "t.trading_mode = $1" in q
            bound.append(args)
            return [
                {"league": "NFL", "n": 4},
                {"league": "NBA", "n": 1},
            ]
        return []

    monkeypatch.setattr(cycle_mod.db, "fetch", _fetch)
    out = asyncio.run(
        cycle_mod._today_trade_counts_per_league(trading_mode="live")
    )
    assert out == {"NFL": 4, "NBA": 1}
    assert bound == [("live",)]


def test_today_trade_counts_isolates_modes(monkeypatch: pytest.MonkeyPatch):
    """The Round-11 leak: paper churn must not throttle a live cycle."""
    seeded = {
        "live": [
            {"league": "MLB", "n": 2},
        ],
        "paper": [
            {"league": "MLB", "n": 200},  # would trip the daily cap
        ],
    }

    async def _fetch(query: str, *args: Any):
        q = " ".join(query.split())
        if "FROM trades t" in q and "GROUP BY upper" in q:
            return seeded[args[0]]
        return []

    monkeypatch.setattr(cycle_mod.db, "fetch", _fetch)
    live = asyncio.run(
        cycle_mod._today_trade_counts_per_league(trading_mode="live")
    )
    paper = asyncio.run(
        cycle_mod._today_trade_counts_per_league(trading_mode="paper")
    )
    assert live == {"MLB": 2}
    assert paper == {"MLB": 200}


def test_today_trade_counts_unknown_mode_defaults_to_live(
    monkeypatch: pytest.MonkeyPatch,
):
    bound: list[tuple[str, ...]] = []

    async def _fetch(query: str, *args: Any):
        if "FROM trades t" in query and "GROUP BY upper" in query:
            bound.append(args)
            return []
        return []

    monkeypatch.setattr(cycle_mod.db, "fetch", _fetch)
    asyncio.run(cycle_mod._today_trade_counts_per_league(trading_mode="halt"))
    asyncio.run(cycle_mod._today_trade_counts_per_league(trading_mode=None))  # type: ignore[arg-type]
    assert all(b == ("live",) for b in bound)
