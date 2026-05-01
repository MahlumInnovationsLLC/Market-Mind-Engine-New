"""Tests for the canary cap on first-N live trades (Task #752 step 5 —
Round-11 audit Bug #8).

Coverage:

* Counter init: ``ensure_initialized`` writes ``AXIOM_CANARY_TRADES``
  on first read; subsequent calls return the existing value.
* Forced-qty path: while ``remaining > 0``, ``force_quantity_for_live``
  returns ``(1, True)`` regardless of intended size.
* Settle decrement: ``decrement_on_settle`` only fires when the trade's
  metadata carries ``canary=True``; non-canary trades are no-ops.
* Reject path: a rejected (never-placed) trade has no metadata, so the
  counter never decrements — the operator gets the full canary budget.
* Auto-lift: once the counter reaches 0, ``force_quantity_for_live``
  returns the intended quantity unchanged.
* Read-failure safety: if ``get_remaining`` errors, callers get
  ``(1, True)`` — biased toward smaller trades, never larger.
* Env-var parsing: ``AXIOM_CANARY_TRADES`` overrides the default.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.risk import canary


# ---------------------------------------------------------------------------
# In-memory ``system_settings`` stub
# ---------------------------------------------------------------------------


class _FakeSettingsDB:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        # Force ``get_remaining`` to error on the *next* read — used by
        # the read-failure safety test.
        self.fetchval_should_raise: bool = False

    async def fetchval(self, query: str, *args: Any):
        if self.fetchval_should_raise:
            self.fetchval_should_raise = False
            raise RuntimeError("simulated DB outage")
        q = " ".join(query.split())
        if "FROM system_settings" in q and "WHERE key = $1" in q:
            return self.rows.get(args[0])
        if (
            "UPDATE system_settings" in q
            and "RETURNING value" in q
        ):
            key = args[0]
            cur = int(self.rows.get(key) or 0)
            new = max(0, cur - 1)
            self.rows[key] = str(new)
            return str(new)
        return None

    async def execute(self, query: str, *args: Any):
        q = " ".join(query.split())
        if "INSERT INTO system_settings" in q:
            key, value = args[0], args[1]
            # Honour ON CONFLICT DO NOTHING for the seed write.
            if "DO NOTHING" in q and key in self.rows:
                return "OK"
            self.rows[key] = str(value)
            return "OK"
        return "OK"


@pytest.fixture
def fake_settings(monkeypatch):
    fake = _FakeSettingsDB()
    monkeypatch.setattr(canary.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(canary.db, "execute", fake.execute)
    return fake


# ---------------------------------------------------------------------------
# Env-var parsing
# ---------------------------------------------------------------------------


def test_default_canary_count_is_five(monkeypatch):
    monkeypatch.delenv("AXIOM_CANARY_TRADES", raising=False)
    assert canary._default_canary_count() == 5


def test_default_canary_count_honours_env(monkeypatch):
    monkeypatch.setenv("AXIOM_CANARY_TRADES", "3")
    assert canary._default_canary_count() == 3


def test_default_canary_count_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("AXIOM_CANARY_TRADES", "abc")
    assert canary._default_canary_count() == 5


def test_default_canary_count_honours_zero(monkeypatch):
    """0 is a legitimate "canary disabled" override."""
    monkeypatch.setenv("AXIOM_CANARY_TRADES", "0")
    assert canary._default_canary_count() == 0


def test_default_canary_count_falls_back_on_negative(monkeypatch):
    monkeypatch.setenv("AXIOM_CANARY_TRADES", "-1")
    assert canary._default_canary_count() == 5


# ---------------------------------------------------------------------------
# get_remaining / ensure_initialized
# ---------------------------------------------------------------------------


def test_get_remaining_returns_none_when_unset(fake_settings):
    assert asyncio.run(canary.get_remaining()) is None


def test_get_remaining_returns_int(fake_settings):
    fake_settings.rows["risk.canary_remaining"] = "3"
    assert asyncio.run(canary.get_remaining()) == 3


def test_get_remaining_swallows_errors(fake_settings):
    fake_settings.fetchval_should_raise = True
    assert asyncio.run(canary.get_remaining()) is None


def test_ensure_initialized_seeds_default(monkeypatch, fake_settings):
    monkeypatch.delenv("AXIOM_CANARY_TRADES", raising=False)
    out = asyncio.run(canary.ensure_initialized())
    assert out == 5
    assert fake_settings.rows["risk.canary_remaining"] == "5"
    assert "risk.canary_init_at" in fake_settings.rows


def test_ensure_initialized_is_idempotent(monkeypatch, fake_settings):
    """Second call observes the existing value and writes nothing."""
    fake_settings.rows["risk.canary_remaining"] = "2"
    out = asyncio.run(canary.ensure_initialized())
    assert out == 2
    # Counter unchanged — second init MUST NOT clobber.
    assert fake_settings.rows["risk.canary_remaining"] == "2"


def test_reset_overwrites_remaining(monkeypatch, fake_settings):
    fake_settings.rows["risk.canary_remaining"] = "1"
    out = asyncio.run(canary.reset(7))
    assert out == 7
    assert fake_settings.rows["risk.canary_remaining"] == "7"


def test_reset_uses_env_default_when_count_omitted(monkeypatch, fake_settings):
    monkeypatch.setenv("AXIOM_CANARY_TRADES", "4")
    out = asyncio.run(canary.reset())
    assert out == 4


def test_reset_floors_negative(monkeypatch, fake_settings):
    out = asyncio.run(canary.reset(-3))
    assert out == 0


# ---------------------------------------------------------------------------
# Placement: force_quantity_for_live
# ---------------------------------------------------------------------------


def test_force_quantity_returns_one_while_remaining_positive(fake_settings):
    fake_settings.rows["risk.canary_remaining"] = "5"
    qty, active = asyncio.run(canary.force_quantity_for_live(intended_quantity=42))
    assert qty == 1
    assert active is True


def test_force_quantity_passes_through_when_canary_exhausted(fake_settings):
    fake_settings.rows["risk.canary_remaining"] = "0"
    qty, active = asyncio.run(canary.force_quantity_for_live(intended_quantity=42))
    assert qty == 42
    assert active is False


def test_force_quantity_passes_through_when_uninit(fake_settings):
    """When ``get_remaining`` returns None due to a missing row (NOT a
    DB error), we *must* force qty=1 — uninit on a live cutover is the
    common path, not an exception.

    This protects against the scenario where ``ensure_initialized``
    failed silently at toggle time but live trades are still flowing."""
    # No row — ensure_initialized was never called.
    qty, active = asyncio.run(canary.force_quantity_for_live(intended_quantity=42))
    assert qty == 1
    assert active is True


def test_force_quantity_biases_safe_on_db_error(fake_settings):
    fake_settings.fetchval_should_raise = True
    qty, active = asyncio.run(canary.force_quantity_for_live(intended_quantity=42))
    assert qty == 1
    assert active is True


# ---------------------------------------------------------------------------
# Settlement: decrement_on_settle
# ---------------------------------------------------------------------------


def test_is_canary_trade_recognizes_flag():
    assert canary.is_canary_trade({"canary": True}) is True
    assert canary.is_canary_trade({"canary": "true"}) is True
    assert canary.is_canary_trade({"canary": 1}) is True
    assert canary.is_canary_trade({"canary": False}) is False
    assert canary.is_canary_trade({"canary": 0}) is False
    assert canary.is_canary_trade({}) is False
    assert canary.is_canary_trade(None) is False
    assert canary.is_canary_trade("not a dict") is False


def test_decrement_on_settle_skips_non_canary(fake_settings):
    fake_settings.rows["risk.canary_remaining"] = "5"
    out = asyncio.run(canary.decrement_on_settle({
        "id": 1, "metadata": {"three_way_leg": "home"},
    }))
    assert out is False
    assert fake_settings.rows["risk.canary_remaining"] == "5"


def test_decrement_on_settle_skips_when_metadata_missing(fake_settings):
    fake_settings.rows["risk.canary_remaining"] = "5"
    out = asyncio.run(canary.decrement_on_settle({"id": 1, "metadata": None}))
    assert out is False
    assert fake_settings.rows["risk.canary_remaining"] == "5"


def test_decrement_on_settle_decrements_canary(fake_settings):
    fake_settings.rows["risk.canary_remaining"] = "3"
    out = asyncio.run(canary.decrement_on_settle({
        "id": 7, "metadata": {"canary": True},
    }))
    assert out is True
    assert fake_settings.rows["risk.canary_remaining"] == "2"


def test_decrement_on_settle_floors_at_zero(fake_settings):
    """Two settlements race past the last canary slot — counter must
    not go negative."""
    fake_settings.rows["risk.canary_remaining"] = "1"
    out1 = asyncio.run(canary.decrement_on_settle({
        "id": 1, "metadata": {"canary": True},
    }))
    out2 = asyncio.run(canary.decrement_on_settle({
        "id": 2, "metadata": {"canary": True},
    }))
    assert out1 is True
    assert out2 is True
    assert fake_settings.rows["risk.canary_remaining"] == "0"


# ---------------------------------------------------------------------------
# End-to-end story: 5 settles → auto-lift
# ---------------------------------------------------------------------------


def test_canary_lifts_after_five_settlements(monkeypatch, fake_settings):
    """The full Round-11 contract: seed at 5, force qty=1 for the
    first 5 live trades, then return full sizing on the 6th."""
    monkeypatch.setenv("AXIOM_CANARY_TRADES", "5")
    asyncio.run(canary.ensure_initialized())

    # 5 placements + 5 settles.
    for i in range(5):
        qty, active = asyncio.run(
            canary.force_quantity_for_live(intended_quantity=20)
        )
        assert qty == 1
        assert active is True
        asyncio.run(canary.decrement_on_settle({
            "id": i, "metadata": {"canary": True},
        }))

    # 6th placement: canary lifted.
    qty, active = asyncio.run(
        canary.force_quantity_for_live(intended_quantity=20)
    )
    assert qty == 20
    assert active is False


def test_canary_stays_active_when_trades_rejected_pre_settle(
    monkeypatch, fake_settings
):
    """Rejection (lifecycle bounce, gate veto, exchange decline) means
    the trade row was never inserted — no metadata, no settle, no
    decrement.  Operator's full N-trade safety budget is preserved."""
    monkeypatch.setenv("AXIOM_CANARY_TRADES", "3")
    asyncio.run(canary.ensure_initialized())

    # 5 placements all see canary=active, but no settles happen
    # because the trades were rejected before insert.
    for _ in range(5):
        qty, active = asyncio.run(
            canary.force_quantity_for_live(intended_quantity=10)
        )
        assert qty == 1
        assert active is True

    # Counter still at 3 — rejections did not consume canary budget.
    assert fake_settings.rows["risk.canary_remaining"] == "3"
