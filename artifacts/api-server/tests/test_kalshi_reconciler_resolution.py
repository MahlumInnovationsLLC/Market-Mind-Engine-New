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
    """Run an async coroutine without poisoning global event-loop state.

    Other tests in this suite (e.g. ``test_odds_api_persist_outcome``)
    use the legacy ``asyncio.get_event_loop().run_until_complete()``
    pattern, which on Python 3.10+ raises ``RuntimeError`` if the
    policy's current loop is missing or closed. We:

      1. Snapshot the current loop (if any) at entry — using a private
         ``_get_running_loop`` peek so we don't trip auto-creation.
      2. Run our coroutine on a brand-new loop.
      3. In the finally block, close that loop and ensure the policy
         is left with a fresh, open, current loop (restoring the
         pre-existing one if it's still alive, otherwise installing a
         new one). This guarantees subsequent legacy callers always
         see a valid current loop.
    """
    policy = asyncio.get_event_loop_policy()
    prev_loop: asyncio.AbstractEventLoop | None
    try:
        prev_loop = policy.get_event_loop()
    except Exception:
        prev_loop = None
    new_loop = asyncio.new_event_loop()
    try:
        return new_loop.run_until_complete(coro)
    finally:
        new_loop.close()
        if prev_loop is not None and not prev_loop.is_closed():
            asyncio.set_event_loop(prev_loop)
        else:
            # No usable previous loop — install a fresh one so legacy
            # callers downstream don't blow up on get_event_loop().
            asyncio.set_event_loop(asyncio.new_event_loop())


# ---------------------------------------------------------------------------
# Tiny DB stub that records every call. Tests can preload responses keyed
# by the FIRST keyword in the SQL — good enough for our query shapes.
# ---------------------------------------------------------------------------


class _DBStub:
    """In-memory replacement for ``app.db``.

    Captures every fetch/fetchrow/fetchval/execute call (so tests can
    assert query text + params) and pretends to support the
    ``acquire() / conn.transaction()`` pattern that
    ``accept_exchange_truth`` uses for atomicity. The stub's
    transaction is simulated: it does NOT roll back captured calls on
    exception (each test that wants to exercise the rollback path
    drives the failure via ``execute_raises``), but the context
    managers behave correctly enough for the production code to run
    against the stub unchanged.
    """

    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple]] = []
        self.fetches: list[tuple[str, tuple]] = []
        self.fetchrows: list[tuple[str, tuple]] = []
        self.fetchvals: list[tuple[str, tuple]] = []
        self.fetch_responses: list[Any] = []
        self.fetchrow_responses: list[Any] = []
        self.fetchval_responses: list[Any] = []
        # If set, the next ``execute`` whose query contains this
        # substring raises the given exception — used to drive the
        # transactional-rollback test path.
        self.execute_raises: tuple[str, Exception] | None = None
        # Counters for transaction-context entries/exits so tests can
        # assert that production code wraps multi-step writes in a
        # transaction.
        self.tx_enters = 0
        self.tx_exits = 0

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
        if self.execute_raises is not None:
            needle, exc = self.execute_raises
            if needle in q:
                self.execute_raises = None
                raise exc
        return None

    # -- acquire() / conn.transaction() simulation -----------------

    def acquire(self):
        return _StubAcquireCM(self)


class _StubAcquireCM:
    def __init__(self, db: "_DBStub") -> None:
        self._db = db

    async def __aenter__(self) -> "_StubConn":
        return _StubConn(self._db)

    async def __aexit__(self, *exc_info) -> None:
        return None


class _StubConn:
    """A fake asyncpg connection that delegates ``execute`` to the
    parent stub so call-capture works inside transactions.
    """

    def __init__(self, db: "_DBStub") -> None:
        self._db = db

    async def execute(self, q, *a):
        return await self._db.execute(q, *a)

    async def fetch(self, q, *a):
        return await self._db.fetch(q, *a)

    async def fetchrow(self, q, *a):
        return await self._db.fetchrow(q, *a)

    def transaction(self):
        return _StubTxn(self._db)


class _StubTxn:
    def __init__(self, db: "_DBStub") -> None:
        self._db = db

    async def __aenter__(self) -> "_StubTxn":
        self._db.tx_enters += 1
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._db.tx_exits += 1
        return None


def _install_db(monkeypatch) -> _DBStub:
    from app import db as app_db
    stub = _DBStub()
    monkeypatch.setattr(app_db, "fetch", stub.fetch)
    monkeypatch.setattr(app_db, "fetchrow", stub.fetchrow)
    monkeypatch.setattr(app_db, "fetchval", stub.fetchval)
    monkeypatch.setattr(app_db, "execute", stub.execute)
    # ``accept_exchange_truth`` uses the ``acquire() / conn.transaction()``
    # pattern for atomicity — patch it too so production code never
    # falls through to the real connection pool during tests.
    monkeypatch.setattr(app_db, "acquire", stub.acquire)
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


def test_snooze_drift_writes_absolute_until(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append({
        "id": 21, "local_qty": 3, "exchange_qty": 5, "drift_qty": 2,
    })

    until = datetime.now(timezone.utc) + timedelta(hours=24)
    res = _run(reconciler.snooze_drift(
        ticker="MKT-A", until=until,
        resolver_email="ops@axiom.dev", reason="known stale",
    ))

    assert res["changed"] is True
    assert res["snoozed_until"] == until.isoformat()
    # The UPDATE must persist the absolute datetime, not a relative
    # interval — verify both the SQL shape and the bound parameter.
    update_calls = [(q, p) for q, p in db.executes if "state = 'snoozed'" in q]
    assert len(update_calls) == 1
    assert update_calls[0][1][1] == until


def test_snooze_drift_rejects_past_until(monkeypatch):
    """A past ``until`` is rejected with a stable, non-DB reason — the
    UI relies on this to show "no active drift" vs "bad input"."""
    from datetime import datetime, timedelta, timezone
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    until = datetime.now(timezone.utc) - timedelta(hours=1)
    res = _run(reconciler.snooze_drift(
        ticker="MKT-A", until=until,
        resolver_email="ops@axiom.dev", reason=None,
    ))
    assert res["changed"] is False
    assert "until_too_soon" in res["reason"]
    # Critically: no DB writes happen when validation fails.
    assert db.executes == []


def test_snooze_drift_rejects_far_future_until(monkeypatch):
    """A fat-fingered far-future ``until`` (e.g. year 2099) is rejected
    so a typo can't permanently silence the alert."""
    from datetime import datetime, timedelta, timezone
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    until = datetime.now(timezone.utc) + timedelta(days=400)
    res = _run(reconciler.snooze_drift(
        ticker="MKT-A", until=until,
        resolver_email="ops@axiom.dev", reason=None,
    ))
    assert res["changed"] is False
    assert "until_too_far" in res["reason"]
    assert db.executes == []


def test_snooze_drift_idempotent_no_active(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append(None)

    until = datetime.now(timezone.utc) + timedelta(hours=24)
    res = _run(reconciler.snooze_drift(
        ticker="MKT-A", until=until, resolver_email=None,
    ))
    assert res["changed"] is False
    assert all("state = 'snoozed'" not in q for q, _ in db.executes)


def test_snooze_drift_treats_naive_datetime_as_utc(monkeypatch):
    """A timezone-naive ``until`` (e.g. ``datetime.utcnow()``) must be
    interpreted as UTC, not the host's local timezone — operators
    running shells in PST would otherwise snooze for the wrong number
    of hours.
    """
    from datetime import datetime, timedelta, timezone
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append({
        "id": 22, "local_qty": 1, "exchange_qty": 4, "drift_qty": 3,
    })

    naive_until = (
        datetime.now(timezone.utc) + timedelta(hours=24)
    ).replace(tzinfo=None)
    res = _run(reconciler.snooze_drift(
        ticker="MKT-NAIVE", until=naive_until, resolver_email=None,
    ))
    assert res["changed"] is True
    # The persisted snoozed_until must be tz-aware UTC even though we
    # passed naive in.
    update_calls = [(q, p) for q, p in db.executes if "state = 'snoozed'" in q]
    assert len(update_calls) == 1
    persisted = update_calls[0][1][1]
    assert persisted.tzinfo is not None
    assert persisted.utcoffset().total_seconds() == 0


def test_snooze_drift_parses_iso_string(monkeypatch):
    """The admin endpoint receives a Pydantic-parsed datetime, but
    operators calling the reconciler from a shell (or the SQL fallback
    path) may pass an ISO string. Both must work.
    """
    from datetime import datetime, timedelta, timezone
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append({
        "id": 23, "local_qty": 0, "exchange_qty": 7, "drift_qty": 7,
    })

    until_iso = (
        datetime.now(timezone.utc) + timedelta(hours=12)
    ).isoformat().replace("+00:00", "Z")
    res = _run(reconciler.snooze_drift(
        ticker="MKT-ISO", until=until_iso, resolver_email=None,
    ))
    assert res["changed"] is True


def test_snooze_drift_rejects_garbage_iso_string(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    res = _run(reconciler.snooze_drift(
        ticker="MKT-X", until="not-a-date", resolver_email=None,
    ))
    assert res["changed"] is False
    assert "invalid_until" in res["reason"]
    assert db.executes == []


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
