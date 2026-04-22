"""Unit tests for the Telegram concierge bot (task #111).

The tests stub out:
* the asyncpg pool — replaced with an in-memory fake so we never touch
  Supabase from the test harness;
* the outbound Telegram HTTP client — replaced with a recorder that
  captures every ``sendMessage`` call;
* the emergency-stop side effects — wrapped so we can assert the
  helper is invoked with the right actor + reason.

End-to-end coverage:
* ``/start CODE`` binds an issued link code to a Telegram user.
* ``/start`` with a stale code is rejected.
* ``/status``, ``/positions`` and ``/halts`` reply with the same
  formatters the queries module produces.
* ``/estop`` requires admin, then issues a confirm code.
* ``/confirm`` with the right code triggers
  ``execute_emergency_stop`` exactly once with the bound operator as
  actor.
* ``/confirm`` with the wrong code drops the pending request.
* Group chats are silently ignored.
* Unbound chats get a "link first" response.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import db as _db
from app.telegram import bindings as _bindings, bot as _bot, estop as _estop, queries as _queries


# ---------------------------------------------------------------------------
# In-memory DB stand-in for the few tables the tests touch.
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self) -> None:
        # telegram_bindings keyed by clerk_user_id
        self.bindings: dict[str, dict[str, Any]] = {}
        # admin allow-list
        self.admins: set[str] = set()

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO telegram_bindings"):
            cuid, code, expires = args
            row = self.bindings.get(cuid) or {"clerk_user_id": cuid}
            row["link_code"] = code
            row["link_code_expires_at"] = expires
            self.bindings[cuid] = row
            return _r(row)
        if q.startswith("SELECT * FROM telegram_bindings WHERE clerk_user_id"):
            row = self.bindings.get(args[0])
            return _r(row) if row else None
        if q.startswith("SELECT * FROM telegram_bindings WHERE telegram_user_id"):
            tg = int(args[0])
            for row in self.bindings.values():
                if row.get("telegram_user_id") == tg:
                    return _r(row)
            return None
        if q.startswith("UPDATE telegram_bindings"):
            code = args[0]
            tg, chat, uname = int(args[1]), int(args[2]), args[3]
            from datetime import datetime, timezone
            for row in self.bindings.values():
                if (
                    row.get("link_code") == code
                    and row.get("link_code_expires_at")
                    and row["link_code_expires_at"] > datetime.now(timezone.utc)
                ):
                    row.update({
                        "telegram_user_id": tg,
                        "telegram_chat_id": chat,
                        "telegram_username": uname,
                        "link_code": None,
                        "link_code_expires_at": None,
                        "bound_at": datetime.now(timezone.utc),
                        "last_seen_at": datetime.now(timezone.utc),
                    })
                    return _r(row)
            return None
        raise AssertionError(f"unexpected fetchrow query: {q[:120]}")

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE telegram_bindings SET telegram_user_id = NULL"):
            tg = int(args[0])
            for row in self.bindings.values():
                if row.get("telegram_user_id") == tg:
                    row["telegram_user_id"] = None
                    row["telegram_chat_id"] = None
                    row["telegram_username"] = None
                    row["bound_at"] = None
            return "UPDATE 0"
        if q.startswith("UPDATE telegram_bindings SET last_seen_at"):
            return "UPDATE 0"
        if q.startswith("DELETE FROM telegram_bindings"):
            n = 0
            for cuid in list(self.bindings):
                if cuid == args[0]:
                    del self.bindings[cuid]
                    n += 1
            return f"DELETE {n}"
        # Tolerate any other execute (queries.py / estop.py side effects
        # are stubbed out at the module level in the tests below).
        return "OK"

    async def fetch(self, *_args, **_kwargs):
        return []

    async def fetchval(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT 1 FROM telegram_bindings WHERE link_code"):
            from datetime import datetime, timezone
            code = args[0]
            for row in self.bindings.values():
                if (
                    row.get("link_code") == code
                    and row.get("link_code_expires_at")
                    and row["link_code_expires_at"] > datetime.now(timezone.utc)
                ):
                    return 1
            return None
        return 0


def _r(row: dict[str, Any] | None):
    """Wrap a dict so it quacks like an asyncpg Record (dict(row) works)."""
    return row


# ---------------------------------------------------------------------------
# Telegram HTTP recorder — replaces httpx.AsyncClient.post / .get.
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecorderClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> _Resp:
        self.sent.append({"url": url, "json": json})
        return _Resp({"ok": True})

    async def get(self, url: str, *, params: dict[str, Any] | None = None) -> _Resp:
        return _Resp({"ok": True, "result": []})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeDB:
    fdb = _FakeDB()
    monkeypatch.setattr(_db, "fetchrow", fdb.fetchrow)
    monkeypatch.setattr(_db, "execute", fdb.execute)
    monkeypatch.setattr(_db, "fetch", fdb.fetch)
    monkeypatch.setattr(_db, "fetchval", fdb.fetchval)
    return fdb


@pytest.fixture
def recorder() -> _RecorderClient:
    return _RecorderClient()


@pytest.fixture(autouse=True)
def _stub_queries(monkeypatch: pytest.MonkeyPatch):
    """Replace the read-only formatters with sentinel strings so the
    tests can assert routing without needing the dashboard module."""
    async def _status() -> str:
        return "STATUS_TEXT"

    async def _positions(limit: int = 15) -> str:
        return f"POSITIONS_TEXT(limit={limit})"

    async def _halts(limit: int = 5) -> str:
        return f"HALTS_TEXT(limit={limit})"

    monkeypatch.setattr(_queries, "status_text", _status)
    monkeypatch.setattr(_queries, "positions_text", _positions)
    monkeypatch.setattr(_queries, "halts_text", _halts)


@pytest.fixture
def admin_check(monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB):
    """check_admin honours the fake_db.admins set."""
    from app import auth as auth_mod

    async def _check(user) -> bool:
        return user.user_id in fake_db.admins

    monkeypatch.setattr(auth_mod, "check_admin", _check)
    return fake_db.admins


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(text: str, *, tg_user: int = 555, chat: int = 555, chat_type: str = "private", username: str = "op") -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "text": text,
            "chat": {"id": chat, "type": chat_type},
            "from": {"id": tg_user, "username": username},
        },
    }


async def _dispatch(recorder: _RecorderClient, update: dict[str, Any]) -> None:
    await _bot.handle_update_for_test(update, client=recorder)  # type: ignore[arg-type]


def _last_text(recorder: _RecorderClient) -> str:
    assert recorder.sent, "no message was sent"
    return recorder.sent[-1]["json"]["text"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unbound_chat_is_prompted_to_link(fake_db: _FakeDB, recorder: _RecorderClient) -> None:
    asyncio.run(_dispatch(recorder, _msg("/status")))
    assert "/start CODE" in _last_text(recorder)


def test_group_chats_are_ignored(fake_db: _FakeDB, recorder: _RecorderClient) -> None:
    asyncio.run(_dispatch(recorder, _msg("/status", chat_type="group")))
    assert recorder.sent == []


def test_start_binds_link_code(fake_db: _FakeDB, recorder: _RecorderClient) -> None:
    async def _flow() -> None:
        row = await _bindings.issue_link_code("user_abc")
        code = row["link_code"]
        await _dispatch(recorder, _msg(f"/start {code}", tg_user=42, chat=42, username="alice"))

    asyncio.run(_flow())
    assert "Linked." in _last_text(recorder)
    bound = fake_db.bindings["user_abc"]
    assert bound["telegram_user_id"] == 42
    assert bound["link_code"] is None


def test_start_rejects_unknown_code(fake_db: _FakeDB, recorder: _RecorderClient) -> None:
    asyncio.run(_dispatch(recorder, _msg("/start NOPECODE", tg_user=42, chat=42)))
    assert "invalid or has expired" in _last_text(recorder)


def test_invalid_start_does_not_unbind_existing_binding(
    fake_db: _FakeDB, recorder: _RecorderClient
) -> None:
    """Regression: an already-bound operator typing `/start WRONGCODE`
    must not silently lose their binding via the cleanup UPDATE that
    runs before the claim. claim_link_code now validates first."""
    async def _flow() -> None:
        row = await _bindings.issue_link_code("user_op")
        await _dispatch(recorder, _msg(f"/start {row['link_code']}", tg_user=77, chat=77))
        assert fake_db.bindings["user_op"]["telegram_user_id"] == 77
        recorder.sent.clear()
        await _dispatch(recorder, _msg("/start NOPECODE", tg_user=77, chat=77))

    asyncio.run(_flow())
    # Binding must still be intact.
    assert fake_db.bindings["user_op"]["telegram_user_id"] == 77
    assert fake_db.bindings["user_op"]["bound_at"] is not None
    assert "invalid or has expired" in _last_text(recorder)


def test_status_routes_to_queries_module(fake_db: _FakeDB, recorder: _RecorderClient, admin_check) -> None:
    async def _flow() -> None:
        row = await _bindings.issue_link_code("user_abc")
        await _dispatch(recorder, _msg(f"/start {row['link_code']}", tg_user=42, chat=42))
        recorder.sent.clear()
        await _dispatch(recorder, _msg("/status", tg_user=42, chat=42))
        await _dispatch(recorder, _msg("/positions", tg_user=42, chat=42))
        await _dispatch(recorder, _msg("/halts", tg_user=42, chat=42))

    asyncio.run(_flow())
    texts = [s["json"]["text"] for s in recorder.sent]
    assert texts == ["STATUS_TEXT", "POSITIONS_TEXT(limit=15)", "HALTS_TEXT(limit=5)"]


def test_estop_requires_admin(fake_db: _FakeDB, recorder: _RecorderClient, admin_check) -> None:
    async def _flow() -> None:
        row = await _bindings.issue_link_code("user_op")
        await _dispatch(recorder, _msg(f"/start {row['link_code']}", tg_user=10, chat=10))
        recorder.sent.clear()
        await _dispatch(recorder, _msg("/estop runaway loop", tg_user=10, chat=10))

    asyncio.run(_flow())
    assert "restricted to platform admins" in _last_text(recorder)


def test_estop_confirm_flow(monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB, recorder: _RecorderClient, admin_check) -> None:
    calls: list[dict[str, Any]] = []

    async def _fake_exec(*, actor_user_id: str, actor_email, reason: str) -> dict[str, Any]:
        calls.append({"actor": actor_user_id, "reason": reason})
        return {
            "ok": True,
            "canceled_orders": 3,
            "trading_mode": "halt",
            "reason": reason,
            "kill_switch": {"halted": True},
        }

    monkeypatch.setattr(_estop, "execute_emergency_stop", _fake_exec)

    async def _flow() -> str:
        admin_check.add("user_admin")
        row = await _bindings.issue_link_code("user_admin")
        await _dispatch(recorder, _msg(f"/start {row['link_code']}", tg_user=99, chat=99))
        recorder.sent.clear()
        await _dispatch(recorder, _msg("/estop runaway loop", tg_user=99, chat=99))
        prompt = _last_text(recorder)
        # Pull the 6-digit code off the prompt — bot embeds it directly.
        import re
        m = re.search(r"/confirm (\d{6})", prompt)
        assert m, prompt
        code = m.group(1)
        await _dispatch(recorder, _msg(f"/confirm {code}", tg_user=99, chat=99))
        return _last_text(recorder)

    final = asyncio.run(_flow())
    assert "EMERGENCY STOP fired." in final
    assert "canceled orders: 3" in final
    assert calls == [{"actor": "user_admin", "reason": "runaway loop"}]


def test_confirm_with_wrong_code_drops_pending(monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB, recorder: _RecorderClient, admin_check) -> None:
    fired: list[Any] = []

    async def _fake_exec(**_kw):
        fired.append(_kw)
        return {}

    monkeypatch.setattr(_estop, "execute_emergency_stop", _fake_exec)

    async def _flow() -> None:
        admin_check.add("user_admin")
        row = await _bindings.issue_link_code("user_admin")
        await _dispatch(recorder, _msg(f"/start {row['link_code']}", tg_user=11, chat=11))
        await _dispatch(recorder, _msg("/estop", tg_user=11, chat=11))
        await _dispatch(recorder, _msg("/confirm 000000", tg_user=11, chat=11))
        # Now even the right /confirm should fail because the bad attempt
        # consumed the pending entry.
        await _dispatch(recorder, _msg("/confirm 123456", tg_user=11, chat=11))

    asyncio.run(_flow())
    assert fired == []
    # Final reply must say "no matching pending /estop"
    assert "No matching pending /estop" in recorder.sent[-1]["json"]["text"]
