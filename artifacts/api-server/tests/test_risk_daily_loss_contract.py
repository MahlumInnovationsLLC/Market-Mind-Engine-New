"""Contract test for GET /api/risk/daily-loss (task #284).

The System -> Controls panel in the web UI consumes this payload via
``apiFetch<DailyLossSnapshot>("/api/risk/daily-loss", ...)``.  A silent
server-side rename or removal of one of these fields would render the
panel broken at runtime with no warning.  This test pins the response
shape so that any drift breaks the standard test suite first.

The endpoint is exercised via FastAPI's TestClient with ``require_auth``
overridden, and the underlying daily-loss snapshot module is stubbed so
the test does not need a database pool.
"""
from __future__ import annotations

from typing import Any

import pytest


def _signed_in_user():
    from app.auth import AuthedUser

    return AuthedUser(
        user_id="user_test",
        session_id="sess_test",
        claims={"sub": "user_test"},
    )


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.auth import require_auth
    from app.main import app
    from app.orchestrator import cycle as _cycle
    from app.risk import daily_loss as _dl

    async def _no_overrides() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(_cycle, "_load_operator_overrides", _no_overrides)

    async def _fake_snapshot(overrides=None, *, use_cache: bool = True):
        return _dl.DailyLossSnapshot(
            date_local="2026-04-24",
            timezone="UTC",
            trading_mode="live",
            realized_pnl_usd=-120.5,
            unrealized_pnl_usd=-25.0,
            net_pnl_usd=-145.5,
            start_of_day_bankroll_usd=2000.0,
            threshold_usd=500.0,
            threshold_pct=10.0,
            threshold_pct_usd=200.0,
            loss_usd=145.5,
            tripped_abs=False,
            tripped_pct=False,
            tripped=False,
            tripped_reason=None,
        )

    monkeypatch.setattr(_dl, "compute_snapshot", _fake_snapshot)

    app.dependency_overrides[require_auth] = _signed_in_user
    # Raw TestClient (no `with`) so the app lifespan does not open the
    # real DB pool during the unit test.
    c = TestClient(app)
    try:
        yield c
    finally:
        app.dependency_overrides.pop(require_auth, None)


def test_daily_loss_endpoint_response_contract(client):
    """The payload must contain every field the UI consumes, with the
    expected types.  Any drift here (rename, removal, type change) means
    the System -> Controls Daily-Loss card would break — fix the API or
    update the UI + this test together."""
    r = client.get("/api/risk/daily-loss")
    assert r.status_code == 200, r.text
    body = r.json()

    str_fields = ("date_local", "timezone")
    for f in str_fields:
        assert f in body, f"missing field: {f}"
        assert isinstance(body[f], str), f"{f} must be str, got {type(body[f]).__name__}"

    # Numeric fields the UI always renders (loss + P&L breakdown).
    required_number_fields = (
        "realized_pnl_usd",
        "unrealized_pnl_usd",
        "net_pnl_usd",
        "loss_usd",
    )
    for f in required_number_fields:
        assert f in body, f"missing field: {f}"
        assert isinstance(body[f], (int, float)), (
            f"{f} must be numeric, got {type(body[f]).__name__}"
        )
        assert not isinstance(body[f], bool), f"{f} must not be bool"

    # Threshold + bankroll fields are number-or-null (None when the
    # operator has not configured a cap or no bankroll history exists).
    nullable_number_fields = (
        "threshold_usd",
        "threshold_pct",
        "threshold_pct_usd",
        "start_of_day_bankroll_usd",
        "used_pct_abs",
        "used_pct_pct",
        "used_pct_max",
    )
    for f in nullable_number_fields:
        assert f in body, f"missing field: {f}"
        v = body[f]
        assert v is None or (
            isinstance(v, (int, float)) and not isinstance(v, bool)
        ), f"{f} must be number-or-null, got {type(v).__name__}"

    assert "tripped" in body
    assert isinstance(body["tripped"], bool), "tripped must be bool"

    assert "tripped_reason" in body
    assert body["tripped_reason"] is None or isinstance(body["tripped_reason"], str)

    assert "configured" in body
    assert isinstance(body["configured"], bool), "configured must be bool"

    # Spot-check the values flow through unchanged.
    assert body["date_local"] == "2026-04-24"
    assert body["timezone"] == "UTC"
    assert body["loss_usd"] == 145.5
    assert body["threshold_usd"] == 500.0
    assert body["threshold_pct"] == 10.0
    assert body["threshold_pct_usd"] == 200.0
    assert body["start_of_day_bankroll_usd"] == 2000.0
    assert body["tripped"] is False
    assert body["configured"] is True


def test_daily_loss_endpoint_used_pct_math(client, monkeypatch):
    """``used_pct_*`` derivations are owned by the endpoint (not the
    snapshot dataclass), so pin the formula: percent of each cap that
    today's loss has consumed, with ``used_pct_max`` returning the
    most-restrictive (largest) consumption so the UI can render a
    single gauge."""
    from app.risk import daily_loss as _dl

    async def _snap(overrides=None, *, use_cache: bool = True):
        return _dl.DailyLossSnapshot(
            date_local="2026-04-24",
            timezone="UTC",
            trading_mode="live",
            realized_pnl_usd=-100.0,
            unrealized_pnl_usd=0.0,
            net_pnl_usd=-100.0,
            start_of_day_bankroll_usd=1000.0,
            threshold_usd=400.0,         # 100/400 = 25% used
            threshold_pct=5.0,
            threshold_pct_usd=50.0,      # 100/50 = 200% used (cap blown)
            loss_usd=100.0,
            tripped_abs=False,
            tripped_pct=True,
            tripped=True,
            tripped_reason="loss $100 >= 5% of bankroll ($50)",
        )

    monkeypatch.setattr(_dl, "compute_snapshot", _snap)
    r = client.get("/api/risk/daily-loss")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["used_pct_abs"] == 25.0
    assert body["used_pct_pct"] == 200.0
    assert body["used_pct_max"] == 200.0
    assert body["tripped"] is True
    assert body["configured"] is True
