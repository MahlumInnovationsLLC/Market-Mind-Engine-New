"""Round-trip tests for the per-vertical persona toggle store (Task #194).

The store backs the admin "Personas" tab; these tests stub the
`app.db` calls so the round-trip + audit-log behaviour can be
exercised without a real Postgres instance.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.swarm import toggles as toggle_store


class _FakeConn:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def execute(self, sql: str, *args: Any) -> str:
        s = sql.strip().lower()
        if s.startswith("create table"):
            return "CREATE"
        if s.startswith("insert into swarm_persona_toggles"):
            v, p, enabled, actor = args
            self.store["rows"][(v, p)] = {
                "enabled": bool(enabled),
                "updated_by": actor,
            }
            return "INSERT 0 1"
        if s.startswith("insert into admin_audit_log"):
            self.store["audit"].append(args)
            return "INSERT 0 1"
        return ""


class _FakeDB:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {"rows": {}, "audit": []}

    def acquire(self):
        store = self.store

        class _Cm:
            async def __aenter__(self_inner):
                return _FakeConn(store)

            async def __aexit__(self_inner, *_a):
                return None

        return _Cm()

    async def fetch(self, sql: str, *_args: Any) -> list[dict[str, Any]]:
        s = sql.strip().lower()
        if "from swarm_persona_toggles" in s:
            return [
                {
                    "vertical": v,
                    "persona": p,
                    "enabled": meta["enabled"],
                    "updated_at": "2026-01-01T00:00:00Z",
                    "updated_by": meta.get("updated_by"),
                }
                for (v, p), meta in self.store["rows"].items()
            ]
        return []


def _install_fake_db(monkeypatch) -> _FakeDB:
    fake = _FakeDB()
    monkeypatch.setattr(toggle_store, "db", fake)
    toggle_store.reset_cache()
    toggle_store._ready = False  # force schema re-init against the fake
    return fake


def test_set_toggle_persists_and_invalidates_cache(monkeypatch):
    fake = _install_fake_db(monkeypatch)

    # Prime the cache (empty) then disable a persona.
    asyncio.run(
        toggle_store.set_toggle(
            "crypto",
            "vol_regime",
            enabled=False,
            actor_user_id="admin_1",
            actor_email="ops@example.com",
        )
    )
    assert ("crypto", "vol_regime") in fake.store["rows"]
    assert fake.store["rows"][("crypto", "vol_regime")]["enabled"] is False

    # Cache must reflect the write on the next read.
    disabled = asyncio.run(toggle_store.disabled_persona_ids("crypto"))
    assert disabled == ("vol_regime",)


def test_disabled_persona_ids_skips_enabled_rows(monkeypatch):
    _install_fake_db(monkeypatch)
    # Disable + immediately re-enable — the enabled row must NOT appear
    # in the deny-list.
    asyncio.run(
        toggle_store.set_toggle(
            "elections",
            "ground_game",
            enabled=False,
            actor_user_id="admin",
            actor_email=None,
        )
    )
    asyncio.run(
        toggle_store.set_toggle(
            "elections",
            "ground_game",
            enabled=True,
            actor_user_id="admin",
            actor_email=None,
        )
    )
    disabled = asyncio.run(toggle_store.disabled_persona_ids("elections"))
    assert disabled == ()


def test_set_toggle_writes_audit_log_row(monkeypatch):
    fake = _install_fake_db(monkeypatch)
    asyncio.run(
        toggle_store.set_toggle(
            "tech-ai",
            "regulatory_pulse",
            enabled=False,
            actor_user_id="ops_42",
            actor_email="ops@example.com",
        )
    )
    assert fake.store["audit"], "expected an admin_audit_log insert"
    row = fake.store["audit"][-1]
    actor_user_id, actor_email, target_user_id, target_email, action, before_json, after_json = row
    assert actor_user_id == "ops_42"
    assert actor_email == "ops@example.com"
    assert target_user_id == "system"
    assert target_email is None
    assert action == "set_persona_toggle"
    after = json.loads(after_json)
    assert after["vertical"] == "tech-ai"
    assert after["persona"] == "regulatory_pulse"
    assert after["after"]["regulatory_pulse"] is False


def test_set_toggle_rejects_blank_inputs(monkeypatch):
    _install_fake_db(monkeypatch)
    import pytest

    with pytest.raises(ValueError):
        asyncio.run(
            toggle_store.set_toggle(
                "",
                "x",
                enabled=True,
                actor_user_id=None,
                actor_email=None,
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            toggle_store.set_toggle(
                "crypto",
                "",
                enabled=True,
                actor_user_id=None,
                actor_email=None,
            )
        )
