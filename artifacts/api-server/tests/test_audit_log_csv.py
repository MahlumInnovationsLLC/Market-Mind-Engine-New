"""Tests for the admin audit-log CSV export (task #292).

Covers both the helper (``app.admin.export_audit_log_csv``) and the
HTTP endpoint (``GET /api/admin/audit-log.csv``).  The DB layer and
auth are stubbed in-process so the tests run under ``uv run pytest``
without any network or Postgres.
"""
from __future__ import annotations

import asyncio
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
ADMIN_UID = "user_admin_csv"


# ---------------------------------------------------------------------------
# JWKS / JWT plumbing (mirrors test_admin_endpoints.py)
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
            "sid": "sess_csv",
        },
        private_key,
        algorithm="RS256",
    )


# ---------------------------------------------------------------------------
# Helper: directly test the CSV builder by stubbing `list_audit_log`
# ---------------------------------------------------------------------------


def test_export_audit_log_csv_writes_expected_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Header row + a single populated row, JSON-encoded blobs, and the
    actor/target preferring the email when present.
    """
    canned = [
        {
            "id": 7,
            "actor_user_id": "user_actor",
            "actor_email": "actor@example.com",
            "target_user_id": "user_target",
            "target_email": "target@example.com",
            "action": "set_admin",
            "before_value": {"is_admin": False},
            "after_value": {"is_admin": True},
            "created_at": "2026-04-23T12:00:00Z",
        }
    ]

    async def fake_list(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        return canned

    monkeypatch.setattr(admin_module, "list_audit_log", fake_list)
    csv_text = asyncio.run(admin_module.export_audit_log_csv())
    lines = csv_text.strip().splitlines()
    assert lines[0] == "created_at,actor,target,action,before_value,after_value"
    # Body uses email labels and JSON-encodes the payload columns.
    assert lines[1].startswith("2026-04-23T12:00:00Z,actor@example.com,target@example.com,set_admin,")
    assert '"{""is_admin"": false}"' in lines[1]
    assert '"{""is_admin"": true}"' in lines[1]


def test_export_audit_log_csv_falls_back_to_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """When email is missing, the user id is written instead so the
    column is never blank for a real audit row.
    """
    canned = [
        {
            "id": 1,
            "actor_user_id": "user_actor",
            "actor_email": None,
            "target_user_id": "user_target",
            "target_email": None,
            "action": "clear_denied",
            "before_value": None,
            "after_value": None,
            "created_at": "2026-04-22T01:02:03Z",
        }
    ]

    async def fake_list(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        return canned

    monkeypatch.setattr(admin_module, "list_audit_log", fake_list)
    csv_text = asyncio.run(admin_module.export_audit_log_csv())
    lines = csv_text.strip().splitlines()
    assert lines[1] == "2026-04-22T01:02:03Z,user_actor,user_target,clear_denied,,"


def test_export_audit_log_csv_forwards_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_list(limit: int, offset: int, **kw: Any) -> list[dict[str, Any]]:
        captured["limit"] = limit
        captured["offset"] = offset
        captured.update(kw)
        return []

    monkeypatch.setattr(admin_module, "list_audit_log", fake_list)
    asyncio.run(
        admin_module.export_audit_log_csv(
            target="alice@example.com",
            actor="user_admin",
            action="set_approved",
            limit=250,
        )
    )
    assert captured == {
        "limit": 250,
        "offset": 0,
        "target": "alice@example.com",
        "actor": "user_admin",
        "action": "set_approved",
    }


def test_export_audit_log_csv_clamps_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_list(limit: int, offset: int, **kw: Any) -> list[dict[str, Any]]:
        captured["limit"] = limit
        return []

    monkeypatch.setattr(admin_module, "list_audit_log", fake_list)
    # Above the cap → clamped to 50_000.
    asyncio.run(admin_module.export_audit_log_csv(limit=10**9))
    assert captured["limit"] == 50000
    # Below 1 → clamped up to 1.
    asyncio.run(admin_module.export_audit_log_csv(limit=0))
    assert captured["limit"] == 1


# ---------------------------------------------------------------------------
# Endpoint test: full HTTP path with auth, filter forwarding, and
# Content-Disposition headers.
# ---------------------------------------------------------------------------


class _AdminOnlyConn:
    """Minimal asyncpg stand-in: only knows how to look up the admin row
    that ``require_admin`` consults.  All other queries return None / [].
    """

    def __init__(self, admin_uid: str) -> None:
        self.admin_uid = admin_uid

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split()).upper()
        if q.startswith("SELECT * FROM APP_USERS WHERE CLERK_USER_ID"):
            if args[0] == self.admin_uid:
                return {
                    "clerk_user_id": self.admin_uid,
                    "email": "boss@example.com",
                    "display_name": None,
                    "approved": True,
                    "is_admin": True,
                    "denied_at": None,
                }
            return None
        return None

    async def fetch(self, *_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, *_a: Any, **_kw: Any) -> str:
        return "OK"


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch: pytest.MonkeyPatch, _keypair):
    auth_module._frontend_api_host.cache_clear()
    auth_module._issuer.cache_clear()
    auth_module._jwks_client.cache_clear()
    auth_module._user_cache.clear()
    monkeypatch.setattr(
        auth_module,
        "_jwks_client",
        lambda: _StubJWKSClient(_keypair.public_key()),
    )
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)

    @asynccontextmanager
    async def fake_acquire():
        yield _AdminOnlyConn(ADMIN_UID)

    monkeypatch.setattr(db_module, "acquire", fake_acquire)
    monkeypatch.setattr(admin_module.db, "acquire", fake_acquire)
    yield
    auth_module._user_cache.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_endpoint_requires_admin(client: TestClient) -> None:
    """Unauthenticated callers must be rejected."""
    r = client.get("/api/admin/audit-log.csv")
    assert r.status_code in (401, 403)


def test_endpoint_returns_csv_with_attachment_headers(
    client: TestClient,
    _keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_list(limit: int, offset: int, **kw: Any) -> list[dict[str, Any]]:
        captured["limit"] = limit
        captured.update(kw)
        return [
            {
                "id": 1,
                "actor_user_id": "user_actor",
                "actor_email": "actor@example.com",
                "target_user_id": "user_target",
                "target_email": "target@example.com",
                "action": "set_approved",
                "before_value": {"approved": False},
                "after_value": {"approved": True},
                "created_at": "2026-04-23T09:00:00Z",
            }
        ]

    monkeypatch.setattr(admin_module, "list_audit_log", fake_list)
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/audit-log.csv?target=target@example.com&actor=user_actor&action=set_approved&limit=42",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    dispo = r.headers.get("content-disposition", "")
    assert "attachment" in dispo
    assert "axiom-audit-log-" in dispo
    assert dispo.endswith('.csv"')

    body = r.text.splitlines()
    assert body[0] == "created_at,actor,target,action,before_value,after_value"
    assert body[1].startswith(
        "2026-04-23T09:00:00Z,actor@example.com,target@example.com,set_approved,"
    )

    # Filters forwarded into list_audit_log; limit clamped through the helper.
    assert captured["limit"] == 42
    assert captured["target"] == "target@example.com"
    assert captured["actor"] == "user_actor"
    assert captured["action"] == "set_approved"


def test_endpoint_handles_empty_filter_set(
    client: TestClient,
    _keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No filters → still returns a header-only CSV body."""

    async def fake_list(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(admin_module, "list_audit_log", fake_list)
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/audit-log.csv",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.text.strip() == "created_at,actor,target,action,before_value,after_value"
