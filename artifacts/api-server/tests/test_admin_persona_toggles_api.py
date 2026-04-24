"""API-level tests for the admin persona-toggles endpoint (Task #205).

The store-level coverage in ``test_admin_persona_toggles.py`` exercises
the round-trip through ``app.swarm.toggles`` directly, but it does
*not* drive the FastAPI handler.  That gap let the spec.id / spec.label
AttributeError that escaped to runtime slip past CI: the handler reads
``spec.name`` / ``spec.description`` and serialises ``profile.personas``,
none of which the store-level tests touch.

These tests hit ``GET /api/admin/persona-toggles`` and
``POST /api/admin/persona-toggles`` through ``TestClient`` with
``require_admin`` overridden so the handler logic — payload shape,
admin-only gate, audit-log write — is exercised end-to-end.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main as main_module
from app.auth import AuthedUser, require_admin
from app.main import app
from app.swarm import toggles as toggle_store


# ---------------------------------------------------------------------------
# In-memory stand-in for the swarm_persona_toggles table + admin_audit_log.
# Mirrors the fake used in the store-level test but adds the SELECT shape
# the handler relies on (vertical, persona, enabled, updated_at, updated_by).
# ---------------------------------------------------------------------------


@dataclass
class _FakeStore:
    rows: dict[tuple[str, str], dict[str, Any]]
    audit: list[tuple[Any, ...]]


class _FakeConn:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

    async def execute(self, sql: str, *args: Any) -> str:
        s = sql.strip().lower()
        if s.startswith("create table"):
            return "CREATE"
        if s.startswith("insert into swarm_persona_toggles"):
            v, p, enabled, actor = args
            self.store.rows[(v, p)] = {
                "enabled": bool(enabled),
                "updated_by": actor,
            }
            return "INSERT 0 1"
        if s.startswith("insert into admin_audit_log"):
            self.store.audit.append(args)
            return "INSERT 0 1"
        return ""


class _FakeDB:
    def __init__(self, store: _FakeStore) -> None:
        self.store = store

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
                for (v, p), meta in self.store.rows.items()
            ]
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> _FakeStore:
    return _FakeStore(rows={}, audit=[])


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch, store: _FakeStore) -> _FakeDB:
    fake = _FakeDB(store)
    monkeypatch.setattr(toggle_store, "db", fake)
    toggle_store.reset_cache()
    return fake


@pytest.fixture
def admin_actor() -> AuthedUser:
    return AuthedUser(user_id="admin_user_1", session_id="sess_test", claims={})


@pytest.fixture
def override_admin(admin_actor: AuthedUser):
    """Override require_admin so the handler runs without JWT plumbing."""

    async def _fake_admin() -> AuthedUser:
        return admin_actor

    app.dependency_overrides[require_admin] = _fake_admin
    yield
    app.dependency_overrides.pop(require_admin, None)


@pytest.fixture
def stub_check_approval(monkeypatch: pytest.MonkeyPatch):
    """Pretend the admin caller resolves to a known email."""

    async def _ok(_user: AuthedUser):
        return (True, "ops@example.com")

    monkeypatch.setattr(main_module, "check_approval", _ok)


@pytest.fixture
def client(fake_db, override_admin, stub_check_approval) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# (1) GET payload shape
# ---------------------------------------------------------------------------


def test_get_persona_toggles_returns_expected_shape(client: TestClient) -> None:
    r = client.get("/api/admin/persona-toggles")
    assert r.status_code == 200, r.text
    body = r.json()

    assert set(body.keys()) >= {"items", "available_profiles"}
    assert isinstance(body["items"], list) and body["items"], (
        "expected at least one registered vertical to appear in items"
    )

    item = body["items"][0]
    # Required keys for every vertical entry — these are exactly the
    # attributes the store-level test could not have caught had they
    # been mis-named (spec.id / spec.label vs spec.name / spec.description).
    for key in ("vertical", "vertical_label", "persona_profile", "personas"):
        assert key in item, f"missing key {key!r} in item: {item!r}"
    assert isinstance(item["vertical"], str) and item["vertical"]
    assert isinstance(item["vertical_label"], str) and item["vertical_label"]
    assert isinstance(item["persona_profile"], str) and item["persona_profile"]
    assert isinstance(item["personas"], list) and item["personas"], (
        "every vertical's profile must surface at least one persona"
    )

    persona = item["personas"][0]
    for key in ("id", "display", "provider", "model_id", "enabled"):
        assert key in persona, f"missing persona key {key!r}: {persona!r}"
    assert isinstance(persona["enabled"], bool)

    # available_profiles is the catalogue used by the operator console
    # to show which profiles a vertical *could* run on.
    assert isinstance(body["available_profiles"], list)
    assert body["available_profiles"], "available_profiles must not be empty"
    profile = body["available_profiles"][0]
    for key in ("name", "description", "personas"):
        assert key in profile, f"missing available_profiles key {key!r}"
    assert isinstance(profile["personas"], list)


# ---------------------------------------------------------------------------
# (2) Non-admin is rejected
# ---------------------------------------------------------------------------


def test_non_admin_is_rejected(fake_db, stub_check_approval) -> None:
    """When require_admin raises 403, neither GET nor POST may succeed."""

    async def _deny() -> AuthedUser:
        raise HTTPException(status_code=403, detail="admin only")

    app.dependency_overrides[require_admin] = _deny
    try:
        c = TestClient(app)
        r_get = c.get("/api/admin/persona-toggles")
        assert r_get.status_code == 403, r_get.text

        r_post = c.post(
            "/api/admin/persona-toggles",
            json={"vertical": "crypto", "persona": "vol_regime", "enabled": False},
        )
        assert r_post.status_code == 403, r_post.text
    finally:
        app.dependency_overrides.pop(require_admin, None)

    # No write reached the store + no audit row.
    assert fake_db.store.rows == {}
    assert fake_db.store.audit == []


# ---------------------------------------------------------------------------
# (3) POST round-trip + admin_audit_log row
# ---------------------------------------------------------------------------


def test_post_toggle_round_trips_and_writes_audit_log(
    client: TestClient, fake_db: _FakeDB
) -> None:
    r = client.post(
        "/api/admin/persona-toggles",
        json={"vertical": "crypto", "persona": "vol_regime", "enabled": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vertical"] == "crypto"
    assert body["toggles"].get("vol_regime") is False

    # The toggle landed in the fake store keyed by (vertical, persona).
    assert ("crypto", "vol_regime") in fake_db.store.rows
    assert fake_db.store.rows[("crypto", "vol_regime")]["enabled"] is False
    assert fake_db.store.rows[("crypto", "vol_regime")]["updated_by"] == "admin_user_1"

    # An admin_audit_log row was written with action=set_persona_toggle.
    audit_rows = fake_db.store.audit
    assert audit_rows, "expected at least one admin_audit_log insert"
    actor_user_id, actor_email, target_user_id, target_email, action, before_json, after_json = audit_rows[-1]
    assert actor_user_id == "admin_user_1"
    assert actor_email == "ops@example.com"
    assert target_user_id == "system"
    assert target_email is None
    assert action == "set_persona_toggle"
    after = json.loads(after_json)
    assert after["vertical"] == "crypto"
    assert after["persona"] == "vol_regime"
    assert after["after"]["vol_regime"] is False

    # Reading back through GET reflects the new disabled state.
    r2 = client.get("/api/admin/persona-toggles")
    assert r2.status_code == 200, r2.text
    crypto_item = next(
        (it for it in r2.json()["items"] if it["vertical"] == "crypto"),
        None,
    )
    assert crypto_item is not None, "crypto vertical must be in the GET payload"
    vol = next(
        (p for p in crypto_item["personas"] if p["id"] == "vol_regime"),
        None,
    )
    assert vol is not None, "vol_regime persona must be in the crypto profile"
    assert vol["enabled"] is False
