"""Coverage for the emergency-stop endpoint and the Kalshi bankroll
sync paths added in task #104, plus the cycle-side bankroll lookup
that prefers the new events table.

The emergency-stop test goes through ``POST /api/admin/emergency-stop``
(via FastAPI's ``TestClient``) so the wiring between the HTTP handler
and ``app.telegram.estop.execute_emergency_stop`` is exercised end to
end. Auth is bypassed with ``app.dependency_overrides`` because the
JWT/Clerk plumbing is already covered exhaustively by
``test_admin_endpoints.py``; what we want here is the side effects.

The bankroll tests stub ``app.bankroll_sync.db`` /
``app.orchestrator.cycle.db`` with a tiny in-memory fake.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import bankroll_sync
from app import db as db_module
from app.auth import AuthedUser, require_admin, check_approval
from app.main import app
from app.orchestrator import cycle as cycle_mod
from app.telegram import estop as estop_mod


# ---------------------------------------------------------------------------
# Emergency-stop endpoint
# ---------------------------------------------------------------------------


class _StubKalshiLifecycle:
    def __init__(self) -> None:
        self.cancel_called = 0
        self.halt_calls: list[str | None] = []
        self.cancel_count = 4

    async def cancel_all_resting(self) -> int:
        self.cancel_called += 1
        return self.cancel_count

    def halt(self, reason: str | None = None) -> dict[str, Any]:
        self.halt_calls.append(reason)
        return {"halted": True, "reason": reason, "since": "now"}

    def kill_switch_state(self) -> dict[str, Any]:
        return {"halted": True, "reason": self.halt_calls[-1] if self.halt_calls else None}


class _DBRecorder:
    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.executes.append((" ".join(query.split()), args))
        return "OK"


@pytest.fixture
def stub_admin():
    """Bypass JWT/admin checks — auth is covered by other tests."""
    fake = AuthedUser(
        user_id="user_admin_estop",
        session_id="sess_test",
        claims={"email": "ops@example.com"},
    )

    async def _admin() -> AuthedUser:
        return fake

    async def _check(_user: AuthedUser) -> tuple[bool, str | None]:
        return True, "ops@example.com"

    app.dependency_overrides[require_admin] = _admin
    # check_approval is invoked from inside the handler to look up the
    # email; patch it on the module so it doesn't try to hit the DB.
    import app.main as main_mod
    prev_check = main_mod.check_approval
    main_mod.check_approval = _check
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(require_admin, None)
        main_mod.check_approval = prev_check


@pytest.fixture
def patched_estop(monkeypatch: pytest.MonkeyPatch):
    klife = _StubKalshiLifecycle()
    rec = _DBRecorder()
    alerts_fired: list[dict[str, Any]] = []

    def _alert(*, actor, canceled_count, reason, dry_run=False):  # noqa: ANN001
        alerts_fired.append(
            {"actor": actor, "canceled_count": canceled_count, "reason": reason}
        )
        return {"ok": True}

    monkeypatch.setattr(estop_mod, "kalshi_lifecycle", klife)
    monkeypatch.setattr(estop_mod.db, "execute", rec.execute)
    monkeypatch.setattr(estop_mod.notif_alerts, "emergency_stop", _alert)
    return {"klife": klife, "db": rec, "alerts": alerts_fired}


def test_emergency_stop_endpoint_runs_full_pipeline(stub_admin, patched_estop) -> None:
    client = TestClient(app)
    r = client.post("/api/admin/emergency-stop", json={"reason": "runaway loop"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["trading_mode"] == "halt"
    assert body["canceled_orders"] == 4
    assert body["reason"] == "runaway loop"
    assert body["kill_switch"]["halted"] is True

    klife = patched_estop["klife"]
    assert klife.cancel_called == 1, "cancel_all_resting must be invoked"
    assert klife.halt_calls == ["runaway loop"], "kill switch must be tripped"

    rec: _DBRecorder = patched_estop["db"]
    queries = [q for q, _ in rec.executes]
    # 1) trading_mode = 'halt' upsert into system_settings
    assert any(
        "INSERT INTO system_settings" in q and "risk.trading_mode" in q
        for q in queries
    ), queries
    # 2) exactly one admin_audit_log row with action=emergency_stop
    audit_rows = [args for q, args in rec.executes if "INSERT INTO admin_audit_log" in q]
    assert len(audit_rows) == 1, audit_rows
    args = audit_rows[0]
    # action arg lands at index 2 in execute_emergency_stop's call.
    assert args[0] == "user_admin_estop"
    assert args[1] == "ops@example.com"
    assert args[2] == "emergency_stop"
    after = json.loads(args[4])
    assert after["trading_mode"] == "halt"
    assert after["reason"] == "runaway loop"
    assert after["canceled_orders"] == 4

    # 3) operator alert fired exactly once with the cancel count.
    assert patched_estop["alerts"] == [
        {"actor": "ops@example.com", "canceled_count": 4, "reason": "runaway loop"}
    ]


# ---------------------------------------------------------------------------
# bankroll_sync.record_balance
# ---------------------------------------------------------------------------


class _FakeBankrollDB:
    """In-memory stand-in for the ``bankroll_events`` table.

    Mirrors the post-#752 schema: every row carries a ``trading_mode``
    column ('paper' or 'live') and the SELECT/INSERT helpers pass it
    explicitly so the paper and live ledgers stay isolated.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._next_id = 1

    async def fetchval(self, query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT balance_cents FROM bankroll_events"):
            # Post-#752 the helper always passes (exchange, trading_mode).
            exchange = args[0] if len(args) >= 1 else "kalshi"
            trading_mode = args[1] if len(args) >= 2 else "live"
            scoped = [
                r for r in self.rows
                if r["exchange"] == exchange
                and r.get("trading_mode", "live") == trading_mode
            ]
            if not scoped:
                return None
            return scoped[-1]["balance_cents"]
        return None

    async def fetchrow(self, query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO bankroll_events"):
            row = {
                "id": self._next_id,
                "occurred_at": "now",
                "exchange": args[0],
                "reason": args[1],
                "delta_cents": args[2],
                "balance_cents": args[3],
                # Trading mode is the 6th positional arg post-#752.
                # Default to 'live' for compat with any callers that
                # haven't been migrated yet.
                "trading_mode": args[5] if len(args) >= 6 else "live",
            }
            self._next_id += 1
            self.rows.append(row)
            return row
        return None


def test_record_balance_seeds_first_row_with_zero_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBankrollDB()
    monkeypatch.setattr(bankroll_sync.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(bankroll_sync.db, "fetchrow", fake.fetchrow)

    out = asyncio.run(bankroll_sync.record_balance(12_500))
    assert out is not None
    assert out["balance_cents"] == 12_500
    assert out["delta_cents"] == 12_500  # vs prev=None treated as 0
    assert len(fake.rows) == 1


def test_record_balance_skips_when_balance_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBankrollDB()
    monkeypatch.setattr(bankroll_sync.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(bankroll_sync.db, "fetchrow", fake.fetchrow)

    asyncio.run(bankroll_sync.record_balance(12_500))
    out = asyncio.run(bankroll_sync.record_balance(12_500))
    assert out is None
    assert len(fake.rows) == 1, "no second row when balance is identical"


def test_record_balance_writes_delta_against_latest_row(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBankrollDB()
    monkeypatch.setattr(bankroll_sync.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(bankroll_sync.db, "fetchrow", fake.fetchrow)

    asyncio.run(bankroll_sync.record_balance(10_000))
    asyncio.run(bankroll_sync.record_balance(10_750))  # +750
    asyncio.run(bankroll_sync.record_balance(10_250))  # -500 vs 10_750

    deltas = [r["delta_cents"] for r in fake.rows]
    balances = [r["balance_cents"] for r in fake.rows]
    assert balances == [10_000, 10_750, 10_250]
    # First row seeds against prev=None (treated as 0).
    assert deltas == [10_000, 750, -500]


# ---------------------------------------------------------------------------
# orchestrator.cycle._latest_bankroll
# ---------------------------------------------------------------------------


def test_latest_bankroll_prefers_bankroll_events(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _fetchval(query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT balance_cents FROM bankroll_events"):
            calls.append("events")
            return 25_000  # $250.00
        if q.startswith("SELECT balance::float FROM bankroll_snapshots"):
            calls.append("snapshots")
            return 999.0  # would be wrong if used
        return None

    monkeypatch.setattr(cycle_mod.db, "fetchval", _fetchval)

    out = asyncio.run(cycle_mod._latest_bankroll())
    assert out == pytest.approx(250.0)
    assert calls == ["events"], "snapshot fallback must not run when events has a row"


def test_latest_bankroll_falls_back_to_snapshots_when_events_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _fetchval(query: str, *args: Any):
        q = " ".join(query.split())
        if q.startswith("SELECT balance_cents FROM bankroll_events"):
            calls.append("events")
            return None
        if q.startswith("SELECT balance::float FROM bankroll_snapshots"):
            calls.append("snapshots")
            return 314.15
        return None

    monkeypatch.setattr(cycle_mod.db, "fetchval", _fetchval)

    out = asyncio.run(cycle_mod._latest_bankroll())
    assert out == pytest.approx(314.15)
    assert calls == ["events", "snapshots"]
