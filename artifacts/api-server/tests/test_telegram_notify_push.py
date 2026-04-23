"""Tests for proactive Telegram pings (task #186).

Covers:
* `notify.broadcast` fans out to every chat with `notify_push = true`,
  skipping muted bindings.
* `alerts.halt`, `alerts.daily_loss_halt`, and `alerts.emergency_stop`
  schedule a Telegram broadcast in addition to the email dispatch.
* The `/mute` and `/unmute` bot commands flip `notify_push` on the
  binding row.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.telegram import bindings as _bindings, bot as _bot, notify as _notify


# ---------------------------------------------------------------------------
# Fake DB — tracks bindings rows + push fan-out queries.
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self) -> None:
        self.bindings: dict[str, dict[str, Any]] = {}

    def add_binding(
        self,
        clerk_user_id: str,
        *,
        chat_id: int,
        tg_user_id: int = 1,
        notify_push: bool = True,
        bound: bool = True,
    ) -> None:
        from datetime import datetime, timezone
        self.bindings[clerk_user_id] = {
            "clerk_user_id": clerk_user_id,
            "telegram_user_id": tg_user_id,
            "telegram_chat_id": chat_id,
            "telegram_username": "op",
            "link_code": None,
            "link_code_expires_at": None,
            "bound_at": datetime.now(timezone.utc) if bound else None,
            "last_seen_at": None,
            "notify_push": notify_push,
        }

    async def fetch(self, query: str, *args):
        q = " ".join(query.split())
        if "FROM telegram_bindings" in q and "telegram_chat_id" in q:
            return [
                {"telegram_chat_id": row["telegram_chat_id"]}
                for row in self.bindings.values()
                if row.get("telegram_chat_id") is not None
                and row.get("bound_at") is not None
                and row.get("notify_push", True)
            ]
        return []

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM telegram_bindings WHERE clerk_user_id"):
            row = self.bindings.get(args[0])
            return row
        return None

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
def captured_chats(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Replace `bot.send_to_chat` with a recorder so tests can assert
    fan-out without touching httpx."""
    sent: list[tuple[int, str]] = []

    async def _send(chat_id: int, text: str) -> bool:
        sent.append((int(chat_id), text))
        return True

    monkeypatch.setattr(_bot, "send_to_chat", _send)
    return sent


# ---------------------------------------------------------------------------
# notify.broadcast
# ---------------------------------------------------------------------------


def test_broadcast_fans_out_to_opted_in_chats(fake_db, captured_chats) -> None:
    fake_db.add_binding("u1", chat_id=111, notify_push=True)
    fake_db.add_binding("u2", chat_id=222, notify_push=True)
    fake_db.add_binding("u3", chat_id=333, notify_push=False)  # muted

    sent_count = asyncio.run(_notify.broadcast("hello operators"))

    assert sent_count == 2
    chat_ids = sorted(c for c, _ in captured_chats)
    assert chat_ids == [111, 222]
    assert all(text == "hello operators" for _, text in captured_chats)


def test_broadcast_skips_unbound_rows(fake_db, captured_chats) -> None:
    fake_db.add_binding("u1", chat_id=111, bound=False)  # link issued, not bound
    fake_db.add_binding("u2", chat_id=222, notify_push=True)

    asyncio.run(_notify.broadcast("ping"))

    assert [c for c, _ in captured_chats] == [222]


def test_broadcast_noop_on_empty_text(fake_db, captured_chats) -> None:
    fake_db.add_binding("u1", chat_id=111)
    assert asyncio.run(_notify.broadcast("")) == 0
    assert asyncio.run(_notify.broadcast("   ")) == 0
    assert captured_chats == []


# ---------------------------------------------------------------------------
# Alert wiring
# ---------------------------------------------------------------------------


def _stub_email_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the email side of `_dispatch` so the tests don't need
    Postmark configured. Returns the recorder list."""
    sent: list[dict[str, Any]] = []

    def _fake(subject, body, *, tag, dry_run=False, html_body=None, **_kw):
        sent.append({"subject": subject, "tag": tag, "dry_run": dry_run})
        return {"sent": True, "tag": tag}

    monkeypatch.setattr(_alerts, "_dispatch", _fake)
    return sent


def _stub_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the mobile push side (Expo) — out of scope here."""
    from app.notifications import push as _push
    monkeypatch.setattr(_push, "push_halt", lambda **_k: None)


def test_halt_alert_broadcasts_to_telegram(
    monkeypatch: pytest.MonkeyPatch, fake_db, captured_chats
) -> None:
    _stub_email_dispatch(monkeypatch)
    _stub_push(monkeypatch)
    fake_db.add_binding("u1", chat_id=111, notify_push=True)
    fake_db.add_binding("u2", chat_id=222, notify_push=False)

    async def _flow():
        _alerts.halt(reason="circuit breaker tripped")
        # broadcast_in_background schedules a task; let it run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_flow())

    assert [c for c, _ in captured_chats] == [111]
    text = captured_chats[0][1]
    assert "HALT" in text and "circuit breaker tripped" in text


def test_emergency_stop_alert_broadcasts_to_telegram(
    monkeypatch: pytest.MonkeyPatch, fake_db, captured_chats
) -> None:
    _stub_email_dispatch(monkeypatch)
    fake_db.add_binding("u1", chat_id=111, notify_push=True)

    async def _flow():
        _alerts.emergency_stop(actor="alice", canceled_count=4, reason="drill")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_flow())

    assert len(captured_chats) == 1
    text = captured_chats[0][1]
    assert "EMERGENCY STOP" in text
    assert "drill" in text


def test_daily_loss_halt_broadcasts_to_telegram(
    monkeypatch: pytest.MonkeyPatch, fake_db, captured_chats
) -> None:
    _stub_email_dispatch(monkeypatch)
    _stub_push(monkeypatch)
    fake_db.add_binding("u1", chat_id=111, notify_push=True)

    async def _flow():
        _alerts.daily_loss_halt(
            date_utc="2026-04-23",
            loss_usd=250.0,
            realized_pnl_usd=-200.0,
            unrealized_pnl_usd=-50.0,
            threshold_usd=200.0,
            threshold_pct=None,
            threshold_pct_usd=None,
            start_of_day_bankroll_usd=10000.0,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_flow())

    assert len(captured_chats) == 1
    assert "Daily loss cap breached" in captured_chats[0][1]


def test_dry_run_does_not_broadcast(
    monkeypatch: pytest.MonkeyPatch, fake_db, captured_chats
) -> None:
    _stub_email_dispatch(monkeypatch)
    _stub_push(monkeypatch)
    fake_db.add_binding("u1", chat_id=111, notify_push=True)

    async def _flow():
        _alerts.halt(reason="test", dry_run=True)
        _alerts.emergency_stop(actor="alice", canceled_count=0, dry_run=True)
        await asyncio.sleep(0)

    asyncio.run(_flow())
    assert captured_chats == []


# ---------------------------------------------------------------------------
# /mute and /unmute commands
# ---------------------------------------------------------------------------


def test_set_notify_push_toggles_column(fake_db) -> None:
    fake_db.add_binding("user_x", chat_id=999, notify_push=True)
    asyncio.run(_bindings.set_notify_push("user_x", False))
    assert fake_db.bindings["user_x"]["notify_push"] is False
    asyncio.run(_bindings.set_notify_push("user_x", True))
    assert fake_db.bindings["user_x"]["notify_push"] is True


def test_set_notify_push_returns_false_for_missing_binding(fake_db) -> None:
    assert asyncio.run(_bindings.set_notify_push("nope", False)) is False


def test_mute_command_flips_notify_push(fake_db) -> None:
    """End-to-end: /mute and /unmute through the bot dispatcher flip
    `notify_push` on the binding row."""
    import httpx

    class _Recorder:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def post(self, url: str, *, json: dict[str, Any]):
            self.sent.append(json)

            class _R:
                status_code = 200
                text = ""
            return _R()

        async def get(self, *_a, **_kw):
            class _R:
                status_code = 200
                text = ""

                def json(self):
                    return {"ok": True, "result": []}
            return _R()

    fake_db.add_binding("user_x", chat_id=42, tg_user_id=42, notify_push=True)
    rec = _Recorder()

    async def _flow():
        update = {
            "update_id": 1,
            "message": {
                "text": "/mute",
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "username": "op"},
            },
        }
        await _bot.handle_update_for_test(update, client=rec)  # type: ignore[arg-type]
        assert fake_db.bindings["user_x"]["notify_push"] is False
        assert "muted" in rec.sent[-1]["text"].lower()

        update["message"]["text"] = "/unmute"
        update["update_id"] = 2
        await _bot.handle_update_for_test(update, client=rec)  # type: ignore[arg-type]
        assert fake_db.bindings["user_x"]["notify_push"] is True
        assert "re-enabled" in rec.sent[-1]["text"].lower()

    asyncio.run(_flow())
