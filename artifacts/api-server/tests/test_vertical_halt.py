"""Task #271 — per-vertical halt toggle.

Covers:
  * round-trip halt / resume persistence against an in-memory
    ``system_settings`` stand-in.
  * ``admin_audit_log`` rows on every state change.
  * Unknown vertical names are rejected with ``ValueError``.
  * Double-halt / double-resume are idempotent (``changed == False``
    and no extra audit row).
  * ``gate_vertical_halted`` rejects candidates whose vertical is
    halted and clears candidates whose vertical is live.
  * The halt admin endpoint cancels every resting Kalshi order
    attached to the halted vertical and leaves other verticals'
    resting orders untouched.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from app import runtime_settings as runtime_settings_mod
from app.risk import gates as risk_gates


# ---------------------------------------------------------------------------
# Memory DB (same shape as tests/test_vertical_visibility.py)
# ---------------------------------------------------------------------------


class _MemoryDb:
    def __init__(self) -> None:
        self.system_settings: dict[str, str] = {}
        self.audit: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM system_settings" in sql and "LIKE" in sql:
            prefix = args[0].rstrip("%")
            return [
                {"key": k, "value": v}
                for k, v in self.system_settings.items()
                if k.startswith(prefix)
            ]
        if "FROM system_settings" in sql:
            return [
                {"key": k, "value": v}
                for k, v in self.system_settings.items()
                if k in args
            ]
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return None

    class _Conn:
        def __init__(self, parent: "_MemoryDb") -> None:
            self.parent = parent

        async def execute(self, sql: str, *args: Any) -> str:
            if "INSERT INTO system_settings" in sql:
                self.parent.system_settings[args[0]] = args[1]
            elif "DELETE FROM system_settings" in sql:
                self.parent.system_settings.pop(args[0], None)
            elif "INSERT INTO admin_audit_log" in sql:
                action = args[4]
                self.parent.audit.append(
                    (
                        action,
                        {
                            "actor_user_id": args[0],
                            "actor_email": args[1],
                            "target_user_id": args[2],
                            "target_email": args[3],
                            "before": (
                                json.loads(args[5])
                                if isinstance(args[5], str)
                                else args[5]
                            ),
                            "after": (
                                json.loads(args[6])
                                if isinstance(args[6], str)
                                else args[6]
                            ),
                        },
                    )
                )
            return "OK"

    def acquire(self):
        parent = self

        class _Ctx:
            async def __aenter__(self_inner):
                return _MemoryDb._Conn(parent)

            async def __aexit__(self_inner, *exc):
                return None

        return _Ctx()


@pytest.fixture
def mem_db(monkeypatch):
    db = _MemoryDb()
    monkeypatch.setattr(runtime_settings_mod, "db", db, raising=True)
    runtime_settings_mod._system_settings_ready = True
    return db


def _first_known_vertical() -> str:
    from app import verticals as _verticals

    return _verticals.all_specs()[0].name


# ---------------------------------------------------------------------------
# runtime_settings round-trips + audit log
# ---------------------------------------------------------------------------


def test_halt_then_resume_round_trip(mem_db):
    target = _first_known_vertical()

    meta, changed = asyncio.run(
        runtime_settings_mod.set_vertical_halted(
            target,
            actor_user_id="user_admin",
            actor_email="admin@example.com",
            reason="smoke test",
        )
    )
    assert changed is True
    assert meta["by_user_id"] == "user_admin"
    assert meta["by_email"] == "admin@example.com"
    assert meta["reason"] == "smoke test"

    halts = asyncio.run(runtime_settings_mod.get_vertical_halts())
    assert target in halts

    assert asyncio.run(runtime_settings_mod.is_vertical_halted(target)) is True

    prev, changed = asyncio.run(
        runtime_settings_mod.clear_vertical_halt(
            target, actor_user_id="user_admin", actor_email="admin@example.com"
        )
    )
    assert changed is True
    assert prev is not None and prev["by_user_id"] == "user_admin"
    assert asyncio.run(runtime_settings_mod.is_vertical_halted(target)) is False


def test_halt_writes_audit_log(mem_db):
    target = _first_known_vertical()
    asyncio.run(
        runtime_settings_mod.set_vertical_halted(
            target, actor_user_id="u1", actor_email="u1@example.com"
        )
    )
    asyncio.run(
        runtime_settings_mod.clear_vertical_halt(
            target, actor_user_id="u1", actor_email="u1@example.com"
        )
    )
    actions = [a for a, _ in mem_db.audit]
    assert actions.count("halt_vertical") == 1
    assert actions.count("resume_vertical") == 1
    _, halt_row = next(r for r in mem_db.audit if r[0] == "halt_vertical")
    assert halt_row["actor_user_id"] == "u1"
    assert halt_row["target_user_id"] == "system"
    assert halt_row["after"]["halted"] is True
    assert halt_row["after"]["vertical"] == target


def test_unknown_vertical_rejected(mem_db):
    with pytest.raises(ValueError, match="unknown vertical"):
        asyncio.run(
            runtime_settings_mod.set_vertical_halted(
                "not-a-real-vertical",
                actor_user_id="u",
                actor_email=None,
            )
        )


def test_double_halt_is_idempotent(mem_db):
    target = _first_known_vertical()
    _, changed1 = asyncio.run(
        runtime_settings_mod.set_vertical_halted(
            target, actor_user_id="u", actor_email=None
        )
    )
    _, changed2 = asyncio.run(
        runtime_settings_mod.set_vertical_halted(
            target, actor_user_id="u", actor_email=None
        )
    )
    assert changed1 is True
    assert changed2 is False
    # Only one audit row despite two halt calls.
    halt_rows = [a for a, _ in mem_db.audit if a == "halt_vertical"]
    assert len(halt_rows) == 1


def test_double_resume_is_idempotent(mem_db):
    target = _first_known_vertical()
    prev, changed = asyncio.run(
        runtime_settings_mod.clear_vertical_halt(
            target, actor_user_id="u", actor_email=None
        )
    )
    assert prev is None
    assert changed is False
    resume_rows = [a for a, _ in mem_db.audit if a == "resume_vertical"]
    assert resume_rows == []


# ---------------------------------------------------------------------------
# gate_vertical_halted
# ---------------------------------------------------------------------------


def _ctx(vertical: str, overrides: dict[str, Any]) -> risk_gates.RiskContext:
    return risk_gates.RiskContext(
        market_id="TEST-MKT",
        side="YES",
        vertical=vertical,
        league=None,
        market_type=None,
        matchup=None,
        current_price=0.5,
        model_probability=0.6,
        edge=0.10,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.9,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=1,
        intended_notional_usd=50.0,
        bankroll_usd=1000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides,
        market_cooldown_until=None,
    )


def test_gate_clears_when_vertical_not_halted():
    result = asyncio.run(
        risk_gates.gate_vertical_halted(_ctx("sports", {}))
    )
    assert result.passed is True
    assert result.name == "vertical_halted"


def test_gate_rejects_when_vertical_halted():
    meta_json = json.dumps(
        {"by_user_id": "u1", "by_email": "u1@example.com", "at": "2026-04-23T00:00:00+00:00"}
    )
    result = asyncio.run(
        risk_gates.gate_vertical_halted(
            _ctx("sports", {"vertical_halted.sports": meta_json})
        )
    )
    assert result.passed is False
    assert result.reason == "vertical 'sports' is halted"
    assert result.details["vertical"] == "sports"
    assert result.details["by_email"] == "u1@example.com"


def test_gate_other_vertical_not_affected():
    result = asyncio.run(
        risk_gates.gate_vertical_halted(
            _ctx("crypto", {"vertical_halted.sports": "1"})
        )
    )
    assert result.passed is True


def test_gate_is_wired_into_chain():
    names = [name for name, _ in risk_gates.GATES]
    assert "vertical_halted" in names
    # Cheap — should run near the top, before DB-heavy gates.
    assert names.index("vertical_halted") <= 3


# ---------------------------------------------------------------------------
# admin endpoint: halt cancels resting orders scoped to the vertical
# ---------------------------------------------------------------------------


@dataclass
class _FakeReq:
    vertical: str


@dataclass
class _FakeRec:
    order_id: str
    request: _FakeReq


def test_halt_cancels_resting_orders_for_vertical_only(monkeypatch):
    from app import main as api_main

    resting = [
        _FakeRec("ord-sports-1", _FakeReq("sports")),
        _FakeRec("ord-sports-2", _FakeReq("sports")),
        _FakeRec("ord-crypto-1", _FakeReq("crypto")),
    ]

    async def _list_resting() -> list[Any]:
        return list(resting)

    cancelled: list[str] = []

    async def _cancel(order_id: str) -> dict[str, Any]:
        cancelled.append(order_id)
        return {"order_id": order_id, "state": "canceled"}

    monkeypatch.setattr(
        api_main.kalshi_lifecycle, "list_resting_orders", _list_resting
    )
    monkeypatch.setattr(api_main.kalshi_lifecycle, "cancel_order", _cancel)

    summary = asyncio.run(api_main._cancel_resting_for_vertical("sports"))
    assert summary == {
        "scanned": 3,
        "matched": 2,
        "cancelled": 2,
        "failed": 0,
    }
    assert sorted(cancelled) == ["ord-sports-1", "ord-sports-2"]


def test_halt_cancel_sweep_swallows_individual_failures(monkeypatch):
    from app import main as api_main
    from app.kalshi.lifecycle import OrderRejected

    resting = [
        _FakeRec("ord-sports-1", _FakeReq("sports")),
        _FakeRec("ord-sports-2", _FakeReq("sports")),
    ]

    async def _list_resting() -> list[Any]:
        return list(resting)

    async def _cancel(order_id: str) -> dict[str, Any]:
        if order_id == "ord-sports-1":
            raise OrderRejected("venue refused")
        return {"order_id": order_id, "state": "canceled"}

    monkeypatch.setattr(
        api_main.kalshi_lifecycle, "list_resting_orders", _list_resting
    )
    monkeypatch.setattr(api_main.kalshi_lifecycle, "cancel_order", _cancel)

    summary = asyncio.run(api_main._cancel_resting_for_vertical("sports"))
    assert summary["matched"] == 2
    assert summary["cancelled"] == 1
    assert summary["failed"] == 1
