"""Tests for the position-drift resolution workflow (task #709).

These exercise the new code paths in ``app.kalshi.reconciler``:

* ``_upsert_drift_resolution`` — open / refresh / snooze auto-expiry.
* ``_create_orphan_skeleton`` — synthetic ``positions`` row for a true
  orphan exchange position.
* ``_self_heal_resolutions`` — open rows whose drift cleared on its
  own get marked ``resolved_self_healed``.
* ``accept_exchange_truth`` — closes locals, inserts synth, acks
  alerts, resolves drift row, idempotent on re-run.
* ``snooze_drift`` — sets snoozed_until + suppresses alert in the next
  diff cycle, then auto-expires.
* ``_diff_positions`` 4-tuple shape and integration with the flag.
* Flag-off: writes are skipped entirely.

The reconciler module talks to ``app.db`` via fetch / fetchrow /
fetchval / execute — we monkeypatch all four with capture lists.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Tiny DB stub that records every call. Tests can preload responses keyed
# by the FIRST keyword in the SQL — good enough for our query shapes.
# ---------------------------------------------------------------------------


class _DBStub:
    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple]] = []
        self.fetches: list[tuple[str, tuple]] = []
        self.fetchrows: list[tuple[str, tuple]] = []
        self.fetchvals: list[tuple[str, tuple]] = []
        self.fetch_responses: list[Any] = []
        self.fetchrow_responses: list[Any] = []
        self.fetchval_responses: list[Any] = []

    async def fetch(self, q, *a):
        self.fetches.append((q, a))
        if self.fetch_responses:
            return self.fetch_responses.pop(0)
        return []

    async def fetchrow(self, q, *a):
        self.fetchrows.append((q, a))
        if self.fetchrow_responses:
            return self.fetchrow_responses.pop(0)
        return None

    async def fetchval(self, q, *a):
        self.fetchvals.append((q, a))
        if self.fetchval_responses:
            return self.fetchval_responses.pop(0)
        return None

    async def execute(self, q, *a):
        self.executes.append((q, a))
        return None


def _install_db(monkeypatch) -> _DBStub:
    from app import db as app_db
    stub = _DBStub()
    monkeypatch.setattr(app_db, "fetch", stub.fetch)
    monkeypatch.setattr(app_db, "fetchrow", stub.fetchrow)
    monkeypatch.setattr(app_db, "fetchval", stub.fetchval)
    monkeypatch.setattr(app_db, "execute", stub.execute)
    return stub


def _enable_flag(monkeypatch) -> None:
    monkeypatch.setenv("AXIOM_RECONCILER_RESOLUTION_ENABLED", "1")


# ---------------------------------------------------------------------------
# _upsert_drift_resolution
# ---------------------------------------------------------------------------


def test_upsert_inserts_when_no_active_row(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append(None)  # no active row

    state = _run(reconciler._upsert_drift_resolution(
        ticker="MKT-A", vertical="sports",
        local_qty=2, exchange_qty=5, drift=3,
    ))

    assert state == "open"
    assert any("INSERT INTO position_drift_resolutions" in q
               for q, _ in db.executes)


def test_upsert_returns_snoozed_when_active_snooze_unexpired(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    db.fetchrow_responses.append({
        "id": 7, "state": "snoozed", "snoozed_until": future,
    })

    state = _run(reconciler._upsert_drift_resolution(
        ticker="MKT-A", vertical=None,
        local_qty=2, exchange_qty=5, drift=3,
    ))

    assert state == "snoozed"
    # No state-flip — just an UPDATE bumping last_seen_at.
    assert all("state = 'open'" not in q for q, _ in db.executes)


def test_upsert_auto_expires_snooze_when_past_due(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.fetchrow_responses.append({
        "id": 9, "state": "snoozed", "snoozed_until": past,
    })

    state = _run(reconciler._upsert_drift_resolution(
        ticker="MKT-A", vertical=None,
        local_qty=2, exchange_qty=5, drift=3,
    ))

    assert state == "open"
    # The auto-expire UPDATE flips state back to 'open' and clears snoozed_until.
    assert any("state = 'open'" in q and "snoozed_until = NULL" in q
               for q, _ in db.executes)


# ---------------------------------------------------------------------------
# _create_orphan_skeleton
# ---------------------------------------------------------------------------


def test_orphan_skeleton_inserts_positions_row(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)

    ok = _run(reconciler._create_orphan_skeleton(
        ticker="MKT-A", exchange_qty=4, vertical="sports",
    ))
    assert ok is True

    insert_qs = [q for q, _ in db.executes
                 if "INSERT INTO positions" in q]
    assert insert_qs, "expected positions INSERT"
    # Side derived from sign of exchange_qty.
    assert any(("MKT-A", "yes") == (a[1], a[2])
               for q, a in db.executes
               if "INSERT INTO positions" in q and len(a) >= 3)
    # Resolution row marked terminal.
    assert any("resolved_orphan_skeleton" in q for q, _ in db.executes)


def test_orphan_skeleton_short_position(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)

    _run(reconciler._create_orphan_skeleton(
        ticker="MKT-NO", exchange_qty=-3, vertical=None,
    ))

    # Negative exch_qty → side='no', quantity=abs(-3)=3.
    inserts = [a for q, a in db.executes
               if "INSERT INTO positions" in q]
    assert inserts, "expected positions INSERT"
    args = inserts[0]
    assert args[2] == "no"
    assert args[4] == 3


def test_orphan_skeleton_skips_zero_quantity(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)

    ok = _run(reconciler._create_orphan_skeleton(
        ticker="MKT-Z", exchange_qty=0, vertical=None,
    ))
    assert ok is False
    assert all("INSERT INTO positions" not in q for q, _ in db.executes)


# ---------------------------------------------------------------------------
# _has_any_local_position_row
# ---------------------------------------------------------------------------


def test_has_any_local_position_row_failsafe_on_db_error(monkeypatch):
    from app.kalshi import reconciler
    from app import db as app_db

    async def _boom(*a, **k):
        raise RuntimeError("conn lost")

    monkeypatch.setattr(app_db, "fetchval", _boom)
    # Fail-safe: pretend a row exists so we DON'T spuriously skeleton.
    assert _run(reconciler._has_any_local_position_row("MKT-A")) is True


# ---------------------------------------------------------------------------
# accept_exchange_truth
# ---------------------------------------------------------------------------


def test_accept_exchange_truth_closes_then_synths(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append({
        "id": 11, "local_qty": 2, "exchange_qty": 5, "drift_qty": 3,
    })

    res = _run(reconciler.accept_exchange_truth(
        ticker="MKT-A", resolver_email="ops@axiom.dev", reason="manual review",
    ))

    assert res["changed"] is True
    assert res["exchange_qty"] == 5
    qs = [q for q, _ in db.executes]
    # Step 1: closes existing open rows.
    assert any("UPDATE positions" in q and "status = 'closed'" in q for q in qs)
    # Step 2: inserts synth row tagged recon-accept-truth-<ticker>.
    synth_inserts = [
        a for q, a in db.executes
        if "INSERT INTO positions" in q and a and a[0] == "recon-accept-truth-MKT-A"
    ]
    assert synth_inserts, "expected synth INSERT with recon-accept-truth id"
    # Step 3: acks operator alerts.
    assert any("model_health_alerts" in q and "acknowledged = true" in q
               for q in qs)
    # Step 4: resolution row → resolved_accept_exchange.
    assert any("resolved_accept_exchange" in q for q in qs)


def test_accept_exchange_truth_idempotent_when_no_active_drift(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append(None)  # no active row

    res = _run(reconciler.accept_exchange_truth(
        ticker="MKT-A", resolver_email="ops@axiom.dev",
    ))

    assert res["changed"] is False
    assert all("UPDATE positions" not in q for q, _ in db.executes)
    assert all("INSERT INTO positions" not in q for q, _ in db.executes)


def test_accept_exchange_truth_zero_skips_synth_insert(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append({
        "id": 13, "local_qty": 4, "exchange_qty": 0, "drift_qty": 4,
    })

    res = _run(reconciler.accept_exchange_truth(
        ticker="MKT-A", resolver_email=None,
    ))
    assert res["changed"] is True
    # Locals get closed, but no synth row inserted (exchange has zero qty).
    assert all("INSERT INTO positions" not in q for q, _ in db.executes)


# ---------------------------------------------------------------------------
# snooze_drift
# ---------------------------------------------------------------------------


def test_snooze_drift_clamps_and_writes(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append({
        "id": 21, "local_qty": 3, "exchange_qty": 5, "drift_qty": 2,
    })

    res = _run(reconciler.snooze_drift(
        ticker="MKT-A", snooze_seconds=60,  # clamped up to 1h
        resolver_email="ops@axiom.dev", reason="known stale",
    ))

    assert res["changed"] is True
    assert res["snoozed_for_seconds"] == 3600
    assert any("state = 'snoozed'" in q for q, _ in db.executes)


def test_snooze_drift_idempotent_no_active(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append(None)

    res = _run(reconciler.snooze_drift(
        ticker="MKT-A", snooze_seconds=86400, resolver_email=None,
    ))
    assert res["changed"] is False
    assert all("state = 'snoozed'" not in q for q, _ in db.executes)


# ---------------------------------------------------------------------------
# _diff_positions integration with the flag
# ---------------------------------------------------------------------------


class _FakePos:
    def __init__(self, ticker: str, qty: int) -> None:
        self.market_ticker = ticker
        self.position = qty


def test_diff_positions_returns_4_tuple_flag_off(monkeypatch):
    from app.kalshi import reconciler
    monkeypatch.delenv("AXIOM_RECONCILER_RESOLUTION_ENABLED", raising=False)
    db = _install_db(monkeypatch)
    # _local_signed_positions will return [] (DB stub default).
    # Suppress the alert path's dedupe write — it just needs to no-op.
    monkeypatch.setattr(
        reconciler, "_raise_position_drift_alert",
        _stub_async(return_value=True),
    )

    res = _run(reconciler._diff_positions([_FakePos("MKT-A", 5)]))
    assert res == (1, 0, 0, 0)
    # Flag off → no resolution rows queried.
    assert all("position_drift_resolutions" not in q
               for q, _ in db.fetchrows)


def test_diff_positions_snooze_suppresses_alert_when_flag_on(monkeypatch):
    from app.kalshi import reconciler
    _enable_flag(monkeypatch)
    db = _install_db(monkeypatch)
    # _local_signed_positions: empty positions table.
    db.fetch_responses.append([])
    # Active snoozed row in the future.
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    db.fetchrow_responses.append({
        "id": 5, "state": "snoozed", "snoozed_until": future,
    })

    raised: list[str] = []

    async def _no_raise(**kw):
        raised.append(kw["ticker"])
        return True

    monkeypatch.setattr(reconciler, "_raise_position_drift_alert", _no_raise)

    res = _run(reconciler._diff_positions([_FakePos("MKT-A", 5)]))
    drift_count, _, _, snoozed = res
    assert drift_count == 0
    assert snoozed == 1
    assert raised == []


def test_diff_positions_orphan_path_creates_skeleton(monkeypatch):
    from app.kalshi import reconciler
    _enable_flag(monkeypatch)
    db = _install_db(monkeypatch)
    db.fetch_responses.append([])  # _local_signed_positions empty
    db.fetchrow_responses.append(None)  # no active drift row → INSERT
    # _has_any_local_position_row → None means "no positions row at all".
    db.fetchval_responses.append(None)

    raised: list[str] = []

    async def _no_raise(**kw):
        raised.append(kw["ticker"])
        return True

    monkeypatch.setattr(reconciler, "_raise_position_drift_alert", _no_raise)

    res = _run(reconciler._diff_positions([_FakePos("MKT-A", 4)]))
    drift_count, _, orphan, _snoozed = res
    assert drift_count == 0
    assert orphan == 1
    assert raised == []
    # Orphan-skeleton path leaves an INSERT trail in positions.
    assert any("INSERT INTO positions" in q for q, _ in db.executes)


def test_diff_positions_self_heals_open_rows_when_flag_on(monkeypatch):
    """With the flag on, an open drift row whose ticker is no longer
    drifting gets marked ``resolved_self_healed`` by the sweep at the
    end of ``_diff_positions``.
    """
    from app.kalshi import reconciler
    _enable_flag(monkeypatch)
    db = _install_db(monkeypatch)
    db.fetch_responses.append([])  # local positions empty

    _run(reconciler._diff_positions([]))

    sweep_qs = [q for q, _ in db.executes
                if "resolved_self_healed" in q]
    assert sweep_qs, "expected self-heal sweep UPDATE"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stub_async(*, return_value=None):
    async def _f(*a, **kw):
        return return_value
    return _f
