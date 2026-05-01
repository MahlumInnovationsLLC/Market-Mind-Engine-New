"""Tests for the paper-bankroll seed + ledger isolation
(Task #752 step 3 — Round-11 audit Bug #6).

Three guarantees the production code now provides:

1. **Paper seed is idempotent**: ``ensure_paper_bankroll_seeded`` writes
   exactly one ``reason='paper_seed'`` row at boot, then no-ops forever.

2. **Paper ↔ live ledgers are isolated**: ``record_balance`` keys both
   the "is this a duplicate?" lookup and the INSERT on ``trading_mode``.
   A paper seed of $1,000 must NOT make a live exchange-sync of $250
   look like a +$750 delta on the live ledger.

3. **AXIOM_PAPER_BANKROLL_USD overrides the default seed**: the env var
   is the operator-facing knob (`docs/runbook.md` §18 will document it).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import bankroll_sync


# Re-use the shared in-memory fake from the existing emergency-stop tests
# so any change to the schema lands once.
from tests.test_emergency_stop_and_bankroll import _FakeBankrollDB  # type: ignore


# ---------------------------------------------------------------------------
# _paper_bankroll_seed_cents — env-var parsing
# ---------------------------------------------------------------------------


def test_paper_bankroll_seed_default_is_one_thousand(monkeypatch):
    monkeypatch.delenv("AXIOM_PAPER_BANKROLL_USD", raising=False)
    assert bankroll_sync._paper_bankroll_seed_cents() == 100_000  # $1000.00


def test_paper_bankroll_seed_honours_env(monkeypatch):
    monkeypatch.setenv("AXIOM_PAPER_BANKROLL_USD", "2500.50")
    assert bankroll_sync._paper_bankroll_seed_cents() == 250_050


def test_paper_bankroll_seed_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AXIOM_PAPER_BANKROLL_USD", "not-a-number")
    assert bankroll_sync._paper_bankroll_seed_cents() == 100_000


def test_paper_bankroll_seed_negative_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AXIOM_PAPER_BANKROLL_USD", "-100")
    assert bankroll_sync._paper_bankroll_seed_cents() == 100_000


# ---------------------------------------------------------------------------
# ensure_paper_bankroll_seeded — first call writes one row, idempotent.
# ---------------------------------------------------------------------------


def test_ensure_paper_bankroll_seeded_writes_one_row(monkeypatch):
    monkeypatch.setenv("AXIOM_PAPER_BANKROLL_USD", "1500")
    fake = _FakeBankrollDB()
    monkeypatch.setattr(bankroll_sync.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(bankroll_sync.db, "fetchrow", fake.fetchrow)

    out = asyncio.run(bankroll_sync.ensure_paper_bankroll_seeded())

    assert out is not None
    assert out["balance_cents"] == 150_000
    assert out["trading_mode"] == "paper"
    assert out["reason"] == "paper_seed"
    assert out["exchange"] == "kalshi"
    assert len(fake.rows) == 1


def test_ensure_paper_bankroll_seeded_is_idempotent(monkeypatch):
    """Second boot — paper ledger already has a row, so the seeder
    short-circuits and no second row is written."""
    monkeypatch.setenv("AXIOM_PAPER_BANKROLL_USD", "1500")
    fake = _FakeBankrollDB()
    monkeypatch.setattr(bankroll_sync.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(bankroll_sync.db, "fetchrow", fake.fetchrow)

    asyncio.run(bankroll_sync.ensure_paper_bankroll_seeded())
    out_second = asyncio.run(bankroll_sync.ensure_paper_bankroll_seeded())

    assert out_second is None
    assert len(fake.rows) == 1


# ---------------------------------------------------------------------------
# Ledger isolation — paper seed must not affect live delta computation.
# ---------------------------------------------------------------------------


def test_paper_seed_does_not_anchor_live_delta(monkeypatch):
    """The original Round-11 Bug #6: pre-#752 the latest-row lookup
    ignored ``trading_mode``, so the very first live exchange-sync
    after a paper seed of $1,000 would see "prev=$1000" and write
    a spurious -$750 delta when the real Kalshi balance was $250.

    Post-#752: paper and live ledgers are independent.  The first
    live exchange-sync row carries ``delta_cents = balance_cents``
    (seeded against ``prev=None``), regardless of any paper rows.
    """
    monkeypatch.setenv("AXIOM_PAPER_BANKROLL_USD", "1000")
    fake = _FakeBankrollDB()
    monkeypatch.setattr(bankroll_sync.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(bankroll_sync.db, "fetchrow", fake.fetchrow)

    # Seed paper bankroll first.
    asyncio.run(bankroll_sync.ensure_paper_bankroll_seeded())
    assert fake.rows[0]["trading_mode"] == "paper"
    assert fake.rows[0]["balance_cents"] == 100_000

    # Now the live exchange-sync writes its first observation at $250.
    out = asyncio.run(
        bankroll_sync.record_balance(25_000, reason="exchange_sync")
    )
    assert out is not None
    # Critical assertion: live delta is $250 (vs prev=None), NOT
    # $250 - $1000 = -$750 (which would have been the pre-#752 bug).
    assert out["delta_cents"] == 25_000
    assert out["trading_mode"] == "live"
    assert out["balance_cents"] == 25_000

    # Two rows total: paper seed + first live obs.
    assert len(fake.rows) == 2
    paper_rows = [r for r in fake.rows if r["trading_mode"] == "paper"]
    live_rows = [r for r in fake.rows if r["trading_mode"] == "live"]
    assert len(paper_rows) == 1
    assert len(live_rows) == 1


def test_record_balance_explicit_trading_mode_isolates_writes(monkeypatch):
    """Two record_balance calls with the same exchange and balance but
    different ``trading_mode`` MUST both insert — they're different
    ledgers, not duplicates."""
    fake = _FakeBankrollDB()
    monkeypatch.setattr(bankroll_sync.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(bankroll_sync.db, "fetchrow", fake.fetchrow)

    paper_out = asyncio.run(
        bankroll_sync.record_balance(
            50_000, reason="paper_seed", trading_mode="paper"
        )
    )
    live_out = asyncio.run(
        bankroll_sync.record_balance(
            50_000, reason="exchange_sync", trading_mode="live"
        )
    )

    assert paper_out is not None
    assert live_out is not None
    assert paper_out["trading_mode"] == "paper"
    assert live_out["trading_mode"] == "live"
    # Both are seeded against prev=None on their own ledger.
    assert paper_out["delta_cents"] == 50_000
    assert live_out["delta_cents"] == 50_000


def test_latest_event_balance_cents_scopes_by_trading_mode(monkeypatch):
    """The lookup must filter on ``trading_mode`` — without this fix,
    a paper row would shadow the live ledger's "no rows yet" state."""
    fake = _FakeBankrollDB()
    monkeypatch.setattr(bankroll_sync.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(bankroll_sync.db, "fetchrow", fake.fetchrow)

    # Seed paper, then look up the live ledger — must be None.
    asyncio.run(
        bankroll_sync.record_balance(
            100_000, reason="paper_seed", trading_mode="paper"
        )
    )
    live_prev = asyncio.run(
        bankroll_sync._latest_event_balance_cents(trading_mode="live")
    )
    assert live_prev is None

    paper_prev = asyncio.run(
        bankroll_sync._latest_event_balance_cents(trading_mode="paper")
    )
    assert paper_prev == 100_000
