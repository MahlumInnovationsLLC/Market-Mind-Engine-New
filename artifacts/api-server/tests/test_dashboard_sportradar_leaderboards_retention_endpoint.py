"""HTTP-level coverage for ``GET /api/dashboard/sportradar-leaderboards-retention``.

The unit-test module ``test_dashboard_sportradar_leaderboards_retention``
already pins the rollup logic at the
``dashboard.get_sportradar_leaderboards_retention_status`` layer.  This
module is its route-level companion: it exercises the FastAPI handler
through an in-process client to pin three things the unit tests cannot
catch on their own:

* the auth dependency (``require_approved_user``) is wired up — an
  unauthenticated caller is rejected with ``401`` exactly the way the
  sibling crypto-backtest-retention endpoint is;
* the response payload shape conforms to the documented
  ``SportradarLeaderboardsRetentionStatus`` OpenAPI schema (required
  fields are present with the right primitive types, nested
  ``policy`` carries the documented retention horizons, and ``health``
  is one of the four documented enum values);
* the handler round-trips the underlying ``dashboard`` helper's payload
  unchanged — i.e. there's no transformation layer that could drift
  away from the OpenAPI contract / generated React client.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.auth import AuthedUser, require_approved_user
from app.main import app


# Fields the OpenAPI ``SportradarLeaderboardsRetentionStatus`` schema
# marks as required.  Kept in sync with ``lib/api-spec/openapi.yaml``.
_REQUIRED_FIELDS: tuple[str, ...] = (
    "running",
    "enabled",
    "last_deleted",
    "total_deleted",
    "policy",
    "interval_seconds",
    "health",
)

# Optional / nullable fields the schema also documents.  Asserted to be
# present (with the right type when non-null) so the route can't silently
# drop them.
_NULLABLE_FIELDS: tuple[str, ...] = (
    "started_at",
    "last_tick_at",
    "last_error",
    "seconds_since_last_tick",
)

_HEALTH_ENUM = {"ok", "stale", "error", "disabled"}


def _fake_user() -> AuthedUser:
    return AuthedUser(user_id="op_1", session_id="sess_t", claims={})


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_endpoint_rejects_unauthenticated_caller(client: TestClient) -> None:
    """Mirrors the sibling crypto-backtest-retention guard: a request
    with no Authorization header must be rejected with 401, never leak
    the retention snapshot anonymously."""
    resp = client.get("/api/dashboard/sportradar-leaderboards-retention")
    assert resp.status_code == 401, resp.text


def test_endpoint_returns_documented_schema_shape(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """The handler is a thin pass-through over
    ``dashboard.get_sportradar_leaderboards_retention_status``.  Stub
    the helper with a payload that exercises every documented field
    and assert the route returns it unchanged with the schema-required
    keys + types intact.
    """
    payload: dict[str, Any] = {
        "running": True,
        "enabled": True,
        "started_at": 1_700_000_000.0,
        "last_tick_at": 1_700_000_300.0,
        "last_error": None,
        "last_deleted": 7,
        "total_deleted": 42,
        "policy": {"min_snapshots": 5, "retention_days": 14},
        "interval_seconds": 300.0,
        "seconds_since_last_tick": 12.5,
        "health": "ok",
    }

    async def _fake() -> dict[str, Any]:
        return payload

    monkeypatch.setattr(
        main_module.dashboard,
        "get_sportradar_leaderboards_retention_status",
        _fake,
    )
    app.dependency_overrides[require_approved_user] = _fake_user
    try:
        resp = client.get("/api/dashboard/sportradar-leaderboards-retention")
    finally:
        app.dependency_overrides.pop(require_approved_user, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Helper payload is round-tripped verbatim — no shaping layer.
    assert body == payload

    # OpenAPI ``required`` fields are present.
    for key in _REQUIRED_FIELDS:
        assert key in body, f"missing required field: {key}"

    # Nullable / optional documented fields are present too.
    for key in _NULLABLE_FIELDS:
        assert key in body, f"missing nullable field: {key}"

    # Primitive-type guards mirroring the OpenAPI ``type:`` declarations.
    assert isinstance(body["running"], bool)
    assert isinstance(body["enabled"], bool)
    assert isinstance(body["last_deleted"], int)
    assert isinstance(body["total_deleted"], int)
    assert isinstance(body["interval_seconds"], (int, float))
    assert isinstance(body["started_at"], (int, float))
    assert isinstance(body["last_tick_at"], (int, float))
    assert isinstance(body["seconds_since_last_tick"], (int, float))
    assert body["last_error"] is None or isinstance(body["last_error"], str)

    # Nested SportradarLeaderboardsRetentionPolicy contract.
    policy = body["policy"]
    assert isinstance(policy, dict)
    assert isinstance(policy.get("min_snapshots"), int)
    assert isinstance(policy.get("retention_days"), int)

    # Health enum guard — flags a future refactor that introduces a new
    # state without updating the OpenAPI spec / dashboard color table.
    assert body["health"] in _HEALTH_ENUM


def test_endpoint_passes_through_disabled_health(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """A second pass with ``health=disabled`` + ``last_tick_at=None``
    pins the ``nullable: true`` fields of the schema (the happy-path
    test above only exercises non-null values)."""
    payload: dict[str, Any] = {
        "running": False,
        "enabled": False,
        "started_at": None,
        "last_tick_at": None,
        "last_error": None,
        "last_deleted": 0,
        "total_deleted": 0,
        "policy": {"min_snapshots": 3, "retention_days": 30},
        "interval_seconds": 300.0,
        "seconds_since_last_tick": None,
        "health": "disabled",
    }

    async def _fake() -> dict[str, Any]:
        return payload

    monkeypatch.setattr(
        main_module.dashboard,
        "get_sportradar_leaderboards_retention_status",
        _fake,
    )
    app.dependency_overrides[require_approved_user] = _fake_user
    try:
        resp = client.get("/api/dashboard/sportradar-leaderboards-retention")
    finally:
        app.dependency_overrides.pop(require_approved_user, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == payload
    assert body["started_at"] is None
    assert body["last_tick_at"] is None
    assert body["seconds_since_last_tick"] is None
    assert body["health"] == "disabled"
