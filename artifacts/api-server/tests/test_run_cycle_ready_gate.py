"""Integration test: manual cycle triggers refuse to fire pre-READY.

Locks in the contract that ``POST /api/orchestrator/run-cycle`` and
``POST /api/settlement/run-cycle`` both 503 with the readiness payload
until ``app.ready_gate.set_ready()`` has been called, mirroring the
park-on-gate behavior the background scheduler enforces.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import billing, ready_gate
from app.auth import AuthedUser, require_auth
from app.main import app


@pytest.fixture(autouse=True)
def _reset_gate():
    ready_gate.begin()
    yield
    ready_gate._event = None
    ready_gate._checks = {}
    ready_gate._started_at = 0.0
    ready_gate._completed_at = None


@pytest.fixture
def authed_client():
    """TestClient with auth + tier checks bypassed so the test can isolate
    the READY-gate behavior (which is what's under test here)."""
    user = AuthedUser(
        user_id="user_pro_gate",
        session_id="sess_test",
        claims={"email": "pro@example.com"},
    )
    pro_sub = billing.UserSubscription(
        user_id="user_pro_gate",
        email="pro@example.com",
        tier=billing.TIER_PRO,
        status="active",
        stripe_customer_id="cus",
        stripe_subscription_id="sub",
        stripe_price_id="price",
        interval="month",
        current_period_end=datetime(2099, 1, 1, tzinfo=timezone.utc),
        cancel_at_period_end=False,
        is_founder=False,
    )
    # The route's tier dependency was constructed at import time, so a
    # fresh `billing.require_tier(...)` call won't match the route's
    # bound instance. Patch the underlying subscription resolver
    # instead — that's the only thing the dep does that hits state.
    app.dependency_overrides[require_auth] = lambda: user

    async def _fake_get_sub(*_a, **_kw):
        return pro_sub

    original_get = billing.get_user_subscription
    billing.get_user_subscription = _fake_get_sub  # type: ignore[assignment]
    try:
        # Use a raw TestClient (no `with`) so the lifespan does NOT run;
        # we want the gate in its initial begin() state, not the state
        # the lifespan watcher would leave it in.
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_auth, None)
        billing.get_user_subscription = original_get  # type: ignore[assignment]


def test_run_cycle_refuses_until_ready(authed_client: TestClient):
    # Gate has not flipped — request must 503 with readiness payload.
    resp = authed_client.post(
        "/api/orchestrator/run-cycle", json={"vertical": "sports"}
    )
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["status"] == "not_ready"
    assert detail["ready"] is False
    assert "checks" in detail


def test_settlement_run_cycle_refuses_until_ready(authed_client: TestClient):
    resp = authed_client.post("/api/settlement/run-cycle")
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["status"] == "not_ready"
    assert detail["ready"] is False
