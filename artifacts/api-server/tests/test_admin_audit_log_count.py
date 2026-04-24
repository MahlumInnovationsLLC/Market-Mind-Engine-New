"""Tests for the audit-log filter + total-count behaviour.

Operators rely on the ``total`` field returned by ``GET /api/admin/audit-log``
to render "Showing N–M of T" and to disable the Next button on the actual
last page. These tests pin down two things:

  * ``count_audit_log`` honours the same filter rules as ``list_audit_log``
    (target / actor as id-or-email substring, action as exact match).
  * ``GET /api/admin/audit-log`` surfaces ``total`` in its JSON response and
    the SQL placeholders line up correctly when filters are applied.

The asyncpg pool is stubbed with an in-memory backend so no DB is needed.
"""
from __future__ import annotations

import asyncio
import re
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
ADMIN_UID = "user_admin_audit"


# ---------------------------------------------------------------------------
# JWT plumbing (mirror of test_admin_endpoints, kept local for clarity)
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
# Tiny in-memory audit_log + app_users backend
# ---------------------------------------------------------------------------


class FakeAuditStore:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.entries: list[dict[str, Any]] = []
        self._next_id = 1

    def add_user(self, uid: str, *, email: str, approved: bool, is_admin: bool) -> None:
        self.users[uid] = {
            "clerk_user_id": uid,
            "email": email,
            "display_name": None,
            "approved": approved,
            "is_admin": is_admin,
            "denied_at": None,
            "activity_count": 0,
        }

    def add_entry(
        self,
        *,
        actor_user_id: str = "actor_uid",
        actor_email: str | None = None,
        target_user_id: str = "target_uid",
        target_email: str | None = None,
        action: str = "set_approved",
    ) -> None:
        self.entries.append(
            {
                "id": self._next_id,
                "actor_user_id": actor_user_id,
                "actor_email": actor_email,
                "target_user_id": target_user_id,
                "target_email": target_email,
                "action": action,
                "before_value": None,
                "after_value": None,
                "created_at": f"2026-04-{self._next_id:02d}T00:00:00Z",
            }
        )
        self._next_id += 1


def _matches_filters(
    entry: dict[str, Any],
    *,
    target: str | None,
    actor: str | None,
    action: str | None,
) -> bool:
    def _userish(col_id: str, col_email: str, value: str | None) -> bool:
        if not value:
            return True
        v = value.strip()
        if not v:
            return True
        if entry.get(col_id) == v:
            return True
        em = (entry.get(col_email) or "").lower()
        return v.lower() in em

    if not _userish("target_user_id", "target_email", target):
        return False
    if not _userish("actor_user_id", "actor_email", actor):
        return False
    if action and action.strip() and entry.get("action") != action.strip():
        return False
    return True


class FakeConn:
    """Handles only the queries the audit-log code paths issue."""

    def __init__(self, store: FakeAuditStore) -> None:
        self.store = store

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        norm = " ".join(query.split())
        upper = norm.upper()
        if upper.startswith("SELECT * FROM APP_USERS WHERE CLERK_USER_ID"):
            return self.store.users.get(args[0])
        if upper.startswith("SELECT COUNT(*) AS N FROM ADMIN_AUDIT_LOG"):
            target, actor, action = self._parse_filters(norm, args)
            n = sum(
                1
                for e in self.store.entries
                if _matches_filters(e, target=target, actor=actor, action=action)
            )
            return {"n": n}
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        norm = " ".join(query.split())
        upper = norm.upper()
        if "FROM ADMIN_AUDIT_LOG" in upper and "LIMIT $1 OFFSET $2" in upper:
            limit, offset = int(args[0]), int(args[1])
            target, actor, action = self._parse_filters(norm, args)
            matched = [
                e
                for e in self.store.entries
                if _matches_filters(e, target=target, actor=actor, action=action)
            ]
            matched.sort(key=lambda e: e["created_at"], reverse=True)
            return [dict(e) for e in matched[offset : offset + limit]]
        if upper.startswith("SELECT DISTINCT ACTION FROM ADMIN_AUDIT_LOG"):
            seen: list[str] = []
            for e in self.store.entries:
                a = e.get("action")
                if a and a not in seen:
                    seen.append(a)
            return [{"action": a} for a in sorted(seen)]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"

    @staticmethod
    def _parse_filters(
        norm_sql: str, args: tuple[Any, ...]
    ) -> tuple[str | None, str | None, str | None]:
        """Reconstruct (target, actor, action) from the SQL + bound args.

        We can't just look at args by position because the helper packs
        target/actor as two consecutive params (exact + ``%lower%``) only
        when supplied. We disambiguate by scanning the WHERE clause.
        """
        # Args layout: list query is [limit, offset, *filter_args];
        # count query is [*filter_args]. Strip leading limit/offset for
        # the list query.
        if "LIMIT $1 OFFSET $2" in norm_sql.upper():
            filter_args = list(args[2:])
        else:
            filter_args = list(args)

        target = actor = action = None
        # Restrict parsing to the WHERE clause so SELECT-list column names
        # don't get mistaken for filter fragments.
        m = re.search(r"WHERE (.*?)(?: ORDER BY| LIMIT |$)", norm_sql)
        where_sql = m.group(1) if m else ""
        for kind in re.findall(
            r"target_user_id = \$|actor_user_id = \$|action = \$",
            where_sql,
        ):
            if kind.startswith("target_user_id"):
                target = filter_args.pop(0)
                filter_args.pop(0)  # the %like% twin
            elif kind.startswith("actor_user_id"):
                actor = filter_args.pop(0)
                filter_args.pop(0)
            elif kind.startswith("action = "):
                action = filter_args.pop(0)
        return target, actor, action


# ---------------------------------------------------------------------------
# Fake Clerk httpx client (we only need fetchrow-level admin gating to pass)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 404, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def patch(self, *_a: Any, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(200, {})

    async def get(self, *_a: Any, **_kw: Any) -> _FakeResponse:
        return _FakeResponse(404, {})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> FakeAuditStore:
    return FakeAuditStore()


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch: pytest.MonkeyPatch, _keypair, store: FakeAuditStore):
    auth_module._frontend_api_host.cache_clear()
    auth_module._issuer.cache_clear()
    auth_module._jwks_client.cache_clear()
    auth_module._user_cache.clear()
    monkeypatch.setattr(
        auth_module,
        "_jwks_client",
        lambda: _StubJWKSClient(_keypair.public_key()),
    )
    monkeypatch.setattr(admin_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_audit_count")
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)

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
# Direct unit tests for count_audit_log
# ---------------------------------------------------------------------------


def test_count_audit_log_no_filters_returns_total(store: FakeAuditStore) -> None:
    for _ in range(7):
        store.add_entry()
    n = asyncio.run(admin_module.count_audit_log())
    assert n == 7


def test_count_audit_log_action_filter_matches_list(
    store: FakeAuditStore,
) -> None:
    for _ in range(3):
        store.add_entry(action="set_approved")
    for _ in range(2):
        store.add_entry(action="set_admin")

    listed = asyncio.run(
        admin_module.list_audit_log(100, 0, action="set_admin")
    )
    counted = asyncio.run(admin_module.count_audit_log(action="set_admin"))
    assert counted == 2
    assert counted == len(listed)


def test_count_audit_log_target_email_substring(
    store: FakeAuditStore,
) -> None:
    store.add_entry(target_user_id="u1", target_email="alice@example.com")
    store.add_entry(target_user_id="u2", target_email="bob@example.com")
    store.add_entry(target_user_id="u3", target_email="alicia@example.com")

    counted = asyncio.run(admin_module.count_audit_log(target="ali"))
    assert counted == 2  # alice + alicia
    listed = asyncio.run(admin_module.list_audit_log(100, 0, target="ali"))
    assert len(listed) == 2


def test_count_audit_log_combined_filters(store: FakeAuditStore) -> None:
    store.add_entry(actor_email="boss@example.com", action="set_approved")
    store.add_entry(actor_email="boss@example.com", action="set_admin")
    store.add_entry(actor_email="other@example.com", action="set_approved")

    counted = asyncio.run(
        admin_module.count_audit_log(actor="boss", action="set_approved")
    )
    assert counted == 1


# ---------------------------------------------------------------------------
# Endpoint test: /api/admin/audit-log returns `total` and paginates correctly
# ---------------------------------------------------------------------------


def test_audit_log_endpoint_returns_total_and_paginates(
    client: TestClient, store: FakeAuditStore, _keypair
) -> None:
    store.add_user(ADMIN_UID, email="boss@example.com", approved=True, is_admin=True)
    for _ in range(12):
        store.add_entry(action="set_approved")
    for _ in range(3):
        store.add_entry(action="set_admin")

    token = _mint(_keypair, sub=ADMIN_UID)
    headers = {"Authorization": f"Bearer {token}"}

    # Page 1 of an unfiltered query: total = 15, items = 10 (limit).
    r = client.get(
        "/api/admin/audit-log?limit=10&offset=0",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 15
    assert len(body["items"]) == 10

    # Page 2 of the same query: items = 5 (the tail).
    r = client.get(
        "/api/admin/audit-log?limit=10&offset=10",
        headers=headers,
    )
    body = r.json()
    assert body["total"] == 15
    assert len(body["items"]) == 5

    # Filtered query: total reflects only matching rows, not the page size.
    r = client.get(
        "/api/admin/audit-log?limit=10&offset=0&action=set_admin",
        headers=headers,
    )
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert all(e["action"] == "set_admin" for e in body["items"])
