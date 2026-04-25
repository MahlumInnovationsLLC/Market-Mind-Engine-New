"""API-level tests for the dashboard Telegram notify-push toggle (task #418).

Task #186 added the per-binding ``notify_push`` mute flag plus ``/mute``
and ``/unmute`` chat commands. This task wires the same toggle into the
operator dashboard via ``POST /api/me/telegram/notify-push`` and surfaces
the current value on the existing ``GET /api/me/telegram`` endpoint.

The store-level coverage in ``test_telegram_notify_push.py`` already
exercises ``bindings.set_notify_push`` directly. These tests drive the
FastAPI handlers through ``TestClient`` so the auth gate, request shape,
404-on-missing-binding behaviour, and refreshed-binding response are
covered end-to-end.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db as _db
from app.auth import AuthedUser, require_approved_user
from app.main import app
from app.telegram import bot as telegram_bot


_USER_ID = "user_dashboard_op"


class _FakeDB:
    """Tiny in-memory stand-in for the telegram_bindings table."""

    def __init__(self) -> None:
        self.bindings: dict[str, dict[str, Any]] = {}

    def add_binding(
        self,
        clerk_user_id: str = _USER_ID,
        *,
        notify_push: bool = True,
        bound: bool = True,
    ) -> None:
        self.bindings[clerk_user_id] = {
            "clerk_user_id": clerk_user_id,
            "telegram_user_id": 4242,
            "telegram_chat_id": 4242,
            "telegram_username": "op",
            "link_code": None,
            "link_code_expires_at": None,
            "bound_at": (
                datetime.now(timezone.utc) if bound else None
            ),
            "last_seen_at": None,
            "notify_push": notify_push,
        }

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM telegram_bindings WHERE clerk_user_id"):
            return self.bindings.get(args[0])
        return None

    async def fetch(self, *_a, **_kw):
        return []

    async def fetchval(self, *_a, **_kw):
        return 0

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE telegram_bindings SET notify_push"):
            cuid, val = args[0], bool(args[1])
            row = self.bindings.get(cuid)
            if row is None:
                return "UPDATE 0"
            row["notify_push"] = val
            return "UPDATE 1"
        return "OK"


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeDB:
    fdb = _FakeDB()
    monkeypatch.setattr(_db, "fetch", fdb.fetch)
    monkeypatch.setattr(_db, "fetchrow", fdb.fetchrow)
    monkeypatch.setattr(_db, "fetchval", fdb.fetchval)
    monkeypatch.setattr(_db, "execute", fdb.execute)
    return fdb


@pytest.fixture
def stub_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The /api/me/telegram views call telegram_bot.bot_username() and
    is_running(); stub them so tests don't need a real bot config."""
    monkeypatch.setattr(telegram_bot, "bot_username", lambda: "axiom_bot")
    monkeypatch.setattr(telegram_bot, "is_running", lambda: True)


@pytest.fixture
def override_user():
    async def _fake_user() -> AuthedUser:
        return AuthedUser(user_id=_USER_ID, session_id="sess_test", claims={})

    app.dependency_overrides[require_approved_user] = _fake_user
    yield
    app.dependency_overrides.pop(require_approved_user, None)


@pytest.fixture
def client(fake_db, stub_bot, override_user) -> TestClient:
    return TestClient(app)


def test_get_status_surfaces_notify_push_for_linked_binding(
    client: TestClient, fake_db: _FakeDB
) -> None:
    fake_db.add_binding(notify_push=True)
    r = client.get("/api/me/telegram")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linked"] is True
    assert body["notify_push"] is True

    fake_db.bindings[_USER_ID]["notify_push"] = False
    r = client.get("/api/me/telegram")
    assert r.json()["notify_push"] is False


def test_get_status_defaults_notify_push_true_for_legacy_null(
    client: TestClient, fake_db: _FakeDB
) -> None:
    """Legacy binding rows that pre-date the column show up as opted-in."""
    fake_db.add_binding(notify_push=True)
    fake_db.bindings[_USER_ID]["notify_push"] = None
    r = client.get("/api/me/telegram")
    assert r.status_code == 200, r.text
    assert r.json()["notify_push"] is True


def test_post_notify_push_mutes_binding(
    client: TestClient, fake_db: _FakeDB
) -> None:
    fake_db.add_binding(notify_push=True)
    r = client.post("/api/me/telegram/notify-push", json={"enabled": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linked"] is True
    assert body["notify_push"] is False
    assert fake_db.bindings[_USER_ID]["notify_push"] is False


def test_post_notify_push_unmutes_binding(
    client: TestClient, fake_db: _FakeDB
) -> None:
    fake_db.add_binding(notify_push=False)
    r = client.post("/api/me/telegram/notify-push", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["notify_push"] is True
    assert fake_db.bindings[_USER_ID]["notify_push"] is True


def test_post_notify_push_returns_404_when_no_binding(
    client: TestClient, fake_db: _FakeDB
) -> None:
    """Toggling before linking returns a structured 404 instead of a
    silent no-op so the dashboard can show a meaningful error."""
    assert _USER_ID not in fake_db.bindings
    r = client.post("/api/me/telegram/notify-push", json={"enabled": False})
    assert r.status_code == 404, r.text
    detail = r.json().get("detail")
    assert isinstance(detail, dict) and detail.get("error") == "telegram_binding_not_found"


def test_post_notify_push_rejects_missing_field(
    client: TestClient, fake_db: _FakeDB
) -> None:
    fake_db.add_binding(notify_push=True)
    r = client.post("/api/me/telegram/notify-push", json={})
    assert r.status_code == 422, r.text
    # The store row must remain untouched on validation failure.
    assert fake_db.bindings[_USER_ID]["notify_push"] is True
