"""Unit tests for the Expo push notification module.

Covers:
  * register_token rejects malformed tokens.
  * get_prefs defaults to every toggle's documented default value.
  * set_prefs round-trips via the in-memory shim.
  * _tokens_for_pref respects pref toggles + defaults.
  * send_to_tokens calls Expo Push API and removes DeviceNotRegistered tokens.
  * push_live_trade / push_halt do not raise when called from sync code.

We never open the asyncpg pool — `app.db` is monkey-patched with a tiny
in-memory store that mimics our two tables.  HTTP to Expo is intercepted
with a respx-style fake so no network IO happens.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app import db as _db
from app.notifications import push as _push


# ---------------------------------------------------------------------------
# In-memory shim for the two push tables
# ---------------------------------------------------------------------------


_PREF_BOOL_COLS = list(_push.PREF_COLUMNS.keys())


class _Store:
    def __init__(self) -> None:
        # token -> {"clerk_user_id": ..., "platform": ...}
        self.tokens: dict[str, dict[str, Any]] = {}
        # user_id -> dict of every pref column + sms_phone_number
        self.prefs: dict[str, dict[str, Any]] = {}

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        if q.startswith("INSERT INTO expo_push_tokens"):
            tok, uid, plat = args
            self.tokens[tok] = {"clerk_user_id": uid, "platform": plat}
            return "INSERT 0 1"
        if q.startswith("DELETE FROM expo_push_tokens WHERE expo_token = $1 AND clerk_user_id = $2"):
            tok, uid = args
            row = self.tokens.get(tok)
            if row and row["clerk_user_id"] == uid:
                del self.tokens[tok]
                return "DELETE 1"
            return "DELETE 0"
        if q.startswith("DELETE FROM expo_push_tokens WHERE expo_token = $1"):
            (tok,) = args
            self.tokens.pop(tok, None)
            return "DELETE 1"
        if q.startswith("INSERT INTO user_notification_prefs"):
            uid = args[0]
            # All known boolean columns then sms_phone_number, matching
            # the column order assembled in `push.set_prefs`.
            row: dict[str, Any] = {}
            for i, col in enumerate(_PREF_BOOL_COLS):
                row[col] = args[1 + i]
            row["sms_phone_number"] = args[1 + len(_PREF_BOOL_COLS)]
            self.prefs[uid] = row
            return "INSERT 0 1"
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if q.startswith("SELECT") and "FROM user_notification_prefs WHERE clerk_user_id" in q:
            (uid,) = args
            return self.prefs.get(uid)
        return None

    async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        if "FROM expo_push_tokens t" in q:
            col = "notify_live_trade" if "notify_live_trade" in q else "notify_halt"
            out: list[dict[str, str]] = []
            for tok, row in self.tokens.items():
                pref = self.prefs.get(row["clerk_user_id"])
                enabled = True if pref is None else bool(pref[col])
                if enabled:
                    out.append({"expo_token": tok})
            return out
        return []


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    s = _Store()
    monkeypatch.setattr(_db, "execute", s.execute)
    monkeypatch.setattr(_db, "fetchrow", s.fetchrow)
    monkeypatch.setattr(_db, "fetch", s.fetch)

    async def _ready() -> None:
        return None

    monkeypatch.setattr(_push, "ensure_schema", _ready)
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


VALID_TOKEN = "ExponentPushToken[abcdef0123456789]"
VALID_TOKEN_2 = "ExponentPushToken[xxxxxxxxxxxxxxxx]"


def _run(coro):
    return asyncio.run(coro)


def test_register_token_rejects_malformed(store: _Store) -> None:
    ok = _run(_push.register_token(
        clerk_user_id="user_1", expo_token="not-an-expo-token", platform="ios"
    ))
    assert ok is False
    assert store.tokens == {}


def test_register_token_persists_and_rebinds(store: _Store) -> None:
    assert _run(_push.register_token(
        clerk_user_id="user_1", expo_token=VALID_TOKEN, platform="ios"
    )) is True
    assert store.tokens[VALID_TOKEN]["clerk_user_id"] == "user_1"
    assert _run(_push.register_token(
        clerk_user_id="user_2", expo_token=VALID_TOKEN, platform="android"
    )) is True
    assert store.tokens[VALID_TOKEN]["clerk_user_id"] == "user_2"


def test_prefs_default_to_documented_values(store: _Store) -> None:
    prefs = _run(_push.get_prefs("user_1"))
    for col, default in _push.PREF_COLUMNS.items():
        assert prefs[col] == default, f"{col} default mismatch"
    assert prefs["sms_phone_number"] is None


def test_prefs_partial_update_preserves_other(store: _Store) -> None:
    _run(_push.set_prefs(clerk_user_id="user_1", notify_halt=False))
    prefs = _run(_push.get_prefs("user_1"))
    assert prefs["notify_halt"] is False
    assert prefs["notify_live_trade"] is True
    assert prefs["email_daily_summary"] is True

    _run(_push.set_prefs(
        clerk_user_id="user_1",
        notify_live_trade=False,
        email_daily_summary=False,
    ))
    prefs = _run(_push.get_prefs("user_1"))
    assert prefs["notify_live_trade"] is False
    assert prefs["notify_halt"] is False
    assert prefs["email_daily_summary"] is False


def test_prefs_set_phone_number(store: _Store) -> None:
    _run(_push.set_prefs(
        clerk_user_id="user_1",
        sms_phone_number="+15551234567",
        sms_live_trade=True,
    ))
    prefs = _run(_push.get_prefs("user_1"))
    assert prefs["sms_phone_number"] == "+15551234567"
    assert prefs["sms_live_trade"] is True


def test_tokens_for_pref_respects_toggles(store: _Store) -> None:
    _run(_push.register_token(clerk_user_id="user_1", expo_token=VALID_TOKEN, platform="ios"))
    _run(_push.register_token(clerk_user_id="user_2", expo_token=VALID_TOKEN_2, platform="android"))
    _run(_push.set_prefs(clerk_user_id="user_2", notify_live_trade=False))

    lt = _run(_push._tokens_for_pref("notify_live_trade"))
    assert lt == [VALID_TOKEN]

    halt = _run(_push._tokens_for_pref("notify_halt"))
    assert sorted(halt) == sorted([VALID_TOKEN, VALID_TOKEN_2])


def test_send_to_tokens_drops_unregistered_devices(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(_push.register_token(clerk_user_id="user_1", expo_token=VALID_TOKEN, platform="ios"))
    _run(_push.register_token(clerk_user_id="user_2", expo_token=VALID_TOKEN_2, platform="ios"))

    captured: dict[str, Any] = {}

    class _FakeResponse:
        status_code = 200
        text = "ok"

        def json(self) -> dict[str, Any]:
            return {
                "data": [
                    {"status": "ok", "id": "x"},
                    {
                        "status": "error",
                        "message": "device not registered",
                        "details": {"error": "DeviceNotRegistered"},
                    },
                ]
            }

    class _FakeClient:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> _FakeResponse:
            captured["url"] = url
            captured["json"] = kw.get("json")
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setenv("AXIOM_PUSH_DISABLED", "0")

    async def _go() -> dict[str, Any]:
        result = await _push.send_to_tokens(
            [VALID_TOKEN, VALID_TOKEN_2],
            title="t", body="b", data={"type": "live_trade"}, channel_id="trades",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result

    result = asyncio.run(_go())

    assert result["sent"] == 2
    assert result["ok"] == 1
    assert len(result["errors"]) == 1
    assert VALID_TOKEN in store.tokens
    assert VALID_TOKEN_2 not in store.tokens
    assert captured["url"] == _push.EXPO_PUSH_URL
    assert isinstance(captured["json"], list) and len(captured["json"]) == 2
    assert captured["json"][0]["channelId"] == "trades"


def test_push_live_trade_and_halt_are_sync_safe(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync callers (kill switch trip, alert helpers) must not raise."""
    monkeypatch.setenv("AXIOM_PUSH_DISABLED", "1")
    _push.push_live_trade(
        market_ticker="X", side="yes", action="buy",
        contracts=1, price_cents=50, vertical="sports",
    )
    _push.push_halt(reason="test")
