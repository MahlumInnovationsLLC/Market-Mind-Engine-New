"""Idempotency test for canary decrement on duplicate trade settlement
(Task #752 step 5 — second-pass architect finding).

The settlement pipeline can re-run on the same trade row because of:
* live websocket fill + polling fallback both observing the same fill
* operator-initiated settle_market re-run after a transient DB hiccup
* downstream retry of a failed postmortem write that triggers the
  whole loop again

The trade UPDATE itself is naturally idempotent (``settled_at IS
NULL`` predicate) but the canary decrement was previously called
unconditionally after the UPDATE, which meant two calls drained two
slots from a 5-slot canary budget for a single canary trade.

This test locks in the post-fix behaviour: ``_settle_one_trade`` only
calls ``canary.decrement_on_settle`` when the UPDATE actually
transitioned the row, by checking the ``RETURNING id`` result.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.settlement import pipeline as settle_mod
from app.risk import canary


# ---------------------------------------------------------------------------
# Stub DB capturing UPDATE result + canary decrements
# ---------------------------------------------------------------------------


class _SettleDB:
    """In-memory stub that mimics ``UPDATE ... RETURNING id`` semantics
    plus the ``system_settings`` rows the canary helper touches."""

    def __init__(self, *, already_settled: bool) -> None:
        self.already_settled = already_settled
        self.settled_at_set = not already_settled
        # canary state
        self.canary_remaining = 5
        self.update_calls = 0
        self.decrement_calls = 0

    async def fetchrow(self, query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if "UPDATE trades" in q and "RETURNING id" in q:
            self.update_calls += 1
            if self.already_settled:
                # No row returned — predicate `settled_at IS NULL` failed.
                return None
            # First settle wins; subsequent calls must observe the row
            # already settled (we mutate state to reflect that).
            self.already_settled = True
            return {"id": args[0]}
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if "FROM system_settings" in q and args[0] == "risk.canary_remaining":
            return str(self.canary_remaining)
        if "UPDATE system_settings" in q and "RETURNING value" in q:
            # decrement
            self.decrement_calls += 1
            self.canary_remaining = max(0, self.canary_remaining - 1)
            return str(self.canary_remaining)
        return None

    async def execute(self, query: str, *args: Any) -> str:
        # Postmortem inserts and any other defensive writes — accept
        # them silently so the pipeline doesn't crash on something
        # outside this test's scope.
        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_db(monkeypatch):
    """Patch both the settlement and canary db handles to a single
    shared in-memory state object."""
    state = _SettleDB(already_settled=False)
    monkeypatch.setattr(settle_mod.db, "fetchrow", state.fetchrow)
    monkeypatch.setattr(settle_mod.db, "execute", state.execute)
    monkeypatch.setattr(settle_mod.db, "fetch", state.fetch)
    monkeypatch.setattr(canary.db, "fetchval", state.fetchval)
    monkeypatch.setattr(canary.db, "execute", state.execute)
    return state


def _trade(**overrides):
    base: dict[str, Any] = {
        "id": 7,
        "trade_id": "uuid-7",
        "market_id": "KXNFL-7",
        "league": "NFL",
        "vertical": "sports",
        "side": "yes",
        "quantity": 1.0,
        "price": 0.5,
        "model_prob": 0.6,
        "model_edge": 0.1,
        "executed_at": None,
        "metadata": {"canary": True},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_settle_decrements_canary(patched_db):
    """Baseline: a fresh canary trade settling for the first time
    drains exactly one slot."""
    trade = _trade()
    asyncio.run(settle_mod._settle_one_trade(trade, outcome_yes=1, settle_price=1.0))
    assert patched_db.update_calls == 1
    assert patched_db.decrement_calls == 1
    assert patched_db.canary_remaining == 4


def test_duplicate_settle_does_not_double_decrement(patched_db):
    """The fix for the architect's race finding: the second settle
    observes ``settled_at IS NOT NULL`` so the UPDATE returns no row
    and the canary decrement is skipped."""
    trade = _trade()
    asyncio.run(settle_mod._settle_one_trade(trade, outcome_yes=1, settle_price=1.0))
    asyncio.run(settle_mod._settle_one_trade(trade, outcome_yes=1, settle_price=1.0))
    assert patched_db.update_calls == 2  # both UPDATEs ran
    assert patched_db.decrement_calls == 1  # but only one decrement
    assert patched_db.canary_remaining == 4


def test_non_canary_trade_never_decrements(patched_db):
    """Sanity: a non-canary trade settling never touches the counter
    even on the first settle."""
    trade = _trade(metadata={"three_way_leg": "home"})
    asyncio.run(settle_mod._settle_one_trade(trade, outcome_yes=1, settle_price=1.0))
    assert patched_db.update_calls == 1
    assert patched_db.decrement_calls == 0
    assert patched_db.canary_remaining == 5


def test_already_settled_canary_trade_at_pipeline_entry(monkeypatch):
    """Pre-existing settled trade at pipeline entry (the trade row
    came in already settled from the caller's snapshot) — UPDATE
    returns no row, canary not touched."""
    state = _SettleDB(already_settled=True)
    monkeypatch.setattr(settle_mod.db, "fetchrow", state.fetchrow)
    monkeypatch.setattr(settle_mod.db, "execute", state.execute)
    monkeypatch.setattr(settle_mod.db, "fetch", state.fetch)
    monkeypatch.setattr(canary.db, "fetchval", state.fetchval)
    monkeypatch.setattr(canary.db, "execute", state.execute)

    trade = _trade()
    asyncio.run(settle_mod._settle_one_trade(trade, outcome_yes=1, settle_price=1.0))
    assert state.update_calls == 1
    assert state.decrement_calls == 0
    assert state.canary_remaining == 5
