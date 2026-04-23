"""End-to-end coverage that the paid product endpoints are actually
wired up with ``Depends(billing.require_tier(...))``.

Why this exists
---------------
``test_billing.py`` proves the :func:`billing.require_tier` dependency
returns the right 403 payload in isolation, and ``test_pro_elite_gating``
covers the Elite-only ``/api/orchestrator/live-execute`` toggle.  But
nothing previously asserted that the *real* Pro-tier endpoints
(``/api/swarm/debate``, ``/api/orchestrator/run-cycle``) keep their
``require_tier`` dependency.  A future refactor that silently dropped
the dependency would hand free-tier users our most expensive features
without anyone noticing — billing would stay green but the feature
gate would be wide open.

These tests assert two things per paid route:

  1. A free-tier authed caller gets exactly the documented 403 payload
     ``{"detail": {"error": "tier_required", "required_tier": ..., "current_tier": "free"}}``.
  2. A founder caller passes the gate.  The endpoint body itself may
     still 5xx for unrelated reasons (no Stripe creds, no swarm keys,
     READY gate not flipped, etc.) — only the *gate* is asserted.  Any
     non-403 status counts as the gate having let the caller through.

We deliberately mirror the fixtures from ``test_pro_elite_gating`` so
the suite stays consistent: ``billing.get_user_subscription`` is stubbed
wholesale, the real Clerk JWT path is overridden via
``app.dependency_overrides[require_auth]``.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import billing
from app.auth import AuthedUser, require_auth
from app.main import app


# ---------------------------------------------------------------------------
# Inventory of paid endpoints
# ---------------------------------------------------------------------------
#
# Each entry is (method, path, json body, required_tier).  The body is
# whatever schema the endpoint expects so the request makes it past
# FastAPI's pydantic validation and actually reaches the gate.  If a
# refactor changed the body shape the test would 422 before the gate
# ran — which is fine, the test would fail loudly and the inventory
# below would need updating alongside the route.
PAID_ENDPOINTS: tuple[tuple[str, str, dict[str, Any], str], ...] = (
    (
        "POST",
        "/api/swarm/debate",
        {"game_id": "test-game-1", "matchup": "A vs B"},
        billing.TIER_PRO,
    ),
    (
        "POST",
        "/api/orchestrator/run-cycle",
        {"vertical": "sports"},
        billing.TIER_PRO,
    ),
    (
        "POST",
        "/api/orchestrator/live-execute",
        {"enabled": False},
        billing.TIER_ELITE,
    ),
    (
        "POST",
        "/api/kalshi/paper-order",
        {"market_ticker": "TEST-MKT", "side": "yes", "action": "buy", "count": 1},
        billing.TIER_PRO,
    ),
    (
        "POST",
        "/api/polymarket/paper-order",
        {
            "market_id": "0xabc",
            "token_id": "0xdef",
            "side": "yes",
            "action": "buy",
            "count": 1,
        },
        billing.TIER_PRO,
    ),
)


# ---------------------------------------------------------------------------
# Helpers (mirroring test_pro_elite_gating.py for consistency)
# ---------------------------------------------------------------------------


def _user(uid: str = "user_test", email: str | None = None) -> AuthedUser:
    claims: dict[str, Any] = {"sub": uid}
    if email:
        claims["email"] = email
    return AuthedUser(user_id=uid, session_id="sess_test", claims=claims)


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
    """Stub ``billing.get_user_subscription`` so each test picks the
    caller's tier without round-tripping through the real subscriptions
    table."""
    holder: dict[str, billing.UserSubscription] = {
        "value": _sub(billing.TIER_FREE)
    }

    async def _fake_get(user: AuthedUser) -> billing.UserSubscription:
        return holder["value"]

    monkeypatch.setattr(billing, "get_user_subscription", _fake_get)
    return holder


@pytest.fixture
def override_auth():
    """Bypass the real Clerk JWT validation."""
    user = _user()
    app.dependency_overrides[require_auth] = lambda: user
    yield user
    app.dependency_overrides.pop(require_auth, None)


# ---------------------------------------------------------------------------
# Free callers must be rejected by the tier gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body,required_tier", PAID_ENDPOINTS)
def test_paid_endpoint_rejects_free_caller_with_documented_payload(
    method, path, body, required_tier, patch_subscription, override_auth
):
    """Each paid endpoint must reject a free-tier authed caller with the
    exact 403 payload the frontend's upgrade-CTA logic depends on.

    This is the regression check the task is built around: if a future
    refactor silently drops ``Depends(billing.require_tier(...))`` from
    one of these handlers, the call would land on the body and almost
    certainly return 200/422/500 — but never 403 ``tier_required``.
    """
    patch_subscription["value"] = _sub(billing.TIER_FREE)
    client = TestClient(app)
    resp = client.request(method, path, json=body)
    assert resp.status_code == 403, (
        f"{method} {path} should 403 free callers but returned "
        f"{resp.status_code}: {resp.text}"
    )
    assert resp.json() == {
        "detail": {
            "error": "tier_required",
            "required_tier": required_tier,
            "current_tier": billing.TIER_FREE,
        }
    }


# ---------------------------------------------------------------------------
# Founder callers must pass the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body,required_tier", PAID_ENDPOINTS)
def test_paid_endpoint_lets_founder_through_the_gate(
    method, path, body, required_tier, patch_subscription, override_auth
):
    """Founder is the highest tier and trivially satisfies every
    ``require_tier`` check.  We only assert that the *gate* passed —
    the endpoint body may still 5xx for unrelated reasons (missing
    LLM keys for swarm, READY gate not flipped for run-cycle, no DB
    pool for live-execute write, etc.).  Anything that isn't a 403
    ``tier_required`` proves the dependency let the caller through.
    """
    patch_subscription["value"] = _sub(billing.TIER_FOUNDER)
    client = TestClient(app)
    resp = client.request(method, path, json=body)
    assert resp.status_code != 403, (
        f"{method} {path} unexpectedly 403'd a founder caller — the "
        f"tier gate should have let them through.  Body: {resp.text}"
    )
    # Belt-and-braces: even if some other 4xx layer ever started using
    # the same payload shape, the founder path must never produce the
    # ``tier_required`` error string.
    if resp.headers.get("content-type", "").startswith("application/json"):
        try:
            body_json = resp.json()
        except ValueError:
            body_json = None
        if isinstance(body_json, dict):
            detail = body_json.get("detail")
            if isinstance(detail, dict):
                assert detail.get("error") != "tier_required", (
                    f"{method} {path} returned a tier_required payload "
                    f"to a founder caller: {body_json!r}"
                )
