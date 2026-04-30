"""Tests for the trade-provenance (`placed_by`) filter.

This covers two layers:

1. The pure SQL-fragment helpers in :mod:`app.dashboard`
   (`_placed_by_predicate`, `_placed_by_and_clause`,
   `_placed_by_where_clause`) — these bake their values as SQL literals
   so the closed enum and the ``alias=`` plumbing must be exact.

2. The four FastAPI routes that took on the new
   ``placed_by: Literal['manual','system','all'] = 'all'`` query param:

     * ``GET /api/dashboard/overview``
     * ``GET /api/dashboard/recent-trades``
     * ``GET /api/dashboard/vertical-performance``
     * ``GET /api/dashboard/verticals``

   The route layer is the trust boundary — FastAPI's ``Literal`` rejects
   anything outside the enum with HTTP 422 — and we additionally verify
   the value is forwarded to the underlying ``dashboard.*`` coroutine
   and echoed back in the response so the UI can reconstruct the active
   filter without re-parsing the URL.

Live-DB integration (the actual SQL execution against Supabase) was
manually probed during T003; here we exercise just the route plumbing
with monkeypatched dashboard functions to keep the test hermetic.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import billing, dashboard
from app.auth import AuthedUser, require_approved_user
from app.main import app


# ---------------------------------------------------------------------------
# Pure helper tests — _placed_by_predicate / _and_clause / _where_clause
# ---------------------------------------------------------------------------


class TestPlacedByPredicate:
    """`_placed_by_predicate` returns a bare boolean SQL expression.

    ``""`` / ``None`` / ``"all"`` all mean *no filter* — caller appends
    nothing.  The other two map to the closed buckets defined in
    :data:`dashboard._PLACED_BY_SYSTEM_VALUES`.
    """

    @pytest.mark.parametrize("empty", [None, "", "all"])
    def test_empty_filter_returns_empty_string(self, empty):
        assert dashboard._placed_by_predicate(empty) == ""
        # alias must not change the empty case — nothing to scope
        assert dashboard._placed_by_predicate(empty, alias="t") == ""

    def test_manual_maps_to_manual_ui(self):
        assert (
            dashboard._placed_by_predicate("manual") == "placed_by = 'manual_ui'"
        )

    def test_system_lists_orchestrator_autotrader_swarm_operator_recovery(self):
        # Order is fixed by the tuple in dashboard.py; pin it so any
        # accidental reorder shows up here rather than as a silent SQL
        # behaviour change.  `operator_recovery` is a system-side
        # reconciliation tag (see dashboard._PLACED_BY_SYSTEM_VALUES
        # docstring) so it groups with the autotrader / swarm buckets.
        assert dashboard._placed_by_predicate("system") == (
            "placed_by IN ('orchestrator','autotrader','swarm','operator_recovery')"
        )

    def test_alias_scopes_column(self):
        assert (
            dashboard._placed_by_predicate("manual", alias="t")
            == "t.placed_by = 'manual_ui'"
        )
        assert dashboard._placed_by_predicate("system", alias="x") == (
            "x.placed_by IN ('orchestrator','autotrader','swarm','operator_recovery')"
        )

    def test_unknown_value_raises_valueerror(self):
        # Defensive even though FastAPI's Literal rejects this at the
        # HTTP boundary — internal callers (tests, future code) should
        # blow up rather than silently produce an empty WHERE.
        with pytest.raises(ValueError, match="unknown placed_by filter"):
            dashboard._placed_by_predicate("rh_import")
        with pytest.raises(ValueError):
            dashboard._placed_by_predicate("ORCHESTRATOR")  # case-sensitive


class TestPlacedByAndClause:
    """`_placed_by_and_clause` returns ``" AND <pred>"`` (with leading
    space) or ``""`` — used inside queries that already have a WHERE.
    """

    @pytest.mark.parametrize("empty", [None, "", "all"])
    def test_empty_filter_returns_empty_string(self, empty):
        assert dashboard._placed_by_and_clause(empty) == ""

    def test_manual_emits_leading_space_and_AND(self):
        # Leading space is load-bearing — splices safely regardless of
        # whether the prior line ended with whitespace.
        assert (
            dashboard._placed_by_and_clause("manual")
            == " AND placed_by = 'manual_ui'"
        )

    def test_system_with_alias(self):
        assert dashboard._placed_by_and_clause("system", alias="t") == (
            " AND t.placed_by IN ('orchestrator','autotrader','swarm','operator_recovery')"
        )


class TestPlacedByWhereClause:
    """`_placed_by_where_clause` returns ``" WHERE <pred>"`` or ``""`` —
    used by the bare aggregate queries in :func:`get_overview`.
    """

    @pytest.mark.parametrize("empty", [None, "", "all"])
    def test_empty_filter_returns_empty_string(self, empty):
        assert dashboard._placed_by_where_clause(empty) == ""

    def test_manual_emits_leading_space_and_WHERE(self):
        assert (
            dashboard._placed_by_where_clause("manual")
            == " WHERE placed_by = 'manual_ui'"
        )

    def test_system(self):
        assert dashboard._placed_by_where_clause("system") == (
            " WHERE placed_by IN ('orchestrator','autotrader','swarm','operator_recovery')"
        )


# ---------------------------------------------------------------------------
# Route plumbing tests — the 4 endpoints take ?placed_by=... and forward
# it as a kwarg to the underlying dashboard.* coroutine.
# ---------------------------------------------------------------------------


@pytest.fixture
def override_auth():
    """Bypass auth; tests assert filter plumbing, not auth."""
    user = AuthedUser(
        user_id="user_test", session_id="sess_test", claims={"sub": "user_test"}
    )
    app.dependency_overrides[require_approved_user] = lambda: user
    yield user
    app.dependency_overrides.pop(require_approved_user, None)


@pytest.fixture
def patch_subscription(monkeypatch):
    """`/overview` + `/recent-trades` call ``billing.get_user_subscription``
    to render the free-tier banner.  Pin a Pro subscription so the
    delayed-feed branch doesn't mutate the response shape we're asserting
    on.  These tests are about ``placed_by`` plumbing, not tier gating —
    that's covered by ``test_free_tier_gating``.
    """
    sub = billing.UserSubscription(
        user_id="user_test",
        email="user_test@example.com",
        tier=billing.TIER_PRO,
        status="active",
        stripe_customer_id=None,
        stripe_subscription_id=None,
        stripe_price_id=None,
        interval=None,
        current_period_end=None,
        cancel_at_period_end=False,
        is_founder=False,
    )

    async def _fake_get(user: AuthedUser) -> billing.UserSubscription:
        return sub

    monkeypatch.setattr(billing, "get_user_subscription", _fake_get)
    return sub


@pytest.fixture
def captured_calls():
    """Mutable list callers append to so test can inspect kwargs after
    the request returns.  Using a list (not nonlocal) keeps the
    monkeypatched stub a plain async function rather than a closure
    over a mutable dict, which trips up some pytest fixtures."""
    return []


# ---- /api/dashboard/overview ---------------------------------------------


def _stub_overview(monkeypatch, captured: list[Any]) -> None:
    async def _fake(placed_by: str | None = None) -> dict[str, Any]:
        captured.append({"fn": "overview", "placed_by": placed_by})
        return {
            "counts": {"trades": 0},
            "latest_cycle_at": None,
            "latest_trade_at": None,
            "realized_pnl": 0.0,
            "win_rate": None,
            "open_positions": 0,
            "verticals": [],
            "active_verticals": [],
        }

    monkeypatch.setattr(dashboard, "get_overview", _fake)


@pytest.mark.parametrize(
    "qs,expected_kwarg,expected_echo",
    [
        ("", "all", "all"),                       # default
        ("?placed_by=all", "all", "all"),
        ("?placed_by=manual", "manual", "manual"),
        ("?placed_by=system", "system", "system"),
    ],
)
def test_overview_forwards_placed_by_and_echoes_in_response(
    monkeypatch,
    override_auth,
    patch_subscription,
    captured_calls,
    qs,
    expected_kwarg,
    expected_echo,
):
    _stub_overview(monkeypatch, captured_calls)
    client = TestClient(app)

    resp = client.get(f"/api/dashboard/overview{qs}")

    assert resp.status_code == 200, resp.text
    assert captured_calls == [{"fn": "overview", "placed_by": expected_kwarg}]
    body = resp.json()
    # Echo lets the UI reconstruct the active toggle from the response
    # without re-parsing the URL — see T004/T005.
    assert body.get("placed_by") == expected_echo


def test_overview_rejects_unknown_placed_by_with_422(
    monkeypatch, override_auth, patch_subscription, captured_calls
):
    # Literal['manual','system','all'] in the route signature → FastAPI
    # rejects everything else at the boundary; the underlying coroutine
    # never runs.
    _stub_overview(monkeypatch, captured_calls)
    client = TestClient(app)

    resp = client.get("/api/dashboard/overview?placed_by=rh_import")

    assert resp.status_code == 422
    assert captured_calls == []


# ---- /api/dashboard/recent-trades ----------------------------------------


def _stub_recent_trades(monkeypatch, captured: list[Any]) -> None:
    async def _fake(
        limit: int = 25, placed_by: str | None = None
    ) -> list[dict[str, Any]]:
        captured.append(
            {"fn": "recent_trades", "limit": limit, "placed_by": placed_by}
        )
        return []

    monkeypatch.setattr(dashboard, "get_recent_trades", _fake)


@pytest.mark.parametrize(
    "qs,expected_kwarg",
    [
        ("?limit=10", "all"),                              # default
        ("?limit=10&placed_by=manual", "manual"),
        ("?limit=10&placed_by=system", "system"),
        ("?limit=10&placed_by=all", "all"),
    ],
)
def test_recent_trades_forwards_placed_by(
    monkeypatch,
    override_auth,
    patch_subscription,
    captured_calls,
    qs,
    expected_kwarg,
):
    _stub_recent_trades(monkeypatch, captured_calls)
    client = TestClient(app)

    resp = client.get(f"/api/dashboard/recent-trades{qs}")

    assert resp.status_code == 200, resp.text
    assert len(captured_calls) == 1
    assert captured_calls[0]["placed_by"] == expected_kwarg
    assert captured_calls[0]["limit"] == 10
    body = resp.json()
    assert body.get("placed_by") == expected_kwarg


# ---- /api/dashboard/vertical-performance ---------------------------------


def _stub_vertical_perf(monkeypatch, captured: list[Any]) -> None:
    async def _fake(
        days: int = 30, placed_by: str | None = None
    ) -> dict[str, Any]:
        captured.append(
            {"fn": "vertical_perf", "days": days, "placed_by": placed_by}
        )
        return {"days": days, "verticals": []}

    monkeypatch.setattr(dashboard, "get_vertical_performance", _fake)


@pytest.mark.parametrize(
    "qs,expected_kwarg",
    [
        ("?days=7", "all"),
        ("?days=7&placed_by=manual", "manual"),
        ("?days=7&placed_by=system", "system"),
    ],
)
def test_vertical_performance_forwards_placed_by(
    monkeypatch, override_auth, captured_calls, qs, expected_kwarg
):
    _stub_vertical_perf(monkeypatch, captured_calls)
    client = TestClient(app)

    resp = client.get(f"/api/dashboard/vertical-performance{qs}")

    assert resp.status_code == 200, resp.text
    assert captured_calls[0]["placed_by"] == expected_kwarg
    body = resp.json()
    assert body.get("placed_by") == expected_kwarg


# ---- /api/dashboard/verticals --------------------------------------------


def _stub_verticals_overview(monkeypatch, captured: list[Any]) -> None:
    async def _fake(
        days: int = 7, placed_by: str | None = None
    ) -> dict[str, Any]:
        captured.append(
            {"fn": "verticals_overview", "days": days, "placed_by": placed_by}
        )
        return {"days": days, "verticals": []}

    monkeypatch.setattr(dashboard, "get_verticals_overview", _fake)


@pytest.mark.parametrize(
    "qs,expected_kwarg",
    [
        ("?days=7", "all"),
        ("?days=7&placed_by=manual", "manual"),
        ("?days=7&placed_by=system", "system"),
        ("?days=7&placed_by=all", "all"),
    ],
)
def test_verticals_overview_forwards_placed_by(
    monkeypatch, override_auth, captured_calls, qs, expected_kwarg
):
    _stub_verticals_overview(monkeypatch, captured_calls)
    client = TestClient(app)

    resp = client.get(f"/api/dashboard/verticals{qs}")

    assert resp.status_code == 200, resp.text
    assert captured_calls[0]["placed_by"] == expected_kwarg
    body = resp.json()
    assert body.get("placed_by") == expected_kwarg
