"""Unit tests for `app/runtime_settings.py` (Task #137).

Covers the bare-bones contract the rest of the system relies on:

* set/get round-trip for the global aggression mode.
* invalid-mode rejection on writes (typo can't silently land).
* `get_risk_knob_state` snapshot shape — every spec key surfaces with
  default/override/effective fields the operator dashboard reads.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import runtime_settings as runtime_settings_mod


class _MemoryDb:
    """In-memory stand-in for the asyncpg pool covering just the rows
    `runtime_settings.py` reads/writes (`runtime_settings`,
    `system_settings`, `admin_audit_log`)."""

    def __init__(self) -> None:
        self.runtime_settings: dict[str, str] = {}
        self.system_settings: dict[str, str] = {}
        self.audit: list[dict[str, Any]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM system_settings" in sql and "LIKE" in sql:
            pattern = args[0] if args else ""
            prefix = pattern.rstrip("%")
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
        if "FROM runtime_settings" in sql and args:
            return self.runtime_settings.get(args[0])
        # The module's get_aggression_mode hard-codes the key in the SQL
        # rather than passing it as a param, so cover that too.
        if "key = 'aggression_mode'" in sql:
            return self.runtime_settings.get("aggression_mode")
        if "key = 'trading_halted'" in sql:
            return self.runtime_settings.get("trading_halted")
        return None

    class _Conn:
        def __init__(self, parent: "_MemoryDb") -> None:
            self.parent = parent

        async def execute(self, sql: str, *args: Any) -> str:
            if "INSERT INTO runtime_settings" in sql:
                # The module hard-codes the key in the SQL ('aggression_mode'
                # / 'trading_halted') and only binds the value + updated_by
                # as params -> sniff the literal key out of the SQL.
                if "'aggression_mode'" in sql:
                    self.parent.runtime_settings["aggression_mode"] = args[0]
                elif "'trading_halted'" in sql:
                    self.parent.runtime_settings["trading_halted"] = args[0]
            elif "INSERT INTO system_settings" in sql:
                key, value = args[0], args[1]
                self.parent.system_settings[key] = value
            elif "DELETE FROM system_settings" in sql:
                self.parent.system_settings.pop(args[0], None)
            elif "INSERT INTO admin_audit_log" in sql:
                self.parent.audit.append(
                    {"action": args[4], "actor_user_id": args[0]}
                )
            elif "CREATE TABLE" in sql:
                pass
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
    # Skip the schema bootstrap — the in-memory db has the tables ready.
    return db


# ---------------------------------------------------------------------------
# set/get round-trip
# ---------------------------------------------------------------------------


def test_default_mode_when_unset(mem_db):
    assert asyncio.run(runtime_settings_mod.get_aggression_mode()) == (
        runtime_settings_mod.DEFAULT_MODE
    )


@pytest.mark.parametrize("mode", ["conservative", "balanced", "aggressive"])
def test_set_get_round_trip(mem_db, mode):
    out = asyncio.run(
        runtime_settings_mod.set_aggression_mode(mode, actor_user_id="user_admin")
    )
    assert out == mode
    assert asyncio.run(runtime_settings_mod.get_aggression_mode()) == mode
    # And the row actually landed in the table.
    assert mem_db.runtime_settings["aggression_mode"] == mode


def test_set_normalizes_case_and_whitespace(mem_db):
    out = asyncio.run(
        runtime_settings_mod.set_aggression_mode(
            "  Aggressive  ", actor_user_id="user_admin"
        )
    )
    assert out == "aggressive"
    assert asyncio.run(runtime_settings_mod.get_aggression_mode()) == "aggressive"


def test_overwrite_replaces_previous_mode(mem_db):
    asyncio.run(
        runtime_settings_mod.set_aggression_mode(
            "aggressive", actor_user_id="user_admin"
        )
    )
    asyncio.run(
        runtime_settings_mod.set_aggression_mode(
            "conservative", actor_user_id="user_admin"
        )
    )
    assert asyncio.run(runtime_settings_mod.get_aggression_mode()) == "conservative"


# ---------------------------------------------------------------------------
# invalid-mode rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["yolo", "", "  ", "balanc3d", "AGRESSIVE"])
def test_set_aggression_mode_rejects_invalid(mem_db, bad):
    with pytest.raises(ValueError, match="invalid aggression_mode"):
        asyncio.run(
            runtime_settings_mod.set_aggression_mode(
                bad, actor_user_id="user_admin"
            )
        )
    # And nothing landed in the table.
    assert "aggression_mode" not in mem_db.runtime_settings


def test_get_aggression_mode_normalises_garbage_value(mem_db):
    # Simulate a corrupt row left over from a manual SQL edit — the
    # getter must coerce it back to the default rather than propagate.
    mem_db.runtime_settings["aggression_mode"] = "yolo"
    assert asyncio.run(runtime_settings_mod.get_aggression_mode()) == (
        runtime_settings_mod.DEFAULT_MODE
    )


# ---------------------------------------------------------------------------
# snapshot shape
# ---------------------------------------------------------------------------


def test_risk_knob_state_snapshot_shape(mem_db):
    asyncio.run(
        runtime_settings_mod.set_aggression_mode(
            "conservative", actor_user_id="user_admin"
        )
    )
    state = asyncio.run(runtime_settings_mod.get_risk_knob_state())

    assert state["aggression_mode"] == "conservative"
    assert isinstance(state["knobs"], list)
    assert len(state["knobs"]) == len(runtime_settings_mod.RISK_KNOB_SPECS)

    by_key = {k["key"]: k for k in state["knobs"]}
    expected_fields = {
        "key",
        "label",
        "description",
        "minimum",
        "maximum",
        "step",
        "kind",
        "unit",
        "default_value",
        "override_value",
        "effective_value",
    }
    for spec in runtime_settings_mod.RISK_KNOB_SPECS:
        knob = by_key[spec.key]
        assert set(knob.keys()) == expected_fields
        # No override set yet — effective must equal default.
        assert knob["override_value"] is None
        assert knob["effective_value"] == knob["default_value"]

    # The conservative profile's min_edge_pp default must surface.
    assert by_key["min_edge_pp"]["default_value"] == (
        runtime_settings_mod.MODE_PROFILES["conservative"]["min_edge_pp"]
    )


def test_snapshot_reflects_explicit_override(mem_db):
    asyncio.run(
        runtime_settings_mod.set_aggression_mode(
            "balanced", actor_user_id="user_admin"
        )
    )
    asyncio.run(
        runtime_settings_mod.set_risk_knob_overrides(
            {"min_edge_pp": 9.5},
            actor_user_id="user_admin",
            actor_email="admin@example.com",
        )
    )

    state = asyncio.run(runtime_settings_mod.get_risk_knob_state())
    by_key = {k["key"]: k for k in state["knobs"]}
    knob = by_key["min_edge_pp"]
    # Default still tracks the active mode...
    assert knob["default_value"] == (
        runtime_settings_mod.MODE_PROFILES["balanced"]["min_edge_pp"]
    )
    # ...but the override wins for `effective_value`.
    assert knob["override_value"] == 9.5
    assert knob["effective_value"] == 9.5
