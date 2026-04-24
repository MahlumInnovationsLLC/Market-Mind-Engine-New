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
_QUIET_HOURS_COLS = list(_push.QUIET_HOURS_COLUMNS.keys())
_VERIFICATION_COLS = list(_push._VERIFICATION_COLS)


class _Store:
    def __init__(self) -> None:
        # token -> {"clerk_user_id": ..., "platform": ...}
        self.tokens: dict[str, dict[str, Any]] = {}
        # user_id -> dict of every pref column + sms_phone_number
        self.prefs: dict[str, dict[str, Any]] = {}
        # id -> {"clerk_user_id": ..., ...}. Used by the swipe-to-dismiss
        # tests for `delete_notification` / `delete_all_notifications`.
        self.notification_log: dict[int, dict[str, Any]] = {}

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        if q.startswith("DELETE FROM mobile_notification_log WHERE clerk_user_id = $1 AND id = $2"):
            uid, nid = args
            removed = 0
            row = self.notification_log.get(int(nid))
            if row and row["clerk_user_id"] == uid:
                del self.notification_log[int(nid)]
                removed = 1
            return f"DELETE {removed}"
        if q.startswith("DELETE FROM mobile_notification_log WHERE clerk_user_id = $1"):
            (uid,) = args
            ids = [i for i, r in self.notification_log.items() if r["clerk_user_id"] == uid]
            for i in ids:
                del self.notification_log[i]
            return f"DELETE {len(ids)}"
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
            # All known boolean columns, then sms_phone_number, then
            # the quiet-hours columns, then the SMS-verification
            # columns — matching the column order assembled in
            # `push.set_prefs`.
            row: dict[str, Any] = {}
            for i, col in enumerate(_PREF_BOOL_COLS):
                row[col] = args[1 + i]
            row["sms_phone_number"] = args[1 + len(_PREF_BOOL_COLS)]
            for j, col in enumerate(_QUIET_HOURS_COLS):
                row[col] = args[2 + len(_PREF_BOOL_COLS) + j]
            base = 2 + len(_PREF_BOOL_COLS) + len(_QUIET_HOURS_COLS)
            for k, col in enumerate(_VERIFICATION_COLS):
                row[col] = args[base + k]
            self.prefs[uid] = row
            return "INSERT 0 1"
        if q.startswith("UPDATE user_notification_prefs SET sms_phone_verification_code"):
            uid, code, expires = args
            row = self.prefs.get(uid)
            if row is not None:
                row["sms_phone_verification_code"] = code
                row["sms_phone_verification_expires_at"] = expires
                row["sms_phone_verified_at"] = None
            return "UPDATE 1"
        if q.startswith("UPDATE user_notification_prefs SET sms_phone_verified_at = now()"):
            (uid,) = args
            row = self.prefs.get(uid)
            if row is not None:
                from datetime import datetime, timezone
                row["sms_phone_verified_at"] = datetime.now(timezone.utc)
                row["sms_phone_verification_code"] = None
                row["sms_phone_verification_expires_at"] = None
            return "UPDATE 1"
        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if q.startswith("SELECT") and "FROM user_notification_prefs WHERE clerk_user_id" in q:
            (uid,) = args
            return self.prefs.get(uid)
        if q.startswith("SELECT sms_phone_number FROM user_notification_prefs"):
            (uid,) = args
            row = self.prefs.get(uid)
            return {"sms_phone_number": row["sms_phone_number"]} if row else None
        return None

    async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        if "FROM expo_push_tokens t" in q:
            if "notify_live_trade" in q:
                col = "notify_live_trade"
            elif "notify_settlement" in q:
                col = "notify_settlement"
            else:
                col = "notify_halt"
            # Two shapes share the FROM clause: the per-token recipient
            # query (returns expo_token rows + quiet-hours metadata) and
            # the per-user notification-log query (returns DISTINCT
            # clerk_user_id). Pick the projection from the SELECT list.
            wants_users = "SELECT DISTINCT t.clerk_user_id" in q
            out: list[dict[str, Any]] = []
            seen_users: set[str] = set()
            for tok, row in self.tokens.items():
                pref = self.prefs.get(row["clerk_user_id"])
                enabled = True if pref is None else bool(pref[col])
                if not enabled:
                    continue
                if wants_users:
                    if row["clerk_user_id"] in seen_users:
                        continue
                    seen_users.add(row["clerk_user_id"])
                    out.append({"clerk_user_id": row["clerk_user_id"]})
                else:
                    out.append({
                        "expo_token": tok,
                        "quiet_hours_start_hour": (pref or {}).get("quiet_hours_start_hour"),
                        "quiet_hours_end_hour": (pref or {}).get("quiet_hours_end_hour"),
                        "quiet_hours_tz": (pref or {}).get("quiet_hours_tz"),
                    })
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
    # New numbers are unverified by default — task #191 gate.
    assert prefs["sms_phone_verified"] is False
    assert prefs["sms_phone_verified_at"] is None


def test_phone_verification_round_trip(store: _Store) -> None:
    """Save phone → issue code → confirm code → verified flag flips on."""
    _run(_push.set_prefs(
        clerk_user_id="user_v", sms_phone_number="+15550001111",
    ))
    issued = _run(_push.issue_phone_verification_code("user_v"))
    assert issued is not None
    code, phone, expires = issued
    assert phone == "+15550001111"
    assert len(code) == 6 and code.isdigit()

    # Wrong code is rejected and does not flip the verified flag.
    assert _run(_push.confirm_phone_verification_code("user_v", "000000")) is False
    assert _run(_push.get_prefs("user_v"))["sms_phone_verified"] is False

    assert _run(_push.confirm_phone_verification_code("user_v", code)) is True
    after = _run(_push.get_prefs("user_v"))
    assert after["sms_phone_verified"] is True
    assert after["sms_phone_verification_code"] is None


def test_changing_phone_clears_prior_verification(store: _Store) -> None:
    """A verified user who edits their number is forced to re-verify."""
    _run(_push.set_prefs(clerk_user_id="user_e", sms_phone_number="+15551112222"))
    issued = _run(_push.issue_phone_verification_code("user_e"))
    assert issued is not None
    assert _run(_push.confirm_phone_verification_code("user_e", issued[0])) is True
    assert _run(_push.get_prefs("user_e"))["sms_phone_verified"] is True

    _run(_push.set_prefs(clerk_user_id="user_e", sms_phone_number="+15553334444"))
    after = _run(_push.get_prefs("user_e"))
    assert after["sms_phone_number"] == "+15553334444"
    assert after["sms_phone_verified"] is False, (
        "changing the phone must drop the prior verification"
    )


def test_issue_phone_verification_without_phone_returns_none(
    store: _Store,
) -> None:
    assert _run(_push.issue_phone_verification_code("nobody")) is None


def test_notification_prefs_endpoint_blocks_sms_toggle_without_verification(
    store: _Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling `sms_live_trade` for an Elite user with an unverified
    phone must 400 with `sms_phone_not_verified` and the dispatcher
    must skip them too. Verifying the number unlocks both."""
    from fastapi.testclient import TestClient
    from app import main as _main
    from app.auth import require_auth, AuthedUser
    from app import billing as _billing
    from app.notifications import sms as _sms

    user = AuthedUser(
        user_id="user_sms", session_id=None,
        claims={"sub": "user_sms", "email": "s@example.com"},
    )

    async def _fake_sub(_u):
        return _billing.UserSubscription(
            user_id="user_sms", email="s@example.com",
            tier=_billing.TIER_ELITE, status="active",
            stripe_customer_id=None, stripe_subscription_id=None,
            stripe_price_id=None, interval=None,
            current_period_end=None, cancel_at_period_end=False,
            is_founder=False,
        )

    sent: list[tuple[tuple, dict]] = []

    async def _fake_sms_send(to, body, *, tag=None):  # noqa: ANN001
        sent.append((tuple(to), {"body": body, "tag": tag}))
        return [{"to": list(to)[0], "ok": True}]

    monkeypatch.setattr(_billing, "get_user_subscription", _fake_sub)
    monkeypatch.setattr(_sms, "send", _fake_sms_send)
    _main.app.dependency_overrides[require_auth] = lambda: user
    try:
        client = TestClient(_main.app)
        # Save phone — server should auto-fire a verification SMS.
        resp = client.put(
            "/api/me/notification-prefs",
            json={
                "sms_phone_number": "+15557776666",
                "sms_phone_number_present": True,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["prefs"]["sms_phone_number"] == "+15557776666"
        assert body["prefs"]["sms_phone_verified"] is False
        # SECURITY: never echo the in-flight verification code back to
        # the client (would let any session self-verify a stranger's
        # phone without ever receiving the SMS).
        assert "sms_phone_verification_code" not in body["prefs"]
        assert "sms_phone_verified_at" not in body["prefs"]
        assert sent, "saving a new phone must trigger a verification SMS"

        # Enabling the toggle is blocked while unverified.
        resp = client.put(
            "/api/me/notification-prefs",
            json={"sms_live_trade": True},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "sms_phone_not_verified"

        # Confirm the code from the in-flight challenge.
        code = store.prefs["user_sms"]["sms_phone_verification_code"]
        resp = client.post(
            "/api/me/notification-prefs/sms/verify",
            json={"code": code},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["prefs"]["sms_phone_verified"] is True

        # Now the toggle saves cleanly.
        resp = client.put(
            "/api/me/notification-prefs",
            json={"sms_live_trade": True},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["prefs"]["sms_live_trade"] is True
    finally:
        _main.app.dependency_overrides.pop(require_auth, None)


def test_notification_prefs_endpoint_skips_auto_sms_for_non_elite(
    store: _Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A free/pro user saving a phone number must NOT trigger a paid
    Twilio SMS — they can't enable SMS toggles anyway, so auto-sending
    a verification code is pure spend leakage. The same save by an
    Elite user does fire the SMS (covered by the test above)."""
    from fastapi.testclient import TestClient
    from app import main as _main
    from app.auth import require_auth, AuthedUser
    from app import billing as _billing
    from app.notifications import sms as _sms

    user = AuthedUser(
        user_id="user_free", session_id=None,
        claims={"sub": "user_free", "email": "f@example.com"},
    )

    async def _fake_sub(_u):
        return _billing.UserSubscription(
            user_id="user_free", email="f@example.com",
            tier=_billing.TIER_FREE, status="active",
            stripe_customer_id=None, stripe_subscription_id=None,
            stripe_price_id=None, interval=None,
            current_period_end=None, cancel_at_period_end=False,
            is_founder=False,
        )

    sent: list[tuple] = []

    async def _fake_sms_send(to, body, *, tag=None):  # noqa: ANN001
        sent.append((tuple(to), body, tag))
        return [{"to": list(to)[0], "ok": True}]

    monkeypatch.setattr(_billing, "get_user_subscription", _fake_sub)
    monkeypatch.setattr(_sms, "send", _fake_sms_send)
    _main.app.dependency_overrides[require_auth] = lambda: user
    try:
        client = TestClient(_main.app)
        resp = client.put(
            "/api/me/notification-prefs",
            json={
                "sms_phone_number": "+15558884444",
                "sms_phone_number_present": True,
            },
        )
        assert resp.status_code == 200, resp.text
        assert sent == [], (
            "non-elite phone save must not dispatch a verification SMS"
        )
        # And the explicit start-verification endpoint must reject the
        # free user with a tier_required error rather than send the code.
        resp = client.post(
            "/api/me/notification-prefs/sms/start-verification"
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "tier_required"
        assert sent == []
    finally:
        _main.app.dependency_overrides.pop(require_auth, None)


def test_tokens_for_pref_respects_toggles(store: _Store) -> None:
    _run(_push.register_token(clerk_user_id="user_1", expo_token=VALID_TOKEN, platform="ios"))
    _run(_push.register_token(clerk_user_id="user_2", expo_token=VALID_TOKEN_2, platform="android"))
    _run(_push.set_prefs(clerk_user_id="user_2", notify_live_trade=False))

    lt = _run(_push._tokens_for_pref("notify_live_trade"))
    assert lt == [VALID_TOKEN]

    halt = _run(_push._tokens_for_pref("notify_halt"))
    assert sorted(halt) == sorted([VALID_TOKEN, VALID_TOKEN_2])


def test_is_in_quiet_hours_handles_same_day_and_overnight() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = "America/New_York"
    nyc = ZoneInfo(tz)

    # Same-day window 13:00-15:00 — 14:00 is inside, 15:00 is the
    # exclusive upper bound and 12:59 is before.
    inside = datetime(2026, 1, 5, 14, 0, tzinfo=nyc)
    boundary = datetime(2026, 1, 5, 15, 0, tzinfo=nyc)
    before = datetime(2026, 1, 5, 12, 59, tzinfo=nyc)
    assert _push.is_in_quiet_hours(13, 15, tz, now=inside) is True
    assert _push.is_in_quiet_hours(13, 15, tz, now=boundary) is False
    assert _push.is_in_quiet_hours(13, 15, tz, now=before) is False

    # Overnight 22:00 -> 07:00 — 23:00 and 03:00 inside, 07:00 outside.
    late = datetime(2026, 1, 5, 23, 0, tzinfo=nyc)
    early = datetime(2026, 1, 6, 3, 0, tzinfo=nyc)
    morning = datetime(2026, 1, 6, 7, 0, tzinfo=nyc)
    assert _push.is_in_quiet_hours(22, 7, tz, now=late) is True
    assert _push.is_in_quiet_hours(22, 7, tz, now=early) is True
    assert _push.is_in_quiet_hours(22, 7, tz, now=morning) is False

    # Equal start/end is treated as disabled, not 24h mute.
    assert _push.is_in_quiet_hours(8, 8, tz, now=inside) is False
    # Any missing field disables the window.
    assert _push.is_in_quiet_hours(None, 7, tz, now=inside) is False
    assert _push.is_in_quiet_hours(22, None, tz, now=inside) is False
    assert _push.is_in_quiet_hours(22, 7, "", now=inside) is False
    # Bad timezone strings just disable the window rather than crash.
    assert _push.is_in_quiet_hours(22, 7, "Not/A_Zone", now=inside) is False


def test_set_prefs_round_trips_quiet_hours(store: _Store) -> None:
    out = _run(_push.set_prefs(
        clerk_user_id="user_1",
        quiet_hours_start_hour=22,
        quiet_hours_end_hour=7,
        quiet_hours_tz="America/Los_Angeles",
    ))
    assert out["quiet_hours_start_hour"] == 22
    assert out["quiet_hours_end_hour"] == 7
    assert out["quiet_hours_tz"] == "America/Los_Angeles"
    again = _run(_push.get_prefs("user_1"))
    assert again["quiet_hours_start_hour"] == 22
    assert again["quiet_hours_end_hour"] == 7
    assert again["quiet_hours_tz"] == "America/Los_Angeles"

    # A partial update of a single boolean must preserve quiet hours.
    _run(_push.set_prefs(clerk_user_id="user_1", notify_halt=False))
    again = _run(_push.get_prefs("user_1"))
    assert again["quiet_hours_start_hour"] == 22
    assert again["quiet_hours_tz"] == "America/Los_Angeles"

    # Explicitly clearing the timezone disables the window.
    _run(_push.set_prefs(clerk_user_id="user_1", quiet_hours_tz=None))
    again = _run(_push.get_prefs("user_1"))
    assert again["quiet_hours_tz"] is None
    assert again["quiet_hours_start_hour"] == 22


def test_set_prefs_rejects_bad_quiet_hours(store: _Store) -> None:
    with pytest.raises(ValueError):
        _run(_push.set_prefs(clerk_user_id="user_1", quiet_hours_start_hour=99))
    with pytest.raises(ValueError):
        _run(_push.set_prefs(clerk_user_id="user_1", quiet_hours_tz="Not/A_Zone"))


def test_tokens_for_pref_skips_users_in_quiet_hours(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    _run(_push.register_token(clerk_user_id="user_1", expo_token=VALID_TOKEN, platform="ios"))
    _run(_push.register_token(clerk_user_id="user_2", expo_token=VALID_TOKEN_2, platform="android"))
    _run(_push.set_prefs(
        clerk_user_id="user_1",
        quiet_hours_start_hour=22,
        quiet_hours_end_hour=7,
        quiet_hours_tz="America/New_York",
    ))

    fixed = datetime(2026, 1, 5, 23, 0, tzinfo=ZoneInfo("America/New_York"))

    real_is_in = _push.is_in_quiet_hours

    def _faked(start: Any, end: Any, tz: Any, *, now: Any = None) -> bool:
        return real_is_in(start, end, tz, now=fixed)

    monkeypatch.setattr(_push, "is_in_quiet_hours", _faked)

    lt = _run(_push._tokens_for_pref("notify_live_trade"))
    # user_1 is muted at 23:00 NY, user_2 still receives.
    assert lt == [VALID_TOKEN_2]


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
    _push.push_settlement(
        market_ticker="X", vertical="sports", pnl=1.23, outcome_yes=1,
    )
    _push.push_settlement(
        market_ticker="X", vertical="sports", pnl=-0.5, outcome_yes=0,
    )


def test_notification_prefs_endpoint_round_trips_notify_settlement(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT /api/me/notification-prefs must accept and persist
    `notify_settlement` so the mobile UI's third toggle actually
    controls delivery (not just gets silently dropped by Pydantic)."""
    from fastapi.testclient import TestClient
    from app import main as _main
    from app.auth import require_auth, AuthedUser
    from app import billing as _billing

    user = AuthedUser(
        user_id="user_settle", session_id=None,
        claims={"sub": "user_settle", "email": "s@example.com"},
    )

    async def _fake_sub(_u):
        return _billing.UserSubscription(
            user_id="user_settle", email="s@example.com",
            tier=_billing.TIER_PRO, status="active",
            stripe_customer_id=None, stripe_subscription_id=None,
            stripe_price_id=None, interval=None,
            current_period_end=None, cancel_at_period_end=False,
            is_founder=False,
        )

    monkeypatch.setattr(_billing, "get_user_subscription", _fake_sub)
    _main.app.dependency_overrides[require_auth] = lambda: user
    try:
        client = TestClient(_main.app)
        resp = client.put(
            "/api/me/notification-prefs",
            json={"notify_settlement": False},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["prefs"]["notify_settlement"] is False
        # And the bool round-trips back to true.
        resp = client.put(
            "/api/me/notification-prefs",
            json={"notify_settlement": True},
        )
        assert resp.status_code == 200
        assert resp.json()["prefs"]["notify_settlement"] is True
    finally:
        _main.app.dependency_overrides.pop(require_auth, None)


def test_settle_market_emits_one_push_with_aggregated_pnl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single position can be built from multiple trade fills; when
    the underlying market settles we must fire exactly one push per
    resolved position (not one per fill) and the body must carry the
    summed realized P&L."""
    from app.settlement import pipeline as _pipeline

    # Stub every DB call settle_market touches. Two unsettled trades on
    # the same market id, each with a known PnL → expect a single push
    # carrying the sum.
    trade_rows = [
        {
            "id": 1, "trade_id": "t1", "market_id": "MKT-1",
            "vertical": "sports", "league": "NBA",
            "side": "yes", "action": "buy",
            "quantity": 10.0, "price": 0.40,
            "model_prob": 0.6, "model_edge": 0.05, "edge": 0.05,
            "exchange": "kalshi", "trading_mode": "live",
            "metadata": {},
        },
        {
            "id": 2, "trade_id": "t2", "market_id": "MKT-1",
            "vertical": "sports", "league": "NBA",
            "side": "yes", "action": "buy",
            "quantity": 5.0, "price": 0.45,
            "model_prob": 0.6, "model_edge": 0.05, "edge": 0.05,
            "exchange": "kalshi", "trading_mode": "live",
            "metadata": {},
        },
    ]

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM trades" in query and "settled_at IS NULL" in query:
            return trade_rows
        return []

    async def _fake_fetchrow(*_a: Any, **_kw: Any) -> dict[str, Any] | None:
        return None

    async def _fake_fetchval(*_a: Any, **_kw: Any) -> Any:
        return None

    async def _fake_execute(*_a: Any, **_kw: Any) -> str:
        return "OK"

    monkeypatch.setattr(_pipeline.db, "fetch", _fake_fetch)
    monkeypatch.setattr(_pipeline.db, "fetchrow", _fake_fetchrow)
    monkeypatch.setattr(_pipeline.db, "fetchval", _fake_fetchval)
    monkeypatch.setattr(_pipeline.db, "execute", _fake_execute)

    # Capture push_settlement calls.
    captured: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> None:
        captured.append(kwargs)

    from app.notifications import push as _push
    monkeypatch.setattr(_push, "push_settlement", _spy)

    asyncio.run(_pipeline.settle_market("MKT-1", outcome_yes=1, settle_price=1.0))

    assert len(captured) == 1, f"expected exactly one push, got {len(captured)}"
    call = captured[0]
    assert call["market_ticker"] == "MKT-1"
    assert call["vertical"] == "sports"
    assert call["outcome_yes"] == 1
    # YES wins; trade 1 cost 10*0.40 = 4, payout 10 → +6.0;
    # trade 2 cost 5*0.45 = 2.25, payout 5 → +2.75. Sum = 8.75.
    assert abs(call["pnl"] - 8.75) < 1e-6


def test_push_settlement_dispatches_to_opted_in_tokens(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settlement push fans out to every device whose owner has the
    `notify_settlement` toggle on (or no row, defaulting to true), and
    skips users who explicitly opted out."""
    _run(_push.register_token(
        clerk_user_id="user_in", expo_token=VALID_TOKEN, platform="ios",
    ))
    _run(_push.register_token(
        clerk_user_id="user_out", expo_token=VALID_TOKEN_2, platform="ios",
    ))
    _run(_push.set_prefs(clerk_user_id="user_out", notify_settlement=False))

    tokens = _run(_push._tokens_for_pref("notify_settlement"))
    assert tokens == [VALID_TOKEN]

    captured: dict[str, Any] = {}

    class _FakeResponse:
        status_code = 200
        text = "ok"

        def json(self) -> dict[str, Any]:
            return {"data": [{"status": "ok", "id": "x"}]}

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

    async def _go() -> None:
        await _push._dispatch_for_alert(
            "notify_settlement",
            title="Position settled — won",
            body="X: +$1.23 realized P&L",
            data={"type": "settlement", "deep_link": "/positions"},
            channel_id="settlement",
        )

    asyncio.run(_go())

    msgs = captured["json"]
    assert isinstance(msgs, list) and len(msgs) == 1
    assert msgs[0]["to"] == VALID_TOKEN
    assert msgs[0]["channelId"] == "settlement"
    assert msgs[0]["data"]["deep_link"] == "/positions"
    assert msgs[0]["data"]["type"] == "settlement"


# ---------------------------------------------------------------------------
# Swipe-to-dismiss helpers (task #347)
# ---------------------------------------------------------------------------


def test_delete_notification_removes_only_owners_row(store: _Store) -> None:
    store.notification_log[1] = {"clerk_user_id": "user_a"}
    store.notification_log[2] = {"clerk_user_id": "user_b"}

    # Owner can delete their row.
    assert _run(_push.delete_notification("user_a", 1)) is True
    assert 1 not in store.notification_log

    # Non-owner cannot delete someone else's row, even when guessing the id.
    assert _run(_push.delete_notification("user_a", 2)) is False
    assert 2 in store.notification_log

    # Unknown ids return False, never raise.
    assert _run(_push.delete_notification("user_a", 9999)) is False


def test_delete_notification_rejects_non_int_id(store: _Store) -> None:
    store.notification_log[5] = {"clerk_user_id": "user_a"}
    # Garbage ids never reach the DB layer.
    assert _run(_push.delete_notification("user_a", "not-a-number")) is False  # type: ignore[arg-type]
    assert 5 in store.notification_log


def test_delete_all_notifications_clears_only_caller(store: _Store) -> None:
    store.notification_log[1] = {"clerk_user_id": "user_a"}
    store.notification_log[2] = {"clerk_user_id": "user_a"}
    store.notification_log[3] = {"clerk_user_id": "user_b"}

    removed = _run(_push.delete_all_notifications("user_a"))
    assert removed == 2
    # Other users' rows are untouched.
    assert 3 in store.notification_log
    assert all(r["clerk_user_id"] != "user_a" for r in store.notification_log.values())

    # Idempotent: calling again on an empty feed returns 0 cleanly.
    assert _run(_push.delete_all_notifications("user_a")) == 0
