"""Endpoint-level tests for `/api/dashboard/swarm-personas-today`.

The route now accepts an optional ``date_utc=YYYY-MM-DD`` query
parameter so operators can scrub through recent days inline.  These
tests verify:

  * The default (no query param) still returns today's aggregate.
  * A valid in-range date is forwarded verbatim to the helper.
  * A malformed date is rejected with HTTP 400.
  * A date older than the lookback window is rejected with HTTP 400.
  * A future date is rejected with HTTP 400.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import dashboard, main as main_module
from app.auth import AuthedUser, require_approved_user
from app.main import SWARM_PERSONAS_TODAY_LOOKBACK_DAYS, app


@pytest.fixture
def override_user():
    """Bypass the Clerk-backed approval gate."""

    async def _fake_user() -> AuthedUser:
        return AuthedUser(user_id="op_1", session_id="sess_test", claims={})

    app.dependency_overrides[require_approved_user] = _fake_user
    yield
    app.dependency_overrides.pop(require_approved_user, None)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch):
    """Stub the dashboard helper and record every call."""
    seen: list[tuple] = []

    async def _fake(date_utc: str | None = None):
        seen.append((date_utc,))
        return {"date_utc": date_utc or "today", "items": []}

    monkeypatch.setattr(main_module.dashboard, "get_swarm_personas_today", _fake)
    return seen


@pytest.fixture
def client(override_user, captured) -> TestClient:
    return TestClient(app)


def _today_utc() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


def test_default_no_param_calls_helper_without_date(
    client: TestClient, captured: list[tuple]
) -> None:
    r = client.get("/api/dashboard/swarm-personas-today")
    assert r.status_code == 200, r.text
    # Helper called once with no date_utc argument (falls back to today).
    assert captured == [(None,)]


def test_valid_in_range_date_is_forwarded(
    client: TestClient, captured: list[tuple]
) -> None:
    yesterday = (
        datetime.now(tz=timezone.utc).date() - timedelta(days=1)
    ).isoformat()
    r = client.get(
        "/api/dashboard/swarm-personas-today",
        params={"date_utc": yesterday},
    )
    assert r.status_code == 200, r.text
    assert captured == [(yesterday,)]
    assert r.json()["date_utc"] == yesterday


def test_malformed_date_rejected(
    client: TestClient, captured: list[tuple]
) -> None:
    r = client.get(
        "/api/dashboard/swarm-personas-today",
        params={"date_utc": "2026/04/21"},
    )
    assert r.status_code == 400, r.text
    assert "YYYY-MM-DD" in r.json()["detail"]
    # Helper never invoked when validation fails.
    assert captured == []


def test_future_date_rejected(
    client: TestClient, captured: list[tuple]
) -> None:
    future = (
        datetime.now(tz=timezone.utc).date() + timedelta(days=1)
    ).isoformat()
    r = client.get(
        "/api/dashboard/swarm-personas-today",
        params={"date_utc": future},
    )
    assert r.status_code == 400, r.text
    assert captured == []


def test_too_old_date_rejected(
    client: TestClient, captured: list[tuple]
) -> None:
    too_old = (
        datetime.now(tz=timezone.utc).date()
        - timedelta(days=SWARM_PERSONAS_TODAY_LOOKBACK_DAYS + 1)
    ).isoformat()
    r = client.get(
        "/api/dashboard/swarm-personas-today",
        params={"date_utc": too_old},
    )
    assert r.status_code == 400, r.text
    assert captured == []


def test_oldest_in_window_date_accepted(
    client: TestClient, captured: list[tuple]
) -> None:
    oldest = (
        datetime.now(tz=timezone.utc).date()
        - timedelta(days=SWARM_PERSONAS_TODAY_LOOKBACK_DAYS)
    ).isoformat()
    r = client.get(
        "/api/dashboard/swarm-personas-today",
        params={"date_utc": oldest},
    )
    assert r.status_code == 200, r.text
    assert captured == [(oldest,)]


def test_helper_directly_unchanged_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper signature must continue to default to today when no
    date is supplied — guarding against accidental regressions."""
    import asyncio

    async def _stub_aggregate(date_utc: str):
        return [{"persona_id": "p", "debates": 1, "brier": 0.1,
                 "accuracy_pct": 100.0, "lean_yes": 1, "lean_no": 0,
                 "lean_pass": 0}]

    monkeypatch.setattr(
        "app.notifications.daily_summary.aggregate_persona_day",
        _stub_aggregate,
    )
    out = asyncio.new_event_loop().run_until_complete(
        dashboard.get_swarm_personas_today()
    )
    assert out["date_utc"] == _today_utc()
