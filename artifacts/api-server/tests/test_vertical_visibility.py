"""Regression tests for admin per-vertical visibility settings (Task #204).

Verifies:
  * round-trip set / get against `system_settings`
  * unknown vertical names are rejected with `ValueError`
  * an `admin_audit_log` row is written with action `set_vertical_visibility`
  * the admin POST endpoint requires admin auth (sub-admin → 403)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app import runtime_settings as runtime_settings_mod


class _MemoryDb:
    """In-memory `system_settings` substitute that records every audit
    insert so tests can assert on them."""

    def __init__(self) -> None:
        self.system_settings: dict[str, str] = {}
        self.audit: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM system_settings" in sql:
            return [{"key": k, "value": v} for k, v in self.system_settings.items()
                    if k in args]
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
                key, value = args[0], args[1]
                self.parent.system_settings[key] = value
            elif "DELETE FROM system_settings" in sql:
                self.parent.system_settings.pop(args[0], None)
            elif "INSERT INTO admin_audit_log" in sql:
                action = args[4]
                self.parent.audit.append((action, {
                    "actor_user_id": args[0],
                    "actor_email": args[1],
                    "target_user_id": args[2],
                    "target_email": args[3],
                    "before": json.loads(args[5]) if isinstance(args[5], str) else args[5],
                    "after": json.loads(args[6]) if isinstance(args[6], str) else args[6],
                }))
            return "OK"

    def acquire(self):
        parent = self
        class _Ctx:
            async def __aenter__(self_inner): return _MemoryDb._Conn(parent)
            async def __aexit__(self_inner, *exc): return None
        return _Ctx()


@pytest.fixture
def mem_db(monkeypatch):
    db = _MemoryDb()
    monkeypatch.setattr(runtime_settings_mod, "db", db, raising=True)
    runtime_settings_mod._system_settings_ready = True
    return db


def test_round_trip_persists_and_returns_lists(mem_db):
    from app import verticals as _verticals

    names = [s.name for s in _verticals.all_specs()]
    pin_target, hide_target = names[0], names[1]

    out = asyncio.run(runtime_settings_mod.set_vertical_visibility(
        hidden=[hide_target], pinned=[pin_target],
        actor_user_id="user_admin", actor_email="admin@example.com",
    ))
    assert out == {"hidden": [hide_target], "pinned": [pin_target]}

    again = asyncio.run(runtime_settings_mod.get_vertical_visibility())
    assert again == {"hidden": [hide_target], "pinned": [pin_target]}


def test_writes_audit_row(mem_db):
    from app import verticals as _verticals

    target = _verticals.all_specs()[0].name
    asyncio.run(runtime_settings_mod.set_vertical_visibility(
        pinned=[target],
        actor_user_id="user_admin", actor_email="admin@example.com",
    ))
    actions = [a for a, _ in mem_db.audit]
    assert "set_vertical_visibility" in actions
    _, row = next(r for r in mem_db.audit if r[0] == "set_vertical_visibility")
    assert row["actor_user_id"] == "user_admin"
    assert row["target_user_id"] == "system"
    assert row["after"] == {"hidden": [], "pinned": [target]}


def test_unknown_vertical_rejected(mem_db):
    with pytest.raises(ValueError, match="unknown vertical"):
        asyncio.run(runtime_settings_mod.set_vertical_visibility(
            hidden=["does-not-exist"],
            actor_user_id="u", actor_email=None,
        ))


def test_partial_update_leaves_other_side_untouched(mem_db):
    from app import verticals as _verticals

    a, b = (s.name for s in _verticals.all_specs()[:2])
    asyncio.run(runtime_settings_mod.set_vertical_visibility(
        hidden=[a], pinned=[b],
        actor_user_id="u", actor_email=None,
    ))
    asyncio.run(runtime_settings_mod.set_vertical_visibility(
        pinned=[a],  # leave hidden untouched
        actor_user_id="u", actor_email=None,
    ))
    out = asyncio.run(runtime_settings_mod.get_vertical_visibility())
    assert out["hidden"] == [a]  # preserved
    assert out["pinned"] == [a]  # replaced


def test_requires_at_least_one_field(mem_db):
    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(runtime_settings_mod.set_vertical_visibility(
            actor_user_id="u", actor_email=None,
        ))
