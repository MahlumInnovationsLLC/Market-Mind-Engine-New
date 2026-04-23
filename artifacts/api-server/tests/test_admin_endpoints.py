"""Tests for the admin approve / promote endpoints.

These complement the auth-gate inventory check (`test_auth_gate.py`) by
exercising the actual mutation logic behind:

  * ``POST /api/admin/users/{id}/approved``
  * ``POST /api/admin/users/{id}/admin``

Specifically:

  * a non-admin caller is rejected with 403,
  * an admin caller can flip ``approved`` and ``is_admin``,
  * each successful flip writes a row into ``admin_audit_log``,
  * each successful flip attempts the Clerk metadata mirror PATCH.

The Postgres pool and Clerk Backend API are fully stubbed in-process so
the tests run under ``uv run pytest`` without any network or DB.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import admin as admin_module
from app import auth as auth_module
from app import db as db_module
from app.main import app
from tests.conftest import TEST_CLERK_HOST


ISSUER = f"https://{TEST_CLERK_HOST}"
ADMIN_UID = "user_admin_1"
TARGET_UID = "user_target_1"


# ---------------------------------------------------------------------------
# JWKS / JWT plumbing (mirrors test_auth_gate.py)
# ---------------------------------------------------------------------------


class _StubSigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _StubJWKSClient:
    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> _StubSigningKey:
        return _StubSigningKey(self._public_key)


@pytest.fixture(scope="module")
def _keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mint(private_key, *, sub: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "sub": sub,
            "iat": now - 60,
            "nbf": now - 60,
            "exp": now + 3600,
            "sid": "sess_test",
        },
        private_key,
        algorithm="RS256",
    )


# ---------------------------------------------------------------------------
# In-memory stand-in for asyncpg
# ---------------------------------------------------------------------------


class FakeStore:
    """Keeps the rows our tests care about + every audit-log insert."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []

    def add_user(
        self,
        clerk_user_id: str,
        *,
        email: str | None = None,
        approved: bool = False,
        is_admin: bool = False,
    ) -> None:
        self.users[clerk_user_id] = {
            "clerk_user_id": clerk_user_id,
            "email": email,
            "display_name": None,
            "approved": approved,
            "is_admin": is_admin,
            "denied_at": None,
            "activity_count": 0,
            "first_seen_at": None,
            "last_seen_at": None,
            "clerk_created_at": None,
            "clerk_last_sign_in_at": None,
            "clerk_synced_at": None,
        }


class FakeConn:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split()).upper()
        if q.startswith("SELECT * FROM APP_USERS WHERE CLERK_USER_ID"):
            return self.store.users.get(args[0])
        if q.startswith("INSERT INTO APP_USERS (CLERK_USER_ID, EMAIL, DISPLAY_NAME)"):
            uid = args[0]
            if uid in self.store.users:
                return None  # ON CONFLICT DO NOTHING
            self.store.add_user(uid, email=args[1])
            self.store.users[uid]["display_name"] = args[2]
            return self.store.users[uid]
        if q.startswith("UPDATE APP_USERS SET IS_ADMIN = TRUE, APPROVED = TRUE"):
            uid = args[0]
            row = self.store.users.setdefault(uid, {})
            row.update({"clerk_user_id": uid, "is_admin": True, "approved": True})
            return row
        if q.startswith("UPDATE APP_USERS SET APPROVED ="):
            uid, value = args[0], args[1]
            row = self.store.users.setdefault(uid, {"clerk_user_id": uid})
            row["approved"] = value
            return row
        if q.startswith("UPDATE APP_USERS SET IS_ADMIN ="):
            uid, value = args[0], args[1]
            row = self.store.users.setdefault(uid, {"clerk_user_id": uid})
            row["is_admin"] = value
            return row
        if q.startswith("UPDATE APP_USERS SET DENIED_AT"):
            uid = args[0]
            row = self.store.users.setdefault(uid, {"clerk_user_id": uid})
            row["denied_at"] = "now"
            return row
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split()).upper()
        if q.startswith("INSERT INTO ADMIN_AUDIT_LOG"):
            self.store.audit.append(
                {
                    "actor_user_id": args[0],
                    "actor_email": args[1],
                    "target_user_id": args[2],
                    "target_email": args[3],
                    "action": args[4],
                    "before_value": args[5],
                    "after_value": args[6],
                }
            )
        return "OK"


# ---------------------------------------------------------------------------
# Fake Clerk Backend API (HTTP stub injected into app.admin's httpx)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> Any:
        return self._payload


class FakeAsyncClient:
    """Records every PATCH so the test can assert the mirror was attempted."""

    patch_calls: list[dict[str, Any]] = []
    get_calls: list[str] = []

    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def patch(self, url: str, *, headers: dict[str, str], json: Any) -> FakeResponse:
        FakeAsyncClient.patch_calls.append({"url": url, "headers": headers, "json": json})
        return FakeResponse(200, {})

    async def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
        FakeAsyncClient.get_calls.append(url)
        # Return non-200 so admin._fetch_clerk_profile yields {} and avoids
        # touching anything else.
        return FakeResponse(404, {})


# ---------------------------------------------------------------------------
# Autouse fixture: wire up JWKS, fake DB, fake Clerk for every test.
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch: pytest.MonkeyPatch, _keypair, store: FakeStore):
    # JWKS / issuer caches.
    auth_module._frontend_api_host.cache_clear()
    auth_module._issuer.cache_clear()
    auth_module._jwks_client.cache_clear()
    auth_module._user_cache.clear()
    monkeypatch.setattr(
        auth_module,
        "_jwks_client",
        lambda: _StubJWKSClient(_keypair.public_key()),
    )
    # Reset Clerk recorder.
    FakeAsyncClient.patch_calls = []
    FakeAsyncClient.get_calls = []
    monkeypatch.setattr(admin_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", FakeAsyncClient)
    # Pretend the Clerk secret is configured so the mirror runs.
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_admin_endpoints")
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)

    # Replace asyncpg pool acquire with the in-memory store.
    @asynccontextmanager
    async def fake_acquire():
        yield FakeConn(store)

    monkeypatch.setattr(db_module, "acquire", fake_acquire)
    monkeypatch.setattr(admin_module.db, "acquire", fake_acquire)
    yield
    auth_module._user_cache.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_non_admin_caller_rejected(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    """A signed-in but non-admin user must not be able to approve others."""
    store.add_user(ADMIN_UID, email="not-admin@example.com", approved=True, is_admin=False)
    store.add_user(TARGET_UID, email="t@example.com")
    token = _mint(_keypair, sub=ADMIN_UID)

    r = client.post(
        f"/api/admin/users/{TARGET_UID}/approved",
        headers={"Authorization": f"Bearer {token}"},
        json={"approved": True},
    )
    assert r.status_code == 403, r.text

    r = client.post(
        f"/api/admin/users/{TARGET_UID}/admin",
        headers={"Authorization": f"Bearer {token}"},
        json={"is_admin": True},
    )
    assert r.status_code == 403, r.text

    # No mutation, no audit, no Clerk call.
    assert store.users[TARGET_UID]["approved"] is False
    assert store.users[TARGET_UID]["is_admin"] is False
    assert store.audit == []
    assert FakeAsyncClient.patch_calls == []


def test_admin_can_approve_and_audit_log_written(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    store.add_user(ADMIN_UID, email="boss@example.com", approved=True, is_admin=True)
    store.add_user(TARGET_UID, email="t@example.com", approved=False, is_admin=False)
    token = _mint(_keypair, sub=ADMIN_UID)

    r = client.post(
        f"/api/admin/users/{TARGET_UID}/approved",
        headers={"Authorization": f"Bearer {token}"},
        json={"approved": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] is True
    assert body["clerk_user_id"] == TARGET_UID

    # The flag flipped in our store.
    assert store.users[TARGET_UID]["approved"] is True

    # An audit row landed.
    set_approved_rows = [a for a in store.audit if a["action"] == "set_approved"]
    assert len(set_approved_rows) == 1
    row = set_approved_rows[0]
    assert row["actor_user_id"] == ADMIN_UID
    assert row["target_user_id"] == TARGET_UID
    assert "true" in row["after_value"]

    # Clerk mirror PATCH attempted with public_metadata.approved=true.
    mirror = [c for c in FakeAsyncClient.patch_calls if TARGET_UID in c["url"]]
    assert len(mirror) == 1
    assert mirror[0]["json"] == {"public_metadata": {"approved": True}}
    assert "metadata" in mirror[0]["url"]


def test_admin_can_revoke_approval(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    store.add_user(ADMIN_UID, email="boss@example.com", approved=True, is_admin=True)
    store.add_user(TARGET_UID, email="t@example.com", approved=True, is_admin=False)
    token = _mint(_keypair, sub=ADMIN_UID)

    r = client.post(
        f"/api/admin/users/{TARGET_UID}/approved",
        headers={"Authorization": f"Bearer {token}"},
        json={"approved": False},
    )
    assert r.status_code == 200, r.text
    assert store.users[TARGET_UID]["approved"] is False

    rows = [a for a in store.audit if a["action"] == "set_approved"]
    assert len(rows) == 1
    assert "false" in rows[0]["after_value"]

    mirror = [c for c in FakeAsyncClient.patch_calls if TARGET_UID in c["url"]]
    assert mirror[-1]["json"] == {"public_metadata": {"approved": False}}


def test_cannot_revoke_approval_while_target_is_admin(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    """Invariant: admin implies approved — caller must demote first."""
    store.add_user(ADMIN_UID, email="boss@example.com", approved=True, is_admin=True)
    store.add_user(TARGET_UID, email="t@example.com", approved=True, is_admin=True)
    token = _mint(_keypair, sub=ADMIN_UID)

    r = client.post(
        f"/api/admin/users/{TARGET_UID}/approved",
        headers={"Authorization": f"Bearer {token}"},
        json={"approved": False},
    )
    assert r.status_code == 409, r.text
    # Nothing changed, nothing logged, nothing mirrored.
    assert store.users[TARGET_UID]["approved"] is True
    assert store.audit == []
    assert FakeAsyncClient.patch_calls == []


def test_admin_can_promote_and_audit_log_written(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    store.add_user(ADMIN_UID, email="boss@example.com", approved=True, is_admin=True)
    store.add_user(TARGET_UID, email="t@example.com", approved=False, is_admin=False)
    token = _mint(_keypair, sub=ADMIN_UID)

    r = client.post(
        f"/api/admin/users/{TARGET_UID}/admin",
        headers={"Authorization": f"Bearer {token}"},
        json={"is_admin": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_admin"] is True
    # Promoting also implicitly approves.
    assert body["approved"] is True
    assert store.users[TARGET_UID]["is_admin"] is True
    assert store.users[TARGET_UID]["approved"] is True

    rows = [a for a in store.audit if a["action"] == "set_admin"]
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == ADMIN_UID
    assert rows[0]["target_user_id"] == TARGET_UID
    assert "true" in rows[0]["after_value"]

    # Clerk mirror with admin=true.
    mirror = [c for c in FakeAsyncClient.patch_calls if TARGET_UID in c["url"]]
    assert mirror[-1]["json"] == {"public_metadata": {"admin": True}}


def test_admin_can_demote(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    store.add_user(ADMIN_UID, email="boss@example.com", approved=True, is_admin=True)
    store.add_user(TARGET_UID, email="t@example.com", approved=True, is_admin=True)
    token = _mint(_keypair, sub=ADMIN_UID)

    r = client.post(
        f"/api/admin/users/{TARGET_UID}/admin",
        headers={"Authorization": f"Bearer {token}"},
        json={"is_admin": False},
    )
    assert r.status_code == 200, r.text
    assert store.users[TARGET_UID]["is_admin"] is False
    # Demote leaves approval intact.
    assert store.users[TARGET_UID]["approved"] is True

    rows = [a for a in store.audit if a["action"] == "set_admin"]
    assert len(rows) == 1
    assert "false" in rows[0]["after_value"]

    mirror = [c for c in FakeAsyncClient.patch_calls if TARGET_UID in c["url"]]
    assert mirror[-1]["json"] == {"public_metadata": {"admin": False}}
