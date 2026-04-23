"""Tier-gating tests for the live persona stream WebSocket.

Why this exists
---------------
``/api/ws/llm-logs`` mirrors the redaction policy of
``/api/dashboard/recent-debates``: sub-Pro callers must not see live
persona reasoning.  The REST gate is covered in
``test_pro_elite_gating.py``; the WS path was previously uncovered, so a
future refactor that drops the ``billing.get_user_subscription`` check
in ``ws_llm_logs`` would silently re-expose the stream to free users.

These tests pin the close code to 4403 ("pro_tier_required") for
sub-Pro callers and confirm the socket stays open for Pro+.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth as auth_module
from app import billing
from app.auth import AuthedUser
from app.main import app


def _user(uid: str = "user_test") -> AuthedUser:
    return AuthedUser(
        user_id=uid, session_id="sess_test", claims={"sub": uid}
    )


def _sub(tier: str, *, uid: str = "user_test") -> billing.UserSubscription:
    return billing.UserSubscription(
        user_id=uid,
        email=f"{uid}@example.com",
        tier=tier,
        status="active" if tier != billing.TIER_FREE else "inactive",
        stripe_customer_id=None,
        stripe_subscription_id=None,
        stripe_price_id=None,
        interval=None,
        current_period_end=None,
        cancel_at_period_end=False,
        is_founder=(tier == billing.TIER_FOUNDER),
    )


@pytest.fixture
def patch_subscription(monkeypatch):
    """Same pattern used by tests/test_pro_elite_gating.py."""
    holder: dict[str, billing.UserSubscription] = {
        "value": _sub(billing.TIER_FREE)
    }

    async def _fake_get(user: AuthedUser) -> billing.UserSubscription:
        return holder["value"]

    monkeypatch.setattr(billing, "get_user_subscription", _fake_get)
    return holder


@pytest.fixture
def patch_ws_auth(monkeypatch):
    """Bypass Clerk JWT validation for the WS handshake.

    ``ws_llm_logs`` does ``from .auth import ws_authenticate`` lazily, so
    patching the source module attribute is what the handler will pick
    up.  The fake still calls ``websocket.accept()`` to mirror the real
    helper's contract (the upgrade must complete before the handler can
    close with a custom code).
    """
    user = _user()

    async def _fake_ws_authenticate(websocket: Any) -> AuthedUser:
        await websocket.accept()
        return user

    monkeypatch.setattr(auth_module, "ws_authenticate", _fake_ws_authenticate)
    return user


def test_ws_llm_logs_closes_4403_for_free_tier(
    patch_subscription, patch_ws_auth
):
    patch_subscription["value"] = _sub(billing.TIER_FREE)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/ws/llm-logs?token=ignored"
        ) as ws:
            # Server closes immediately after the tier check fails;
            # any read surfaces the close as WebSocketDisconnect.
            ws.receive_text()
    assert exc_info.value.code == 4403


@pytest.mark.parametrize(
    "tier", [billing.TIER_PRO, billing.TIER_ELITE, billing.TIER_FOUNDER]
)
def test_ws_llm_logs_stays_open_for_pro_plus(
    tier, patch_subscription, patch_ws_auth
):
    """For Pro+ the gate must not fire — verify liveness by actually
    receiving a frame from ``llm_log_hub``.  Connecting + closing
    client-side is not enough, since a server-initiated 4403 close
    would not raise on the client when no read is attempted.
    """
    from app.swarm import llm_log_hub

    patch_subscription["value"] = _sub(tier)
    client = TestClient(app)
    payload = {"type": "test", "tier": tier, "marker": "live-stream-ok"}

    with client.websocket_connect("/api/ws/llm-logs?token=ignored") as ws:
        # Publish into the hub from the TestClient's portal so the
        # async publish runs on the server's loop.  The handler then
        # forwards the event to our open socket.
        ws.portal.call(llm_log_hub.publish, payload)
        try:
            received = ws.receive_json()
        except WebSocketDisconnect as e:  # pragma: no cover — regression guard
            pytest.fail(
                f"socket was closed for tier={tier} (code={e.code}); "
                "Pro+ must not be gated"
            )
        assert received == payload
