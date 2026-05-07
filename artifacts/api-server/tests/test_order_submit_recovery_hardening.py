"""Tests for the task #710 order-submit recovery hardening.

Layers under test:

  * ``app.kalshi.lifecycle._record_recovery_attempt`` — sidecar upsert
    that drives the threshold alert and powers the dead-letter queue.
  * ``app.kalshi.dead_letter_sweeper.sweep_once`` — promotes long-failing
    rows into the dead-letter queue.
  * ``GET /api/admin/orders/dead-lettered`` — operator console list.
  * ``POST /api/admin/orders/{client_order_id}/resolve`` — operator
    actions (mark_cancelled / force_retry / claim_filled), each with
    its own audit trail and (for claim_filled) order_fills + position
    rollup side-effects.

The unit layers (lifecycle + sweeper) monkeypatch the ``db`` module so
the tests don't need a live Postgres. The HTTP layer monkeypatches
``db.fetch`` / ``db.fetchrow`` / ``db.execute`` plus the lifecycle's
fill / position rollup helpers, and stubs ``require_admin`` via
``app.dependency_overrides`` (mirrors test_admin_reconciler_endpoints).

Existing ``test_order_submit_recovery.py`` continues to cover the six
recovery branches end-to-end and stays green — those tests already
exercise ``_record_recovery_attempt`` indirectly because every
recovery branch calls it; the helper fails open on DB error so the
existing recovery suite isn't perturbed.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthedUser, require_admin
from app.kalshi import dead_letter_sweeper as dls
from app.kalshi import lifecycle
from app.kalshi.bus import bus
from app.main import app


# ---------------------------------------------------------------------------
# Helpers (mirror test_order_submit_recovery.py)
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _mk_req(coid: str = "coid-x") -> lifecycle.OrderRequest:
    return lifecycle.OrderRequest(
        market_ticker="MKT-1",
        side="yes",
        action="buy",
        count=3,
        limit_price_cents=55,
        client_order_id=coid,
    )


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Clean-slate the in-memory state between every test so counters,
    dedupe gates, and sweeper task handles don't leak across cases."""
    lifecycle._resting.clear()
    lifecycle._kill._halted = False
    lifecycle._kill._reason = None
    lifecycle._kill._at = None
    lifecycle._resting_lock = asyncio.Lock()
    lifecycle._kill._lock = asyncio.Lock()
    bus._lock = asyncio.Lock()
    bus._subs.clear()
    lifecycle._reset_submit_recovery_state()
    dls._reset_state()
    # Make the threshold deterministic — every helper test wants 3
    # so we don't have to fire 5 successive failures by hand.
    monkeypatch.setenv("AXIOM_ORDER_RECOVERY_ATTEMPT_THRESHOLD", "3")
    yield


# ---------------------------------------------------------------------------
# _record_recovery_attempt: failure path increments + threshold alert
# ---------------------------------------------------------------------------


class _FakeDB:
    """Minimal in-memory ``db`` replacement: stores rows keyed by coid
    and returns RETURNING-shaped results from the upsert."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrows: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executes.append((sql, args))
        sql_compact = " ".join(sql.split())
        if sql_compact.startswith("UPDATE order_recovery_attempts SET resolved_at"):
            coid, action = args[0], args[1]
            row = self.rows.get(coid)
            if row and row.get("resolved_at") is None:
                # Always record the recovery outcome.
                row["last_recovery_at"] = "now"
                row["last_recovery_outcome"] = action
                # CASE: only auto-resolve rows that aren't dead-lettered.
                # Once paged, only an explicit operator click closes
                # the loop (see lifecycle.py for rationale).
                if row.get("dead_lettered_at") is None:
                    row["resolved_at"] = "now"
                    row["resolution_action"] = action
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.fetches.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrows.append((sql, args))
        sql_compact = " ".join(sql.split())
        if sql_compact.startswith("INSERT INTO order_recovery_attempts"):
            (
                coid, market, side, action, contracts,
                limit_price, vertical, outcome, error,
            ) = args
            existing = self.rows.get(coid)
            if existing is None:
                row = {
                    "client_order_id": coid,
                    "market_ticker": market,
                    "side": side,
                    "action": action,
                    "contracts": contracts,
                    "limit_price_cents": limit_price,
                    "vertical": vertical,
                    "recovery_attempts": 1,
                    "last_recovery_outcome": outcome,
                    "last_error": error,
                    "resolved_at": None,
                    "dead_lettered_at": None,
                }
                self.rows[coid] = row
            else:
                # Mirror the real ON CONFLICT CASE logic: a new failure
                # against a previously-resolved row (force_retry path)
                # is a fresh start — counter resets to 1 and the
                # resolution / dead-letter columns are cleared.
                if existing.get("resolved_at") is not None:
                    existing["recovery_attempts"] = 1
                    existing["resolved_at"] = None
                    existing["resolution_action"] = None
                    existing["resolver_email"] = None
                    existing["resolution_reason"] = None
                    existing["fill_price_cents"] = None
                    existing["dead_lettered_at"] = None
                else:
                    existing["recovery_attempts"] = (
                        int(existing["recovery_attempts"]) + 1
                    )
                existing["last_recovery_outcome"] = outcome
                existing["last_error"] = error
                row = existing
            return row
        if sql_compact.startswith("SELECT") and "order_recovery_attempts" in sql_compact:
            coid = args[0]
            return self.rows.get(coid)
        return None


def _install_fake_db(monkeypatch, fake: _FakeDB) -> None:
    monkeypatch.setattr(lifecycle.db, "execute", fake.execute)
    monkeypatch.setattr(lifecycle.db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(lifecycle.db, "fetch", fake.fetch)


def test_record_recovery_attempt_first_failure_inserts(monkeypatch):
    fake = _FakeDB()
    _install_fake_db(monkeypatch, fake)

    _run(
        lifecycle._record_recovery_attempt(
            _mk_req("coid-A"), outcome="not_found", error="missing",
        )
    )
    row = fake.rows["coid-A"]
    assert row["recovery_attempts"] == 1
    assert row["last_recovery_outcome"] == "not_found"
    assert row["last_error"] == "missing"
    # No fetchrow returns extra fields, so the caller's threshold check
    # uses the row we returned above.


def test_record_recovery_attempt_increments_on_repeat(monkeypatch):
    fake = _FakeDB()
    _install_fake_db(monkeypatch, fake)
    for _ in range(2):
        _run(
            lifecycle._record_recovery_attempt(
                _mk_req("coid-B"), outcome="lookup_failed", error="boom",
            )
        )
    assert fake.rows["coid-B"]["recovery_attempts"] == 2


def test_record_recovery_attempt_fires_threshold_alert(monkeypatch):
    """Cross the threshold (env=3) once → exactly one
    ``order_submit_stuck`` alert dispatched. Subsequent attempts at or
    above the threshold are absorbed by the dispatch dedupe inside
    :func:`_send_order_submit_stuck_alert`.
    """
    fake = _FakeDB()
    _install_fake_db(monkeypatch, fake)
    fired: list[dict[str, Any]] = []

    async def _capture(snap):
        fired.append(snap)

    monkeypatch.setattr(lifecycle, "_send_order_submit_stuck_alert", _capture)

    async def _drive():
        # Attempts 1 and 2: below threshold → no dispatch.
        for _ in range(2):
            await lifecycle._record_recovery_attempt(
                _mk_req("coid-C"), outcome="not_found", error="x",
            )
        # Attempt 3: crosses threshold → exactly one dispatch.
        await lifecycle._record_recovery_attempt(
            _mk_req("coid-C"), outcome="not_found", error="x",
        )
        # Let the create_task() scheduled inside _record_recovery_attempt
        # run before we assert.
        await asyncio.sleep(0)
        # Attempt 4: still above threshold → dispatcher would normally
        # dedupe; here we capture it pre-dedupe but it's expected.
        await lifecycle._record_recovery_attempt(
            _mk_req("coid-C"), outcome="not_found", error="x",
        )
        await asyncio.sleep(0)

    _run(_drive())
    # Threshold-crossing attempts (3 and 4) each call the dispatcher;
    # the real dispatcher dedupes via dedupe_or_skip. We assert the
    # *snapshot shape* on the first dispatch.
    assert len(fired) >= 1
    snap = fired[0]
    assert snap["client_order_id"] == "coid-C"
    assert snap["recovery_attempts"] >= 3
    assert snap["threshold"] == 3
    assert snap["market_ticker"] == "MKT-1"
    assert snap["last_recovery_outcome"] == "not_found"


def test_record_recovery_attempt_success_resolves_open_row(monkeypatch):
    """A success outcome on a previously-failing coid clears
    ``resolved_at`` so the sweeper never promotes it. This is what
    keeps a flapping coid out of the dead-letter queue."""
    fake = _FakeDB()
    _install_fake_db(monkeypatch, fake)
    # Seed a failing row.
    _run(
        lifecycle._record_recovery_attempt(
            _mk_req("coid-D"), outcome="not_found", error="x",
        )
    )
    assert fake.rows["coid-D"]["resolved_at"] is None
    # Now a recovered_resting outcome on the same coid.
    _run(
        lifecycle._record_recovery_attempt(
            _mk_req("coid-D"), outcome="recovered_resting", error=None,
        )
    )
    assert fake.rows["coid-D"]["resolved_at"] == "now"
    assert fake.rows["coid-D"]["resolution_action"] == "recovered_resting"


def test_record_recovery_attempt_db_error_is_swallowed(monkeypatch):
    """The recovery path must never throw because of a sidecar DB
    failure — the underlying recovery branch already has its own
    audit trail via ``_write_lifecycle``."""
    async def _boom(*args, **kwargs):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(lifecycle.db, "execute", _boom)
    monkeypatch.setattr(lifecycle.db, "fetchrow", _boom)
    monkeypatch.setattr(lifecycle.db, "fetch", _boom)

    # Should not raise.
    _run(
        lifecycle._record_recovery_attempt(
            _mk_req("coid-E"), outcome="not_found", error="x",
        )
    )


# ---------------------------------------------------------------------------
# dead_letter_sweeper.sweep_once
# ---------------------------------------------------------------------------


def test_sweep_once_marks_eligible_rows(monkeypatch):
    """The UPDATE … RETURNING returns the rows the sweeper just
    promoted. Counters and ``last_marked_at`` advance on a hit."""
    captured: list[tuple[str, tuple[Any, ...]]] = []

    async def _fake_fetch(sql, *args):
        captured.append((sql, args))
        return [
            {
                "client_order_id": "coid-promoted",
                "market_ticker": "MKT-1",
                "side": "yes",
                "action": "buy",
                "contracts": 3,
                "limit_price_cents": 55,
                "vertical": "crypto",
                "recovery_attempts": 7,
                "last_recovery_outcome": "not_found",
                "last_error": "no exchange row",
                "first_failed_at": "ago",
                "dead_lettered_at": "now",
            }
        ]

    monkeypatch.setattr(dls.db, "fetch", _fake_fetch)
    rows = _run(dls.sweep_once())
    assert len(rows) == 1
    assert rows[0]["client_order_id"] == "coid-promoted"
    s = dls.state()
    assert s["scans"] == 1
    assert s["marked"] == 1
    assert s["last_marked_at"] is not None
    # Threshold value was passed as the first positional arg.
    assert captured and captured[0][1][0] == 600.0


def test_sweep_once_db_error_increments_errors(monkeypatch):
    async def _boom(sql, *args):
        raise RuntimeError("conn refused")

    monkeypatch.setattr(dls.db, "fetch", _boom)
    rows = _run(dls.sweep_once())
    assert rows == []
    s = dls.state()
    assert s["errors"] == 1
    assert s["last_error"] is not None


def test_sweep_once_no_rows(monkeypatch):
    async def _empty(sql, *args):
        return []

    monkeypatch.setattr(dls.db, "fetch", _empty)
    rows = _run(dls.sweep_once())
    assert rows == []
    s = dls.state()
    assert s["scans"] == 1
    assert s["marked"] == 0
    assert s["last_marked_at"] is None


def test_sweep_dispatch_alerts_invokes_alert_per_row(monkeypatch):
    """One ``order_dead_lettered`` alert per coid; the alert call
    receives the per-row recovery counters and last_error."""
    fired: list[dict[str, Any]] = []

    def _capture(**kwargs):
        fired.append(kwargs)

    from app.notifications import alerts as _alerts
    monkeypatch.setattr(_alerts, "order_dead_lettered", _capture)

    rows = [
        {
            "client_order_id": f"coid-{i}",
            "market_ticker": f"MKT-{i}",
            "side": "yes",
            "action": "buy",
            "contracts": i + 1,
            "limit_price_cents": 50 + i,
            "vertical": "crypto",
            "recovery_attempts": 8,
            "last_recovery_outcome": "lookup_failed",
            "last_error": "timeout",
        }
        for i in range(2)
    ]
    _run(dls._dispatch_alerts(rows))
    assert len(fired) == 2
    assert {f["client_order_id"] for f in fired} == {"coid-0", "coid-1"}
    assert fired[0]["recovery_attempts"] == 8
    assert fired[0]["last_error"] == "timeout"


# ---------------------------------------------------------------------------
# HTTP: GET /api/admin/orders/dead-lettered
# ---------------------------------------------------------------------------


def _admin_user() -> AuthedUser:
    return AuthedUser(
        user_id="user_admin_test",
        session_id="sess_admin_test",
        claims={
            "sub": "user_admin_test",
            "email": "ops@axiom-predict.com",
        },
    )


@pytest.fixture
def override_admin():
    user = _admin_user()
    app.dependency_overrides[require_admin] = lambda: user
    yield user
    app.dependency_overrides.pop(require_admin, None)


def test_dead_lettered_get_requires_auth():
    client = TestClient(app)
    resp = client.get("/api/admin/orders/dead-lettered")
    assert resp.status_code == 401


def test_dead_lettered_get_admin_happy_path(override_admin, monkeypatch):
    """``state=open`` is the default; rows come back with the queue
    contract the UI consumes (snake_case, ISO timestamps, age_seconds)."""
    captured: list[tuple[str, tuple[Any, ...]]] = []

    async def _fake_fetch(sql, *args):
        captured.append((sql, args))
        # Mimic asyncpg's Record: a Mapping with `.get()` and dict()
        # conversion. Plain dict satisfies both interfaces in the
        # endpoint's iteration.
        return [
            {
                "client_order_id": "coid-1",
                "market_ticker": "KXBTC-FAKE",
                "side": "yes",
                "action": "buy",
                "contracts": 3,
                "limit_price_cents": 55,
                "vertical": "crypto",
                "recovery_attempts": 6,
                "last_recovery_at": None,
                "last_recovery_outcome": "lookup_failed",
                "last_error": "timeout",
                "first_failed_at": None,
                "dead_lettered_at": None,
                "resolved_at": None,
                "resolution_action": None,
                "resolver_email": None,
                "resolution_reason": None,
                "fill_price_cents": None,
                "created_at": None,
                "updated_at": None,
                "age_seconds": 1234,
            }
        ]

    from app import main as _m
    monkeypatch.setattr(_m.db, "fetch", _fake_fetch)
    client = TestClient(app)
    resp = client.get("/api/admin/orders/dead-lettered")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "open"
    assert body["items"][0]["client_order_id"] == "coid-1"
    assert body["items"][0]["recovery_attempts"] == 6
    # Default state filter ('open') drives the WHERE clause to the
    # dead_lettered + unresolved partition.
    assert "dead_lettered_at IS NOT NULL" in captured[0][0]
    assert "resolved_at IS NULL" in captured[0][0]


def test_dead_lettered_get_rejects_invalid_state(override_admin):
    client = TestClient(app)
    resp = client.get("/api/admin/orders/dead-lettered?state=bogus")
    assert resp.status_code == 400


def test_dead_lettered_get_db_error_returns_empty(override_admin, monkeypatch):
    async def _boom(sql, *args):
        raise RuntimeError("conn refused")

    from app import main as _m
    monkeypatch.setattr(_m.db, "fetch", _boom)
    client = TestClient(app)
    resp = client.get("/api/admin/orders/dead-lettered")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert "error" in body


# ---------------------------------------------------------------------------
# HTTP: POST /api/admin/orders/{client_order_id}/resolve
# ---------------------------------------------------------------------------


@pytest.fixture
def resolve_db(monkeypatch):
    """In-memory state for the resolve endpoint. Holds the open
    dead-letter row by coid; collects every UPDATE / INSERT for
    assertion (audit row, fill row, etc.).
    """
    state: dict[str, Any] = {
        "rows": {
            "coid-1": {
                "client_order_id": "coid-1",
                "market_ticker": "KXBTC-FAKE",
                "side": "yes",
                "action": "buy",
                "contracts": 3,
                "limit_price_cents": 55,
                "vertical": "crypto",
                "recovery_attempts": 7,
                "last_recovery_outcome": "lookup_failed",
                "last_error": "timeout",
                "dead_lettered_at": "2026-04-28T12:00:00Z",
                "resolved_at": None,
            },
            "coid-failing": {
                # Not yet promoted (dead_lettered_at IS NULL) — only
                # force_retry is allowed against this row.
                "client_order_id": "coid-failing",
                "market_ticker": "KXBTC-FAKE",
                "side": "yes",
                "action": "buy",
                "contracts": 3,
                "limit_price_cents": 55,
                "vertical": "crypto",
                "recovery_attempts": 2,
                "last_recovery_outcome": "lookup_failed",
                "last_error": "timeout",
                "dead_lettered_at": None,
                "resolved_at": None,
            },
            "coid-resolved": {
                "client_order_id": "coid-resolved",
                "market_ticker": "KXBTC-FAKE",
                "side": "yes",
                "action": "buy",
                "contracts": 3,
                "limit_price_cents": 55,
                "vertical": "crypto",
                "recovery_attempts": 9,
                "last_recovery_outcome": "lookup_failed",
                "last_error": "timeout",
                "dead_lettered_at": "2026-04-28T11:00:00Z",
                "resolved_at": "2026-04-28T11:30:00Z",
            },
        },
        "executes": [],
        "fill_calls": [],
        "rollup_calls": [],
    }

    async def _fetchrow(sql, *args):
        sql_compact = " ".join(sql.split())
        if sql_compact.startswith("SELECT") and "order_recovery_attempts" in sql_compact:
            coid = args[0]
            return state["rows"].get(coid)
        return None

    async def _execute(sql, *args):
        sql_compact = " ".join(sql.split())
        state["executes"].append((sql_compact, args))
        if sql_compact.startswith("UPDATE order_recovery_attempts"):
            coid, action, resolver, reason, price = args
            row = state["rows"].get(coid)
            if row and row.get("resolved_at") is None:
                row["resolved_at"] = "now"
                row["resolution_action"] = action
                row["resolver_email"] = resolver
                row["resolution_reason"] = reason
                row["fill_price_cents"] = price
                # asyncpg's command tag — endpoint enforces ==1 to
                # detect concurrent-resolver races.
                return "UPDATE 1"
            return "UPDATE 0"
        return "OK"

    async def _fake_fill(*, order_id, fill_id, count, price_cents,
                         source="ws", **_kwargs):
        # ``**_kwargs`` absorbs the resolve-path's ``strict=True``.
        state["fill_calls"].append({
            "order_id": order_id,
            "fill_id": fill_id,
            "count": count,
            "price_cents": price_cents,
            "source": source,
        })
        return True

    async def _fake_rollup(**kwargs):
        # ``strict`` and any future flags are stored alongside the
        # business kwargs so tests can assert them when they care.
        state["rollup_calls"].append(kwargs)
        return None

    from app import main as _m
    monkeypatch.setattr(_m.db, "fetchrow", _fetchrow)
    monkeypatch.setattr(_m.db, "execute", _execute)
    monkeypatch.setattr(lifecycle, "_record_fill_idempotent", _fake_fill)
    monkeypatch.setattr(lifecycle, "_upsert_position_rollup", _fake_rollup)
    # Squelch the alert dispatch — it's covered by the alert tests.
    async def _noop_alert(**kwargs):
        return None

    monkeypatch.setattr(_m, "_send_dead_letter_resolved_alert", _noop_alert)
    return state


def test_resolve_requires_auth():
    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={"action": "force_retry"},
    )
    assert resp.status_code == 401


def test_resolve_force_retry_happy_path(override_admin, resolve_db):
    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={"action": "force_retry", "reason": "upstream cleared"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["action"] == "force_retry"
    assert body["resolver_email"] == "ops@axiom-predict.com"
    # Sidecar row got resolved_at = 'now', resolution_action = force_retry.
    assert resolve_db["rows"]["coid-1"]["resolved_at"] == "now"
    assert resolve_db["rows"]["coid-1"]["resolution_action"] == "force_retry"
    # No fill insert / rollup for force_retry.
    assert resolve_db["fill_calls"] == []
    assert resolve_db["rollup_calls"] == []
    # Audit row written (UPDATE + INSERT into admin_audit_log).
    audit_inserts = [
        e for e in resolve_db["executes"]
        if e[0].startswith("INSERT INTO admin_audit_log")
    ]
    assert len(audit_inserts) == 1
    # Audit payload encodes coid + before/after as JSON.
    audit_args = audit_inserts[0][1]
    before = json.loads(audit_args[3])
    after = json.loads(audit_args[4])
    assert before["client_order_id"] == "coid-1"
    assert after["resolution_action"] == "force_retry"
    assert audit_args[2] == "order_dead_letter_resolve:force_retry"


def test_resolve_mark_cancelled_does_not_record_fill(override_admin, resolve_db):
    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={"action": "mark_cancelled", "reason": "support confirmed reject"},
    )
    assert resp.status_code == 200, resp.text
    assert resolve_db["rows"]["coid-1"]["resolution_action"] == "mark_cancelled"
    # Critically: NO fill or rollup for mark_cancelled — the position
    # rollup must stay clean.
    assert resolve_db["fill_calls"] == []
    assert resolve_db["rollup_calls"] == []


def test_resolve_claim_filled_writes_fill_and_rollup(override_admin, resolve_db):
    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={
            "action": "claim_filled",
            "reason": "venue confirmed by phone",
            "fill_price_cents": 47,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "claim_filled"
    # Fill row written with source='operator_manual' and the
    # operator-supplied price; quantity comes from the original intent.
    assert len(resolve_db["fill_calls"]) == 1
    fill = resolve_db["fill_calls"][0]
    assert fill["order_id"] == "coid-1"
    assert fill["count"] == 3
    assert fill["price_cents"] == 47
    assert fill["source"] == "operator_manual"
    # Deterministic per-coid fill_id — no millisecond suffix —
    # so concurrent ``claim_filled`` requests cannot insert distinct
    # duplicate fill rows (ON CONFLICT (order_id, fill_id) DO NOTHING).
    assert fill["fill_id"] == "manual:coid-1"
    # Position rollup updated with the same intent + price.
    assert len(resolve_db["rollup_calls"]) == 1
    rollup = resolve_db["rollup_calls"][0]
    assert rollup["order_id"] == "coid-1"
    assert rollup["market_id"] == "KXBTC-FAKE"
    assert rollup["side"] == "yes"
    assert rollup["quantity"] == 3
    assert rollup["avg_price_cents"] == 47
    # And the row is marked resolved with the price preserved for audit.
    assert resolve_db["rows"]["coid-1"]["fill_price_cents"] == 47


def test_resolve_claim_filled_requires_price(override_admin, resolve_db):
    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={"action": "claim_filled", "reason": "no price"},
    )
    assert resp.status_code == 400
    # Did not write the fill or update the row.
    assert resolve_db["fill_calls"] == []
    assert resolve_db["rows"]["coid-1"]["resolved_at"] is None


def test_resolve_rejects_invalid_action(override_admin, resolve_db):
    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={"action": "delete_forever"},
    )
    assert resp.status_code == 400
    assert resolve_db["rows"]["coid-1"]["resolved_at"] is None


def test_resolve_rejects_already_resolved(override_admin, resolve_db):
    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-resolved/resolve",
        json={"action": "force_retry"},
    )
    assert resp.status_code == 409
    # Endpoint short-circuited at the "already resolved" guard — it
    # must NOT have written resolution_action onto the row.
    assert resolve_db["rows"]["coid-resolved"].get("resolution_action") != "force_retry"


def test_resolve_409_when_concurrent_resolver_wins_update_race(
    monkeypatch, override_admin, resolve_db,
):
    """Race window: two operators (or a double-click) both pass the
    snapshot precheck (``resolved_at IS NULL`` at SELECT-time), but
    only one can win the conditional UPDATE (``WHERE resolved_at IS
    NULL``). The loser must NOT emit success/audit/alert — it must
    raise 409.

    We simulate by patching ``_execute`` to always report ``UPDATE 0``
    on the resolution UPDATE (i.e. the concurrent winner already
    flipped the row out from under us between SELECT and UPDATE).
    """
    from app import main as _m

    real_execute = _m.db.execute  # the resolve_db fixture's mock

    async def _race_execute(sql, *args):
        sql_compact = " ".join(sql.split())
        # Record the call so the test can still assert the SQL fired.
        resolve_db["executes"].append((sql_compact, args))
        if sql_compact.startswith("UPDATE order_recovery_attempts"):
            # Concurrent resolver beat us — pretend zero rows matched.
            return "UPDATE 0"
        # Non-resolution UPDATEs / INSERTs flow through the real fake.
        return await real_execute(sql, *args)

    monkeypatch.setattr(_m.db, "execute", _race_execute)

    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={"action": "force_retry", "reason": "racing"},
    )
    assert resp.status_code == 409, resp.text
    assert "concurrency" in (resp.json().get("detail") or "").lower()
    # The endpoint did fire the UPDATE — but rowcount==0 → no
    # success / audit / alert were emitted (the row is unchanged).
    assert any(
        e[0].startswith("UPDATE order_recovery_attempts")
        for e in resolve_db["executes"]
    )


def test_resolve_500_when_update_returns_unexpected_rowcount(
    monkeypatch, override_admin, resolve_db,
):
    """Contract check: only ``UPDATE 1`` is success. Any other
    rowcount (or a non-conforming command tag) is a data-integrity
    violation — coid is the PK so >1 should be impossible. We must
    fail closed (500) rather than silently emit success.
    """
    from app import main as _m

    real_execute = _m.db.execute

    async def _bad_tag_execute(sql, *args):
        sql_compact = " ".join(sql.split())
        resolve_db["executes"].append((sql_compact, args))
        if sql_compact.startswith("UPDATE order_recovery_attempts"):
            return "UPDATE 2"  # impossible — PK is coid
        return await real_execute(sql, *args)

    monkeypatch.setattr(_m.db, "execute", _bad_tag_execute)

    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={"action": "force_retry"},
    )
    assert resp.status_code == 500, resp.text
    assert "rowcount" in (resp.json().get("detail") or "").lower()


def test_resolve_claim_filled_409_when_fill_already_recorded(
    monkeypatch, override_admin, resolve_db,
):
    """Concurrent ``claim_filled`` race: the second request hits
    ON CONFLICT (order_id, fill_id) DO NOTHING in the helper, which
    returns False. The endpoint MUST bail with 409 BEFORE issuing
    the resolution UPDATE — otherwise the loser would race the
    winning row's resolver_email/reason/price.
    """
    from app.kalshi import lifecycle as _life

    async def _conflict_fill(**kwargs):
        # Concurrent claim_filled already wrote the row.
        return False

    monkeypatch.setattr(_life, "_record_fill_idempotent", _conflict_fill)

    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={
            "action": "claim_filled",
            "fill_price_cents": 47,
            "reason": "double-clicked",
        },
    )
    assert resp.status_code == 409, resp.text
    assert "already recorded" in (resp.json().get("detail") or "").lower()
    # No rollup write — the position state belongs to the winner.
    assert resolve_db["rollup_calls"] == []
    # No resolution UPDATE was issued.
    assert not any(
        e[0].startswith("UPDATE order_recovery_attempts")
        for e in resolve_db["executes"]
    )
    # And the row is still unresolved (winner will mark it).
    assert resolve_db["rows"]["coid-1"]["resolved_at"] is None


def test_resolve_claim_filled_skips_rollup_when_update_loses_race(
    monkeypatch, override_admin, resolve_db,
):
    """Position rollup must run ONLY after exclusive ownership of
    the resolution is confirmed. If the UPDATE rowcount check fails
    (concurrent winner), the rollup must NOT execute — otherwise
    the loser's price could overwrite the winner's position state.
    """
    from app import main as _m

    real_execute = _m.db.execute

    async def _race_execute(sql, *args):
        sql_compact = " ".join(sql.split())
        resolve_db["executes"].append((sql_compact, args))
        if sql_compact.startswith("UPDATE order_recovery_attempts"):
            return "UPDATE 0"  # Lost the race.
        return await real_execute(sql, *args)

    monkeypatch.setattr(_m.db, "execute", _race_execute)

    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={
            "action": "claim_filled",
            "fill_price_cents": 47,
            "reason": "racing",
        },
    )
    assert resp.status_code == 409
    # Fill was written (deterministic key — winner's row is the same).
    assert len(resolve_db["fill_calls"]) == 1
    # CRITICAL: rollup must NOT have run — that's the corruption
    # vector this test guards against.
    assert resolve_db["rollup_calls"] == []
    # Cross-action orphan guard: the manual fill we wrote before
    # the UPDATE must be DELETE'd on the lost-race branch so it
    # can't enter PnL as a phantom trade. Scoped to the
    # deterministic key + source='operator_manual'.
    # (The test wrapper records every call AND delegates to the
    # fixture's recorder, so the DELETE shows up twice — assert
    # the contents, not the exact count.)
    deletes = [
        e for e in resolve_db["executes"]
        if e[0].startswith("DELETE FROM order_fills")
    ]
    assert deletes, resolve_db["executes"]
    assert "operator_manual" in deletes[0][0]
    assert deletes[0][1] == ("coid-1", "manual:coid-1")


def _install_cross_action_winner(monkeypatch, resolve_db, winner_action):
    """Patch ``db.execute`` so the resolution UPDATE behaves as if a
    different operator already won the race with ``winner_action`` —
    i.e. the row is now resolved with that action and our UPDATE
    matches zero rows.
    """
    from app import main as _m

    real_execute = _m.db.execute

    async def _race_execute(sql, *args):
        sql_compact = " ".join(sql.split())
        resolve_db["executes"].append((sql_compact, args))
        if sql_compact.startswith("UPDATE order_recovery_attempts"):
            row = resolve_db["rows"].get(args[0])
            if row is not None:
                # Simulate the winner having already flipped the row.
                row["resolved_at"] = "now"
                row["resolution_action"] = winner_action
                row["resolver_email"] = "other-op@axiom-predict.com"
            return "UPDATE 0"
        return await real_execute(sql, *args)

    monkeypatch.setattr(_m.db, "execute", _race_execute)


@pytest.mark.parametrize("winner_action", ["force_retry", "mark_cancelled"])
def test_resolve_claim_filled_cleans_up_orphan_fill_on_cross_action_race(
    monkeypatch, override_admin, resolve_db, winner_action,
):
    """Cross-action concurrency: operator A clicks ``claim_filled``
    at the same instant operator B clicks ``force_retry`` (or
    ``mark_cancelled``). B wins the resolution UPDATE; A's
    pre-UPDATE manual-fill INSERT is now an orphan and would silently
    enter PnL as a phantom trade.

    The endpoint must compensate by DELETE'ing the orphan fill row
    by the deterministic ``(order_id, fill_id)`` key with a
    ``source='operator_manual'`` guard, then return 409. The winning
    resolution row stays untouched.
    """
    _install_cross_action_winner(monkeypatch, resolve_db, winner_action)

    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={
            "action": "claim_filled",
            "fill_price_cents": 47,
            "reason": "racing",
        },
    )
    assert resp.status_code == 409, resp.text
    # The fill INSERT happened (we got there before the UPDATE race)…
    assert len(resolve_db["fill_calls"]) == 1
    assert resolve_db["fill_calls"][0]["fill_id"] == "manual:coid-1"
    # …and the compensating DELETE fired with the deterministic key
    # and the source='operator_manual' guard so a legitimate WS fill
    # sharing the key would never be touched.
    deletes = [
        e for e in resolve_db["executes"]
        if e[0].startswith("DELETE FROM order_fills")
    ]
    assert deletes, resolve_db["executes"]
    assert "operator_manual" in deletes[0][0]
    assert deletes[0][1] == ("coid-1", "manual:coid-1")
    # Position rollup MUST NOT run on the loser path — that's the
    # other half of the phantom-trade vector.
    assert resolve_db["rollup_calls"] == []
    # The winner's resolution row stays as the winner left it; the
    # loser does not overwrite resolution_action / resolver_email.
    row = resolve_db["rows"]["coid-1"]
    assert row["resolution_action"] == winner_action
    assert row["resolver_email"] == "other-op@axiom-predict.com"


def test_resolve_claim_filled_orphan_cleanup_failure_still_returns_409(
    monkeypatch, override_admin, resolve_db,
):
    """If the compensating DELETE itself blows up (DB blip during
    cleanup), the endpoint must still surface 409 to the operator —
    the orphan row is logged loudly for manual reconciliation, but
    we never silently report success on a lost race.
    """
    from app import main as _m

    real_execute = _m.db.execute

    async def _race_then_boom(sql, *args):
        sql_compact = " ".join(sql.split())
        resolve_db["executes"].append((sql_compact, args))
        if sql_compact.startswith("UPDATE order_recovery_attempts"):
            return "UPDATE 0"
        if sql_compact.startswith("DELETE FROM order_fills"):
            raise RuntimeError("simulated cleanup outage")
        return await real_execute(sql, *args)

    monkeypatch.setattr(_m.db, "execute", _race_then_boom)

    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={
            "action": "claim_filled",
            "fill_price_cents": 47,
            "reason": "racing",
        },
    )
    assert resp.status_code == 409
    # The DELETE was attempted (and recorded in executes) even
    # though it raised — the handler logs and continues to 409.
    assert any(
        e[0].startswith("DELETE FROM order_fills")
        for e in resolve_db["executes"]
    )
    assert resolve_db["rollup_calls"] == []


def test_resolve_claim_filled_500s_when_fill_insert_fails(
    monkeypatch, override_admin, resolve_db,
):
    """Strict-mode regression: if the manual-fill INSERT raises (DB
    blip), the endpoint MUST NOT proceed to mark the row resolved.
    Without strict mode the helper would silently swallow the
    exception and the resolution UPDATE would still fire — leaving
    a resolved row with no fill recorded.
    """
    from app import main as _m
    from app.kalshi import lifecycle as _life

    async def _boom_fill(**kwargs):
        # Honor the strict contract: fail-closed when called from
        # the resolve path.
        if kwargs.get("strict"):
            raise RuntimeError("simulated DB outage")
        return True

    monkeypatch.setattr(_life, "_record_fill_idempotent", _boom_fill)
    # Re-bind on the main module since main.py imports lifecycle as _life.
    monkeypatch.setattr(_m, "_send_dead_letter_resolved_alert",
                        lambda **k: None)

    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/coid-1/resolve",
        json={
            "action": "claim_filled",
            "fill_price_cents": 47,
            "reason": "venue confirmed",
        },
    )
    assert resp.status_code == 500
    # CRITICAL: row must still be unresolved — the dead-letter
    # state survives the failed write so the operator can retry.
    assert resolve_db["rows"]["coid-1"]["resolved_at"] is None
    # And no resolution UPDATE was issued.
    assert not any(
        e[0].startswith("UPDATE order_recovery_attempts")
        for e in resolve_db["executes"]
    )


def test_resolve_404_when_no_recovery_row(override_admin, resolve_db):
    client = TestClient(app)
    resp = client.post(
        "/api/admin/orders/never-existed/resolve",
        json={"action": "force_retry"},
    )
    assert resp.status_code == 404


def test_resolve_failing_row_only_allows_force_retry(override_admin, resolve_db):
    """A row that's failing recovery but hasn't been dead-lettered yet
    can be short-circuited with force_retry, but not with the
    state-changing actions."""
    client = TestClient(app)

    resp = client.post(
        "/api/admin/orders/coid-failing/resolve",
        json={"action": "mark_cancelled"},
    )
    assert resp.status_code == 409

    resp = client.post(
        "/api/admin/orders/coid-failing/resolve",
        json={"action": "claim_filled", "fill_price_cents": 50},
    )
    assert resp.status_code == 409

    # Force-retry against the same row works.
    resp = client.post(
        "/api/admin/orders/coid-failing/resolve",
        json={"action": "force_retry"},
    )
    assert resp.status_code == 200
    assert resolve_db["rows"]["coid-failing"]["resolution_action"] == "force_retry"


# ---------------------------------------------------------------------------
# Dead-letter guard: natural recovery on a paged row stays open
# ---------------------------------------------------------------------------


def test_record_recovery_attempt_does_not_resolve_dead_lettered_row(monkeypatch):
    """Once a row is dead-lettered (operator paged), a natural recovery
    outcome from a later orchestrator pass MUST NOT auto-resolve it.

    Rationale: the operator has been alerted and is investigating.
    Auto-resolution would silently drop the row from the operator
    console mid-investigation — they wouldn't know whether the venue
    actually reconciled (in which case any manual claim_filled would
    create a duplicate fill) or whether the row simply vanished. The
    recovery outcome IS recorded in ``last_recovery_outcome`` so the
    operator sees that it healed naturally and can choose
    ``mark_cancelled`` or ``force_retry`` accordingly.
    """
    fake = _FakeDB()
    _install_fake_db(monkeypatch, fake)
    # Seed a dead-lettered row directly (skip the failure→sweep cycle
    # since the sweeper isn't under test here).
    fake.rows["coid-DL"] = {
        "client_order_id": "coid-DL",
        "market_ticker": "MKT-1",
        "side": "yes",
        "action": "buy",
        "contracts": 3,
        "limit_price_cents": 55,
        "vertical": "crypto",
        "recovery_attempts": 7,
        "last_recovery_outcome": "not_found",
        "last_error": "no exchange row",
        "resolved_at": None,
        "dead_lettered_at": "now",
    }

    _run(
        lifecycle._record_recovery_attempt(
            _mk_req("coid-DL"), outcome="recovered_filled", error=None,
        )
    )
    row = fake.rows["coid-DL"]
    # CRITICAL: dead-lettered row stays open (operator must click).
    assert row["resolved_at"] is None
    assert row["dead_lettered_at"] == "now"
    # The recovery outcome is recorded so the operator console can
    # render "actually recovered as recovered_filled".
    assert row["last_recovery_outcome"] == "recovered_filled"


def test_record_recovery_attempt_resolves_non_dead_lettered_row(monkeypatch):
    """Counter-test for the dead-letter guard: a row that has NOT been
    dead-lettered DOES auto-resolve on a recovery outcome — the original
    'flapping coid never lands in the dead-letter queue' invariant
    must still hold for non-paged rows."""
    fake = _FakeDB()
    _install_fake_db(monkeypatch, fake)
    fake.rows["coid-OK"] = {
        "client_order_id": "coid-OK",
        "market_ticker": "MKT-1",
        "side": "yes",
        "action": "buy",
        "contracts": 3,
        "limit_price_cents": 55,
        "vertical": "crypto",
        "recovery_attempts": 2,
        "last_recovery_outcome": "not_found",
        "last_error": "x",
        "resolved_at": None,
        "dead_lettered_at": None,  # NOT dead-lettered
    }

    _run(
        lifecycle._record_recovery_attempt(
            _mk_req("coid-OK"), outcome="recovered_filled", error=None,
        )
    )
    assert fake.rows["coid-OK"]["resolved_at"] == "now"
    assert fake.rows["coid-OK"]["resolution_action"] == "recovered_filled"


# ---------------------------------------------------------------------------
# force_retry: counter resets on next failure
# ---------------------------------------------------------------------------


def test_force_retry_resets_recovery_attempts_on_next_failure(monkeypatch):
    """``force_retry`` marks the recovery row resolved. The contract
    is that a SUBSEQUENT failure for the same coid starts fresh — i.e.
    ``recovery_attempts`` resets to 1 — instead of resuming from the
    pre-resolution count. Otherwise force_retry would page the operator
    again on the very next failure (since the count would already be
    above the threshold).
    """
    fake = _FakeDB()
    _install_fake_db(monkeypatch, fake)
    # Seed a resolved row (post-force_retry state) with a high count.
    fake.rows["coid-FR"] = {
        "client_order_id": "coid-FR",
        "market_ticker": "MKT-1",
        "side": "yes",
        "action": "buy",
        "contracts": 3,
        "limit_price_cents": 55,
        "vertical": "crypto",
        "recovery_attempts": 7,
        "last_recovery_outcome": "not_found",
        "last_error": "x",
        "resolved_at": "now",
        "resolution_action": "force_retry",
        "dead_lettered_at": "now",  # was paged before retry
    }

    _run(
        lifecycle._record_recovery_attempt(
            _mk_req("coid-FR"), outcome="not_found", error="still missing",
        )
    )
    row = fake.rows["coid-FR"]
    # Counter reset → next failure starts at 1, not 8.
    assert row["recovery_attempts"] == 1
    # Resolution columns cleared so the row is "open" again.
    assert row["resolved_at"] is None
    assert row["resolution_action"] is None
    # Dead-letter cleared too — the coid gets a fresh chance to either
    # recover naturally OR re-cross the threshold and re-page.
    assert row["dead_lettered_at"] is None


# ---------------------------------------------------------------------------
# Dashboard surface: model_health_alerts row written for sweeper + threshold
# ---------------------------------------------------------------------------


def test_sweep_dispatch_writes_model_health_alert_per_coid(monkeypatch):
    """The sweeper must write one row to ``model_health_alerts`` per
    promoted coid so the operator dashboard (which queries that table
    for unacknowledged alerts) surfaces the dead-letter event alongside
    other model-health signals (Brier drift, position drift, etc.).
    """
    executes: list[tuple[str, tuple[Any, ...]]] = []

    async def _capture_execute(sql: str, *args: Any) -> str:
        executes.append((sql, args))
        return "INSERT 0 1"

    monkeypatch.setattr(dls.db, "execute", _capture_execute)
    # Stub the email path — we only care about the dashboard write here.
    from app.notifications import alerts as _alerts

    monkeypatch.setattr(_alerts, "order_dead_lettered", lambda **k: None)

    rows = [
        {
            "client_order_id": "coid-A",
            "market_ticker": "MKT-1",
            "side": "yes",
            "action": "buy",
            "contracts": 3,
            "limit_price_cents": 55,
            "vertical": "crypto",
            "recovery_attempts": 11,
            "last_recovery_outcome": "not_found",
            "last_error": "venue 5xx",
        },
        {
            "client_order_id": "coid-B",
            "market_ticker": "MKT-2",
            "side": "no",
            "action": "sell",
            "contracts": 1,
            "limit_price_cents": 30,
            "vertical": "sports",
            "recovery_attempts": 8,
            "last_recovery_outcome": "lookup_failed",
            "last_error": "timeout",
        },
    ]
    _run(dls._dispatch_alerts(rows))

    # Two INSERTs into model_health_alerts (one per coid).
    health_inserts = [
        e for e in executes
        if "INSERT INTO model_health_alerts" in " ".join(e[0].split())
    ]
    assert len(health_inserts) == 2
    # Both rows tagged with the dead-letter alert_type and the coid as
    # metric_name so the dashboard can dedupe and link back.
    coids_written = {e[1][1] for e in health_inserts}
    assert coids_written == {"coid-A", "coid-B"}


def test_sweep_health_alert_swallows_db_errors(monkeypatch):
    """Dashboard surface is best-effort — the email paging path
    (which is the source of truth for operator notification) must NOT
    be blocked by a model_health_alerts INSERT failure."""
    async def _boom_execute(sql: str, *args: Any) -> str:
        raise RuntimeError("postgres degraded")

    monkeypatch.setattr(dls.db, "execute", _boom_execute)

    paged: list[str] = []
    from app.notifications import alerts as _alerts

    def _capture_email(**kwargs):
        paged.append(str(kwargs.get("client_order_id")))

    monkeypatch.setattr(_alerts, "order_dead_lettered", _capture_email)

    rows = [
        {
            "client_order_id": "coid-X",
            "market_ticker": "MKT-1",
            "side": "yes",
            "action": "buy",
            "contracts": 1,
            "limit_price_cents": 50,
            "vertical": "crypto",
            "recovery_attempts": 6,
            "last_recovery_outcome": "not_found",
            "last_error": "x",
        },
    ]
    # Should not raise.
    _run(dls._dispatch_alerts(rows))
    # And the email path still fired.
    assert paged == ["coid-X"]


def test_threshold_alert_persists_health_alert_row(monkeypatch):
    """When the recovery-attempt threshold is crossed, a row must
    appear in ``model_health_alerts`` with ``alert_type='order_submit_stuck'``
    and the coid as ``metric_name`` so the operator dashboard surfaces
    the stuck order alongside other model-health signals.
    """
    executes: list[tuple[str, tuple[Any, ...]]] = []
    fetchrows: list[tuple[str, tuple[Any, ...]]] = []

    async def _capture_execute(sql: str, *args: Any) -> str:
        executes.append((sql, args))
        return "INSERT 0 1"

    async def _capture_fetchrow(sql: str, *args: Any):
        fetchrows.append((sql, args))
        # No recent dedupe row → INSERT proceeds.
        return None

    monkeypatch.setattr(lifecycle.db, "execute", _capture_execute)
    monkeypatch.setattr(lifecycle.db, "fetchrow", _capture_fetchrow)

    snap = {
        "client_order_id": "coid-stuck",
        "market_ticker": "MKT-1",
        "side": "yes",
        "action": "buy",
        "contracts": 5,
        "limit_price_cents": 60,
        "vertical": "crypto",
        "recovery_attempts": 5,
        "threshold": 3,
        "last_recovery_outcome": "not_found",
        "last_error": "missing",
    }
    _run(lifecycle._persist_order_submit_stuck_health_alert(snap))

    # Dedupe SELECT ran first.
    assert any(
        "SELECT 1 FROM model_health_alerts" in " ".join(f[0].split())
        for f in fetchrows
    )
    # INSERT followed.
    health_inserts = [
        e for e in executes
        if "INSERT INTO model_health_alerts" in " ".join(e[0].split())
    ]
    assert len(health_inserts) == 1
    args = health_inserts[0][1]
    # vertical, coid, attempts, threshold, message
    assert args[0] == "crypto"
    assert args[1] == "coid-stuck"
    assert args[2] == 5.0
    assert args[3] == 3.0
    assert "coid-stuck" in args[4]


def test_threshold_alert_dedupes_within_one_hour(monkeypatch):
    """Hourly dedupe on (alert_type, metric_name): a sustained outage
    that re-crosses the threshold within an hour must NOT flood the
    table."""
    executes: list[tuple[str, tuple[Any, ...]]] = []

    async def _capture_execute(sql: str, *args: Any) -> str:
        executes.append((sql, args))
        return "INSERT 0 1"

    async def _recent_fetchrow(sql: str, *args: Any):
        # Pretend a recent row already exists.
        return {"?": 1}

    monkeypatch.setattr(lifecycle.db, "execute", _capture_execute)
    monkeypatch.setattr(lifecycle.db, "fetchrow", _recent_fetchrow)

    _run(
        lifecycle._persist_order_submit_stuck_health_alert(
            {
                "client_order_id": "coid-dup",
                "recovery_attempts": 7,
                "threshold": 3,
                "vertical": "crypto",
                "market_ticker": "MKT-1",
            }
        )
    )
    # Dedupe gate fired → no INSERT.
    assert executes == []
