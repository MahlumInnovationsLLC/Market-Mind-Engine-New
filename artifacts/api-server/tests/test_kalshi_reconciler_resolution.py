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
# queue_health() + drift-queue-backlog health card thresholds (task #716)
# ---------------------------------------------------------------------------


def _install_queue_health(monkeypatch, *, open_count, oldest_age, snoozed):
    """Stub ``reconciler.queue_health`` with a fixed snapshot."""
    from app.kalshi import reconciler

    async def _qh():
        return {
            "open_count": open_count,
            "oldest_open_age_seconds": oldest_age,
            "snoozed_count": snoozed,
        }

    monkeypatch.setattr(reconciler, "queue_health", _qh)


def _stub_alert_capture(monkeypatch):
    """Replace ``raise_drift_queue_backlog_alert`` with a capturing stub
    so the threshold tests never hit the real DB / email path."""
    from app.notifications import alerts as _alerts

    raised: list[dict] = []

    async def _capture(**kw):
        raised.append(kw)
        return True

    monkeypatch.setattr(
        _alerts, "raise_drift_queue_backlog_alert", _capture
    )
    return raised


def test_drift_queue_card_ok_under_thresholds(monkeypatch):
    """20 open, 24h oldest, 5 snoozed — exactly AT both edges, so the
    card stays ``ok`` and no alert is raised."""
    from app import health
    _install_queue_health(
        monkeypatch, open_count=20, oldest_age=24 * 3600, snoozed=5,
    )
    raised = _stub_alert_capture(monkeypatch)

    card = _run(health._probe_drift_resolution_queue())

    assert card["key"] == "drift_resolution_queue"
    assert card["label"] == "Drift resolution queue"
    assert card["status"] == "ok"
    assert card["extras"]["open_count"] == 20
    assert card["extras"]["snoozed_count"] == 5
    assert raised == [], "no alert at the OK boundary"


def test_drift_queue_card_degraded_on_open_count(monkeypatch):
    """21 open trips the degraded threshold (open_count > 20)."""
    from app import health
    _install_queue_health(
        monkeypatch, open_count=21, oldest_age=0, snoozed=2,
    )
    raised = _stub_alert_capture(monkeypatch)

    card = _run(health._probe_drift_resolution_queue())

    assert card["status"] == "degraded"
    assert len(raised) == 1
    assert raised[0]["severity"] == "degraded"
    assert raised[0]["open_count"] == 21
    assert raised[0]["snoozed_count"] == 2


def test_drift_queue_card_degraded_on_oldest_age(monkeypatch):
    """A single drift open for >24h trips degraded even if the queue
    is small."""
    from app import health
    _install_queue_health(
        monkeypatch, open_count=3, oldest_age=24 * 3600 + 60, snoozed=0,
    )
    raised = _stub_alert_capture(monkeypatch)

    card = _run(health._probe_drift_resolution_queue())

    assert card["status"] == "degraded"
    assert len(raised) == 1
    assert raised[0]["severity"] == "degraded"


def test_drift_queue_card_down_on_open_count(monkeypatch):
    """51 open trips the down threshold (open_count > 50)."""
    from app import health
    _install_queue_health(
        monkeypatch, open_count=51, oldest_age=0, snoozed=0,
    )
    raised = _stub_alert_capture(monkeypatch)

    card = _run(health._probe_drift_resolution_queue())

    assert card["status"] == "down"
    assert len(raised) == 1
    assert raised[0]["severity"] == "down"
    assert raised[0]["open_count"] == 51


def test_drift_queue_card_down_on_oldest_age(monkeypatch):
    """A single drift open for >72h trips down regardless of queue size."""
    from app import health
    _install_queue_health(
        monkeypatch, open_count=2, oldest_age=72 * 3600 + 60, snoozed=0,
    )
    raised = _stub_alert_capture(monkeypatch)

    card = _run(health._probe_drift_resolution_queue())

    assert card["status"] == "down"
    assert len(raised) == 1
    assert raised[0]["severity"] == "down"


# ---------------------------------------------------------------------------
# queue_health() SQL helper
# ---------------------------------------------------------------------------


def test_queue_health_returns_zero_dict_on_db_error(monkeypatch):
    from app.kalshi import reconciler
    from app import db as app_db

    async def _boom(*a, **k):
        raise RuntimeError("conn lost")

    monkeypatch.setattr(app_db, "fetchrow", _boom)
    snap = _run(reconciler.queue_health())
    assert snap == {
        "open_count": 0,
        "oldest_open_age_seconds": 0,
        "snoozed_count": 0,
    }


def test_queue_health_normalises_row(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append({
        "open_count": 7,
        "snoozed_count": 3,
        "oldest_open_age_seconds": 90_061,
    })
    snap = _run(reconciler.queue_health())
    assert snap == {
        "open_count": 7,
        "oldest_open_age_seconds": 90_061,
        "snoozed_count": 3,
    }


# ---------------------------------------------------------------------------
# raise_drift_queue_backlog_alert — severity mapping + persisted threshold
# ---------------------------------------------------------------------------


def _stub_alerts_pipeline(monkeypatch):
    """Bypass dedupe + email so we can assert the persisted INSERT."""
    from app.notifications import alerts as _alerts

    async def _claim(*a, **k):
        return True
    monkeypatch.setattr(_alerts, "dedupe_or_skip", _claim)
    sent: list[dict] = []
    def _drift_email(**kw):
        sent.append(kw)
        return {}
    monkeypatch.setattr(_alerts, "drift", _drift_email)
    return sent


def test_raise_drift_queue_backlog_degraded_persists_warn_and_count(monkeypatch):
    """A `degraded` card status normalises to severity=`warn` and the
    persisted threshold reflects the count threshold (20), not 50."""
    from app.notifications import alerts as _alerts
    db = _install_db(monkeypatch)
    sent = _stub_alerts_pipeline(monkeypatch)

    raised = _run(_alerts.raise_drift_queue_backlog_alert(
        severity="degraded",
        open_count=25,
        oldest_open_age_seconds=0,
        snoozed_count=2,
    ))

    assert raised is True
    inserts = [(q, a) for q, a in db.executes if "model_health_alerts" in q]
    assert len(inserts) == 1
    args = inserts[0][1]
    # (severity, metric_name, metric_value, threshold, message)
    assert args[0] == "warn"
    assert args[1] == "open_count"
    assert args[2] == 25.0
    assert args[3] == 20.0
    # Email dispatch sees the same threshold pair.
    assert sent and sent[0]["threshold"] == 20.0
    assert sent[0]["value"] == 25.0


def test_raise_drift_queue_backlog_down_persists_high_and_count(monkeypatch):
    """A `down` card status normalises to severity=`high` with the
    50-row threshold attached."""
    from app.notifications import alerts as _alerts
    db = _install_db(monkeypatch)
    _stub_alerts_pipeline(monkeypatch)

    raised = _run(_alerts.raise_drift_queue_backlog_alert(
        severity="down",
        open_count=60,
        oldest_open_age_seconds=0,
        snoozed_count=0,
    ))

    assert raised is True
    inserts = [(q, a) for q, a in db.executes if "model_health_alerts" in q]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert args[0] == "high"
    assert args[1] == "open_count"
    assert args[2] == 60.0
    assert args[3] == 50.0


def test_raise_drift_queue_backlog_age_trigger_persists_age_dimension(
    monkeypatch,
):
    """When only the oldest-open age trips degraded, the persisted
    metric_name + threshold reflect the AGE dimension, not open_count."""
    from app.notifications import alerts as _alerts
    db = _install_db(monkeypatch)
    _stub_alerts_pipeline(monkeypatch)

    raised = _run(_alerts.raise_drift_queue_backlog_alert(
        severity="degraded",
        open_count=2,
        oldest_open_age_seconds=24 * 3600 + 600,
        snoozed_count=0,
    ))

    assert raised is True
    inserts = [(q, a) for q, a in db.executes if "model_health_alerts" in q]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert args[0] == "warn"
    assert args[1] == "oldest_open_age_seconds"
    assert args[2] == float(24 * 3600 + 600)
    assert args[3] == float(24 * 3600)


def test_raise_drift_queue_backlog_age_trigger_down_uses_72h(monkeypatch):
    """Age-only down trigger persists the 72h threshold, not 24h."""
    from app.notifications import alerts as _alerts
    db = _install_db(monkeypatch)
    _stub_alerts_pipeline(monkeypatch)

    _run(_alerts.raise_drift_queue_backlog_alert(
        severity="down",
        open_count=1,
        oldest_open_age_seconds=72 * 3600 + 60,
        snoozed_count=0,
    ))
    inserts = [(q, a) for q, a in db.executes if "model_health_alerts" in q]
    args = inserts[0][1]
    assert args[0] == "high"
    assert args[1] == "oldest_open_age_seconds"
    assert args[3] == float(72 * 3600)


def test_raise_drift_queue_backlog_dedupe_skips_insert(monkeypatch):
    """When dedupe says "already claimed", no INSERT and no email."""
    from app.notifications import alerts as _alerts
    db = _install_db(monkeypatch)

    async def _no_claim(*a, **k):
        return False
    monkeypatch.setattr(_alerts, "dedupe_or_skip", _no_claim)
    sent: list[dict] = []
    monkeypatch.setattr(
        _alerts, "drift", lambda **kw: sent.append(kw) or {}
    )

    raised = _run(_alerts.raise_drift_queue_backlog_alert(
        severity="degraded",
        open_count=25,
        oldest_open_age_seconds=0,
        snoozed_count=0,
    ))
    assert raised is False
    assert sent == []
    assert all("model_health_alerts" not in q for q, _ in db.executes)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stub_async(*, return_value=None):
    async def _f(*a, **kw):
        return return_value
    return _f


# ---------------------------------------------------------------------------
# (exchange, ticker) namespace separation (task #715) — Polymarket can
# reuse the same drift-resolution table without colliding with Kalshi.
# ---------------------------------------------------------------------------


def test_upsert_namespaces_by_exchange(monkeypatch):
    """``(kalshi, MKT-A)`` and ``(polymarket, MKT-A)`` are independent
    active rows. The fetchrow that picks the active row must filter
    on BOTH exchange and ticker — otherwise the polymarket call would
    pick up the kalshi row and clobber it.
    """
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    # Polymarket sees no active row → INSERT. The Kalshi row that
    # exists is irrelevant because it's filtered out by exchange.
    db.fetchrow_responses.append(None)

    state = _run(reconciler._upsert_drift_resolution(
        ticker="MKT-A", vertical=None,
        local_qty=0, exchange_qty=4, drift=4,
        exchange="polymarket",
    ))

    assert state == "open"
    # Active-row lookup is scoped to (exchange, ticker).
    select_calls = [
        (q, a) for q, a in db.fetchrows
        if "FROM position_drift_resolutions" in q
    ]
    assert select_calls, "expected fetchrow on position_drift_resolutions"
    q, args = select_calls[0]
    assert "exchange = $1" in q
    assert "ticker = $2" in q
    assert args[0] == "polymarket" and args[1] == "MKT-A"

    # The INSERT carries the exchange in the column list.
    insert_calls = [
        (q, a) for q, a in db.executes
        if "INSERT INTO position_drift_resolutions" in q
    ]
    assert insert_calls
    q, args = insert_calls[0]
    assert "exchange" in q
    assert args[0] == "polymarket"
    assert args[1] == "MKT-A"


def test_accept_truth_namespaces_by_exchange(monkeypatch):
    """``accept_exchange_truth(exchange='polymarket')`` selects the
    polymarket row, closes ONLY polymarket positions, and tags the
    synth row with the exchange-prefixed audit id so it can't collide
    with a kalshi accept-truth row on the same ticker.
    """
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append({
        "id": 99, "local_qty": 0, "exchange_qty": 7, "drift_qty": 7,
    })

    res = _run(reconciler.accept_exchange_truth(
        ticker="MKT-A", resolver_email="ops@axiom.dev",
        exchange="polymarket",
    ))

    assert res["changed"] is True
    # Active-row lookup scoped to polymarket.
    select_q, select_args = next(
        (q, a) for q, a in db.fetchrows
        if "FROM position_drift_resolutions" in q
    )
    assert select_args[0] == "polymarket" and select_args[1] == "MKT-A"
    # UPDATE positions filter is also scoped to polymarket so we don't
    # close kalshi rows for the same market_id.
    close_calls = [
        (q, a) for q, a in db.executes
        if "UPDATE positions" in q and "status = 'closed'" in q
    ]
    assert close_calls
    _, close_args = close_calls[0]
    assert close_args[1] == "polymarket"
    # Synthetic correction row is tagged with the exchange so it
    # cannot collide on positions.order_id with the kalshi tag.
    synth_inserts = [
        a for q, a in db.executes
        if "INSERT INTO positions" in q
    ]
    assert synth_inserts
    assert synth_inserts[0][0] == "recon-accept-truth-polymarket-MKT-A"
    assert synth_inserts[0][4] == "polymarket"


def test_orphan_skeleton_tags_non_kalshi_with_exchange(monkeypatch):
    """The orphan synth-id is exchange-prefixed for non-kalshi venues
    so ``positions.order_id`` stays unique across exchanges.
    """
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)

    ok = _run(reconciler._create_orphan_skeleton(
        ticker="MKT-A", exchange_qty=3, vertical=None,
        exchange="polymarket",
    ))
    assert ok is True
    inserts = [a for q, a in db.executes if "INSERT INTO positions" in q]
    assert inserts
    assert inserts[0][0] == "recon-orphan-polymarket-MKT-A"
    assert inserts[0][5] == "polymarket"
    # Kalshi-default keeps the legacy unprefixed shape so existing
    # rows / audit queries don't break.
    assert reconciler._orphan_synth_id("MKT-A", "kalshi") == "recon-orphan-MKT-A"


def test_self_heal_scoped_to_exchange(monkeypatch):
    """A quiet kalshi cycle must NOT sweep polymarket open rows —
    ``_self_heal_resolutions`` is per-exchange.
    """
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    _run(reconciler._self_heal_resolutions({"MKT-A"}, exchange="polymarket"))

    sweep_calls = [
        (q, a) for q, a in db.executes
        if "resolved_self_healed" in q
    ]
    assert sweep_calls
    q, args = sweep_calls[0]
    assert "exchange = $2" in q
    assert args[1] == "polymarket"


def test_list_drifts_filters_by_exchange(monkeypatch):
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)

    _run(reconciler.list_drifts(state="open", exchange="polymarket"))

    qcalls = [(q, a) for q, a in db.fetches
              if "FROM position_drift_resolutions" in q]
    assert qcalls
    q, args = qcalls[0]
    assert "pdr.exchange = $2" in q
    assert args[1] == "polymarket"


def test_accept_truth_acks_alerts_with_namespaced_metric_name(monkeypatch):
    """Polymarket resolution must ack the ``polymarket:<ticker>``
    alert rows, NOT the bare-ticker rows (which belong to kalshi).
    Without this the health banner would stay red after a successful
    polymarket resolution.
    """
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchrow_responses.append({
        "id": 7, "local_qty": 0, "exchange_qty": 3, "drift_qty": 3,
    })
    _run(reconciler.accept_exchange_truth(
        ticker="MKT-A", resolver_email="ops@axiom.dev",
        exchange="polymarket",
    ))
    ack_calls = [
        (q, a) for q, a in db.executes
        if "UPDATE model_health_alerts" in q and "acknowledged = true" in q
    ]
    assert ack_calls, "expected an alert-ack UPDATE"
    _, args = ack_calls[0]
    assert args[1] == "polymarket:MKT-A"


def test_record_auto_correction_namespaces_metric_name(monkeypatch):
    """Within-tolerance INFO rows for non-kalshi venues must use the
    namespaced metric_name so they don't appear in the kalshi
    last_alert_* enrichment join.
    """
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    _run(reconciler._record_auto_correction(
        ticker="MKT-A", local_qty=0, exchange_qty=2,
        drift=2, tolerance=5, exchange="polymarket",
    ))
    inserts = [
        (q, a) for q, a in db.executes
        if "INSERT INTO model_health_alerts" in q
    ]
    assert inserts
    _, args = inserts[0]
    assert args[0] == "polymarket:MKT-A"


def test_raise_drift_alert_namespaces_metric_name(monkeypatch):
    """The DB dedupe lookup AND the INSERT both encode the exchange
    in metric_name so a kalshi 5-contract drift on MKT-A doesn't
    suppress a polymarket 5-contract drift on MKT-A.
    """
    from app.kalshi import reconciler
    db = _install_db(monkeypatch)
    db.fetchval_responses.append(None)  # no recent dup → INSERT path
    _run(reconciler._raise_position_drift_alert(
        ticker="MKT-A", local_qty=0, exchange_qty=5,
        drift=5, tolerance=2, exchange="polymarket",
    ))
    dedupe_lookups = [
        (q, a) for q, a in db.fetchvals
        if "FROM model_health_alerts" in q
    ]
    assert dedupe_lookups
    _, args = dedupe_lookups[0]
    assert args[0] == "polymarket:MKT-A"
    inserts = [
        (q, a) for q, a in db.executes
        if "INSERT INTO model_health_alerts" in q
    ]
    assert inserts
    _, args = inserts[0]
    # vertical, severity, metric_name, ...
    assert args[2] == "polymarket:MKT-A"


def test_diff_positions_uses_exchange_for_local_query(monkeypatch):
    """Polymarket's diff loop queries positions filtered to
    ``coalesce(exchange,'kalshi')='polymarket'`` — without this scope
    a polymarket reconciler tick would treat kalshi positions as its
    own and double-flag them as drift.
    """
    from app.kalshi import reconciler
    monkeypatch.delenv("AXIOM_RECONCILER_RESOLUTION_ENABLED", raising=False)
    db = _install_db(monkeypatch)
    db.fetch_responses.append([])  # empty positions for polymarket

    monkeypatch.setattr(
        reconciler, "_raise_position_drift_alert",
        _stub_async(return_value=True),
    )

    _run(reconciler._diff_positions(
        [_FakePos("MKT-A", 5)], exchange="polymarket",
    ))

    pos_reads = [(q, a) for q, a in db.fetches
                 if "FROM positions" in q]
    assert pos_reads
    q, args = pos_reads[0]
    assert "coalesce(exchange, 'kalshi') = $1" in q
    assert args[0] == "polymarket"
