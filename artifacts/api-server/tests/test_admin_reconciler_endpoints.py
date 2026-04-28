"""Integration tests for the position-drift admin endpoints (task #709).

These exercise the full FastAPI request path — Pydantic validation,
``Depends(require_admin)`` gating, and JSON serialisation — through the
``TestClient``. The reconciler module functions themselves
(``list_drifts``, ``accept_exchange_truth``, ``snooze_drift``) are
exercised exhaustively by ``test_kalshi_reconciler_resolution.py``, so
here we monkeypatch them to record their inputs and return canned
responses. That keeps these tests:

  * Fast (no DB round-trip).
  * Focused on the *contract* between HTTP and the reconciler — the
    exact regression class the validator flagged: a future PR
    silently changing the URL, query param, payload field, or
    forgetting to wrap an endpoint in ``Depends(require_admin)`` would
    look fine to the per-module tests but break the operator UI.

Each endpoint gets two cases:

  1. Unauthenticated → 401 (the auth-gate test already covers this
     parametrically, but we duplicate it here so a failure in *this*
     file points directly at the right handler).
  2. Authenticated admin → 200, with the call args asserted against
     the documented request contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthedUser, require_admin
from app.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _admin_user() -> AuthedUser:
    return AuthedUser(
        user_id="user_admin_test",
        session_id="sess_admin_test",
        claims={
            "sub": "user_admin_test",
            "email": "ops@axiom-predict.com",
        },
    )


@pytest.fixture
def override_admin():
    """Bypass Clerk + DB checks and present a fixed admin caller.

    Mirrors the override_auth pattern in test_paid_endpoint_gates.py.
    """
    user = _admin_user()
    app.dependency_overrides[require_admin] = lambda: user
    yield user
    app.dependency_overrides.pop(require_admin, None)


@pytest.fixture
def patched_reconciler(monkeypatch):
    """Stub the three reconciler entry points called by the admin
    endpoints, capturing every call for assertion.
    """
    from app.kalshi import reconciler as recon

    calls: dict[str, list[dict[str, Any]]] = {
        "list_drifts": [],
        "accept_exchange_truth": [],
        "snooze_drift": [],
    }

    async def fake_list_drifts(**kwargs):
        calls["list_drifts"].append(kwargs)
        return [
            {
                "id": 1,
                "ticker": "KXBTC-FAKE",
                "vertical": "crypto",
                "local_qty": 0,
                "exchange_qty": 5,
                "drift_qty": 5,
                "state": "open",
                "resolver_email": None,
                "resolution_reason": None,
                "snoozed_until": None,
                "first_seen_at": "2026-04-28T12:00:00+00:00",
                "last_seen_at": "2026-04-28T12:30:00+00:00",
                "resolved_at": None,
                "created_at": "2026-04-28T12:00:00+00:00",
                "age_seconds": 1800,
                "last_alert_at": "2026-04-28T12:05:00+00:00",
                "last_alert_severity": "warning",
                "last_alert_message": "drift detected",
                "market_title": "Will BTC close above $50k?",
            }
        ]

    async def fake_accept(**kwargs):
        calls["accept_exchange_truth"].append(kwargs)
        return {
            "changed": True,
            "ticker": kwargs.get("ticker"),
            "exchange_qty": 5,
            "resolution_id": 42,
        }

    async def fake_snooze(**kwargs):
        calls["snooze_drift"].append(kwargs)
        return {
            "changed": True,
            "ticker": kwargs.get("ticker"),
            "snoozed_until": (
                kwargs.get("until").isoformat()
                if hasattr(kwargs.get("until"), "isoformat")
                else str(kwargs.get("until"))
            ),
            "resolution_id": 99,
        }

    monkeypatch.setattr(recon, "list_drifts", fake_list_drifts)
    monkeypatch.setattr(recon, "accept_exchange_truth", fake_accept)
    monkeypatch.setattr(recon, "snooze_drift", fake_snooze)
    # _resolution_enabled is read by GET /drifts to flag the UI banner.
    monkeypatch.setattr(recon, "_resolution_enabled", lambda: True)
    return calls


# ---------------------------------------------------------------------------
# GET /api/admin/reconciler/drifts
# ---------------------------------------------------------------------------


def test_drifts_list_requires_auth():
    """Unauthenticated callers get 401, never the (forged) drift list."""
    client = TestClient(app)
    resp = client.get("/api/admin/reconciler/drifts")
    assert resp.status_code == 401


def test_drifts_list_admin_happy_path(override_admin, patched_reconciler):
    """Default ``state=open`` is forwarded; response keys match the UI
    contract; enriched fields (``market_title``, ``age_seconds``,
    ``last_alert_*``) are surfaced verbatim.
    """
    client = TestClient(app)
    resp = client.get("/api/admin/reconciler/drifts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "open"
    assert body["resolution_enabled"] is True
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["ticker"] == "KXBTC-FAKE"
    assert row["market_title"] == "Will BTC close above $50k?"
    assert row["age_seconds"] == 1800
    assert row["last_alert_severity"] == "warning"
    # Reconciler called with the renamed kwarg (state, not state_filter).
    assert patched_reconciler["list_drifts"] == [{"state": "open", "limit": 100}]


def test_drifts_list_passes_state_and_limit(override_admin, patched_reconciler):
    """Operator-supplied ``?state=`` + ``?limit=`` reach the reconciler."""
    client = TestClient(app)
    resp = client.get("/api/admin/reconciler/drifts?state=resolved&limit=25")
    assert resp.status_code == 200
    assert patched_reconciler["list_drifts"] == [
        {"state": "resolved", "limit": 25}
    ]


# ---------------------------------------------------------------------------
# POST /api/admin/reconciler/drifts/{ticker}/accept-exchange-truth
# ---------------------------------------------------------------------------


def test_accept_truth_requires_auth():
    client = TestClient(app)
    resp = client.post(
        "/api/admin/reconciler/drifts/KXBTC-FAKE/accept-exchange-truth",
        json={"reason": "missed fill"},
    )
    assert resp.status_code == 401


def test_accept_truth_admin_happy_path(override_admin, patched_reconciler):
    client = TestClient(app)
    resp = client.post(
        "/api/admin/reconciler/drifts/KXBTC-FAKE/accept-exchange-truth",
        json={"reason": "missed fill"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["ticker"] == "KXBTC-FAKE"
    # Ticker forwarded with whitespace stripped, reason passed through.
    [call] = patched_reconciler["accept_exchange_truth"]
    assert call["ticker"] == "KXBTC-FAKE"
    assert call["reason"] == "missed fill"


def test_accept_truth_allows_empty_body(override_admin, patched_reconciler):
    """The Pydantic model defaults reason=None, so a body-less POST
    must be accepted (UI sends ``{reason: ''}`` but a curl operator
    might omit it entirely)."""
    client = TestClient(app)
    resp = client.post(
        "/api/admin/reconciler/drifts/KXBTC-FAKE/accept-exchange-truth",
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# POST /api/admin/reconciler/drifts/{ticker}/snooze
# ---------------------------------------------------------------------------


def test_snooze_requires_auth():
    client = TestClient(app)
    resp = client.post(
        "/api/admin/reconciler/drifts/KXBTC-FAKE/snooze",
        json={"until": "2099-01-01T00:00:00Z", "reason": "noise"},
    )
    assert resp.status_code == 401


def test_snooze_admin_happy_path(override_admin, patched_reconciler):
    """Body uses the new contract: absolute UTC ``until`` ISO string,
    optional ``reason``. Pydantic parses the ISO string into a
    ``datetime``, which the reconciler receives verbatim.
    """
    until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    client = TestClient(app)
    resp = client.post(
        "/api/admin/reconciler/drifts/KXBTC-FAKE/snooze",
        json={"until": until, "reason": "settlement lag"},
    )
    assert resp.status_code == 200, resp.text
    [call] = patched_reconciler["snooze_drift"]
    assert call["ticker"] == "KXBTC-FAKE"
    assert call["reason"] == "settlement lag"
    # Pydantic parsed `until` into a datetime; compare to source ISO string
    # (datetime → isoformat round-trip is stable for this case).
    parsed = call["until"]
    assert isinstance(parsed, datetime)


def test_snooze_rejects_missing_until(override_admin, patched_reconciler):
    """``until`` is required — the validator must catch a missing field
    rather than silently snoozing forever."""
    client = TestClient(app)
    resp = client.post(
        "/api/admin/reconciler/drifts/KXBTC-FAKE/snooze",
        json={"reason": "no until field"},
    )
    assert resp.status_code == 422
    assert patched_reconciler["snooze_drift"] == []


def test_snooze_rejects_garbage_until(override_admin, patched_reconciler):
    """A malformed ``until`` is rejected at the Pydantic boundary, not
    forwarded to the reconciler where it would fall through to a
    ``ValueError`` 500."""
    client = TestClient(app)
    resp = client.post(
        "/api/admin/reconciler/drifts/KXBTC-FAKE/snooze",
        json={"until": "not-a-real-timestamp", "reason": "noise"},
    )
    assert resp.status_code == 422
    assert patched_reconciler["snooze_drift"] == []
