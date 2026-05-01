"""Tests for the ``trading_mode`` filter on dashboard read endpoints.

This is the Round-11 Bug #6 follow-up (Task #752 step 1).  Every
mode-aware dashboard query was leaking cross-mode aggregates into the
operator's headline numbers — paper bumps would show up in the live
equity curve and vice versa.  The fix is two-layered:

1. **Pure SQL helpers** (`_trading_mode_predicate`, `_trading_mode_and_clause`,
   `_trading_mode_where_clause`, `_combine_where_and`) bake the value as a
   SQL literal from a closed enum (``paper``/``live`` only), with the same
   alias plumbing as the placed_by helpers so per-table joins stay correct.

2. **Route plumbing** — every mode-aware dashboard route grew an optional
   ``trading_mode`` query param that's resolved through the operator's
   current mode (`risk.trading_mode.resolve_trading_mode`) when omitted,
   and validated to ``paper``/``live`` (HTTP 400 on a typo) before being
   forwarded to the underlying coroutine.  ``all`` is intentionally NOT
   accepted on read endpoints — that's exactly the leakage we're fixing.

Hermetic test style: route handlers are exercised through `TestClient`
with the dashboard coroutine monkeypatched so we capture the kwargs
threaded by the route layer.  The actual SQL execution path is covered
by the helper-level tests below.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import dashboard
from app.auth import AuthedUser, require_approved_user, require_auth
from app.main import app


# ---------------------------------------------------------------------------
# Pure SQL-helper tests
# ---------------------------------------------------------------------------


class TestTradingModePredicate:
    """`_trading_mode_predicate` mirrors the `placed_by` helpers exactly:

    * empty / ``None`` / ``"all"`` → ``""`` (no filter)
    * ``"paper"`` / ``"live"`` → bare SQL boolean
    * anything else → ``ValueError`` (the route layer turns this into 400).
    """

    @pytest.mark.parametrize("empty", [None, "", "all"])
    def test_empty_filter_returns_empty_string(self, empty):
        assert dashboard._trading_mode_predicate(empty) == ""
        assert dashboard._trading_mode_predicate(empty, alias="t") == ""

    @pytest.mark.parametrize("mode", ["paper", "live"])
    def test_known_modes_bake_as_literal(self, mode):
        assert (
            dashboard._trading_mode_predicate(mode) == f"trading_mode = '{mode}'"
        )

    def test_alias_scopes_column(self):
        assert (
            dashboard._trading_mode_predicate("paper", alias="t")
            == "t.trading_mode = 'paper'"
        )

    @pytest.mark.parametrize("bad", ["PAPER", "Live", "demo", "any", "1"])
    def test_unknown_value_raises(self, bad):
        # Round-11 Bug #6: a typo in a route handler must NOT silently
        # return the unfiltered cross-mode aggregate.
        with pytest.raises(ValueError):
            dashboard._trading_mode_predicate(bad)


class TestTradingModeWhereAndClauses:
    """The ``WHERE`` / ``AND`` wrappers add the leading keyword + space
    when the predicate is non-empty, otherwise they collapse to ``""``."""

    def test_where_clause_paper(self):
        assert (
            dashboard._trading_mode_where_clause("paper")
            == " WHERE trading_mode = 'paper'"
        )

    def test_where_clause_empty(self):
        assert dashboard._trading_mode_where_clause(None) == ""

    def test_and_clause_live(self):
        assert (
            dashboard._trading_mode_and_clause("live", alias="b")
            == " AND b.trading_mode = 'live'"
        )

    def test_and_clause_empty(self):
        assert dashboard._trading_mode_and_clause("all") == ""


class TestCombineWhereAnd:
    """`_combine_where_and` joins the optional placed_by + trading_mode
    predicates — empty fragments drop out, non-empty ones get glued with
    `` AND `` after a single leading `` WHERE ``."""

    def test_no_predicates_returns_empty(self):
        assert dashboard._combine_where_and("", "") == ""

    def test_single_predicate(self):
        assert (
            dashboard._combine_where_and("trading_mode = 'paper'", "")
            == " WHERE trading_mode = 'paper'"
        )

    def test_two_predicates_glued_with_and(self):
        assert dashboard._combine_where_and(
            "placed_by = 'manual_ui'", "trading_mode = 'live'"
        ) == " WHERE placed_by = 'manual_ui' AND trading_mode = 'live'"

    def test_three_predicates(self):
        assert dashboard._combine_where_and(
            "a = 1", "b = 2", "c = 3"
        ) == " WHERE a = 1 AND b = 2 AND c = 3"


# ---------------------------------------------------------------------------
# Route plumbing tests — kwargs forwarded + bad input → 400
# ---------------------------------------------------------------------------


@pytest.fixture
def authed(monkeypatch):
    """Stub the auth dependencies so the dashboard routes are reachable
    without a real Clerk session — same pattern as test_dashboard_placed_by.
    """
    fake = AuthedUser(
        user_id="user_test",
        session_id="sess_test",
        claims={"sub": "user_test"},
    )

    async def _fake_user() -> AuthedUser:
        return fake

    app.dependency_overrides[require_auth] = _fake_user
    app.dependency_overrides[require_approved_user] = _fake_user
    yield fake
    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(require_approved_user, None)


@pytest.fixture
def patched_resolver(monkeypatch):
    """Pin the operator's current mode so tests don't depend on the live
    ``system_settings`` value or the env default.  Returning a sentinel
    here also proves the route falls back to it when the caller omits
    ``trading_mode``.
    """
    async def _fake_resolve(env_default: str | None = None) -> str:
        return "paper"

    import app.risk.trading_mode as tm

    monkeypatch.setattr(tm, "resolve_trading_mode", _fake_resolve)
    return _fake_resolve


def _stub_overview(monkeypatch, captured: list[dict[str, Any]]) -> None:
    async def _fake(
        placed_by: str | None = None, *, trading_mode: str | None = None
    ) -> dict[str, Any]:
        captured.append({"placed_by": placed_by, "trading_mode": trading_mode})
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
    "qs,expected_mode",
    [
        ("", "paper"),                       # falls back to resolver
        ("?trading_mode=paper", "paper"),
        ("?trading_mode=live", "live"),
        ("?trading_mode=LIVE", "live"),      # case-folded
        ("?trading_mode=  paper  ", "paper"),  # trimmed
    ],
)
def test_overview_forwards_trading_mode_to_dashboard_helper(
    monkeypatch, authed, patched_resolver, qs, expected_mode
):
    captured: list[dict[str, Any]] = []
    _stub_overview(monkeypatch, captured)
    client = TestClient(app)

    resp = client.get(f"/api/dashboard/overview{qs}")

    assert resp.status_code == 200, resp.text
    assert captured == [{"placed_by": "all", "trading_mode": expected_mode}]


@pytest.mark.parametrize("bad", ["demo", "all", "PAPERLIVE", "1"])
def test_overview_rejects_invalid_trading_mode_with_400(
    monkeypatch, authed, patched_resolver, bad
):
    """Round-11 Bug #6: a typo must surface as HTTP 400, not return the
    unfiltered aggregate. ``all`` is intentionally rejected on reads —
    cross-mode aggregation is exactly what we're closing here.
    """
    captured: list[dict[str, Any]] = []
    _stub_overview(monkeypatch, captured)
    client = TestClient(app)

    resp = client.get(f"/api/dashboard/overview?trading_mode={bad}")

    assert resp.status_code == 400, resp.text
    assert captured == []  # the helper must NEVER be called on bad input


def test_recent_trades_threads_trading_mode(
    monkeypatch, authed, patched_resolver
):
    captured: list[dict[str, Any]] = []

    async def _fake(
        limit: int = 25,
        placed_by: str | None = None,
        *,
        trading_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        captured.append(
            {
                "limit": limit,
                "placed_by": placed_by,
                "trading_mode": trading_mode,
            }
        )
        return []

    monkeypatch.setattr(dashboard, "get_recent_trades", _fake)
    client = TestClient(app)

    resp = client.get("/api/dashboard/recent-trades?limit=5&trading_mode=live")

    assert resp.status_code == 200, resp.text
    assert captured == [
        {"limit": 5, "placed_by": "all", "trading_mode": "live"}
    ]


def test_bankroll_recent_threads_trading_mode_and_echoes(
    monkeypatch, authed, patched_resolver
):
    captured: list[dict[str, Any]] = []

    async def _fake(
        limit: int = 200, *, trading_mode: str | None = None
    ) -> dict[str, Any]:
        captured.append({"limit": limit, "trading_mode": trading_mode})
        return {"events": [], "open_value": 0.0}

    monkeypatch.setattr(dashboard, "get_recent_bankroll_events", _fake)

    # The route also imports bankroll_sync.state(); stub it to a no-op so
    # the test doesn't depend on the global sync coroutine being running.
    from app import bankroll_sync as _bankroll_sync

    monkeypatch.setattr(
        _bankroll_sync, "state", lambda: {"healthy": True}
    )

    client = TestClient(app)

    resp = client.get(
        "/api/dashboard/bankroll/recent?limit=10&trading_mode=paper"
    )

    assert resp.status_code == 200, resp.text
    assert captured == [{"limit": 10, "trading_mode": "paper"}]
    body = resp.json()
    # Echo so the UI can reconstruct the active filter — same pattern as
    # the placed_by echo on /overview, /recent-trades etc.
    assert body.get("trading_mode") == "paper"


# ---------------------------------------------------------------------------
# Exposure routes — Round-11 second-pass architect finding
#
# `dashboard.get_nfl_exposure` and `get_leagues_exposure` source from
# `_open_position_aggregates` and `_latest_bankroll`, both of which now
# default to "live" for legacy caller safety.  Without route plumbing
# the exposure panels would silently always show live numbers even when
# the operator is running paper.  These tests prove the route resolves
# the runtime mode (paper here) and forwards it through.
# ---------------------------------------------------------------------------


def test_nfl_exposure_defaults_to_runtime_mode_paper(
    monkeypatch, authed, patched_resolver
):
    captured: list[dict[str, Any]] = []

    async def _fake(*, trading_mode: str = "live") -> dict[str, Any]:
        captured.append({"trading_mode": trading_mode})
        return {"league": "NFL"}

    monkeypatch.setattr(dashboard, "get_nfl_exposure", _fake)
    client = TestClient(app)

    resp = client.get("/api/dashboard/nfl-exposure")

    assert resp.status_code == 200, resp.text
    assert captured == [{"trading_mode": "paper"}]


def test_nfl_exposure_explicit_live_overrides_runtime_paper(
    monkeypatch, authed, patched_resolver
):
    captured: list[dict[str, Any]] = []

    async def _fake(*, trading_mode: str = "live") -> dict[str, Any]:
        captured.append({"trading_mode": trading_mode})
        return {"league": "NFL"}

    monkeypatch.setattr(dashboard, "get_nfl_exposure", _fake)
    client = TestClient(app)

    resp = client.get("/api/dashboard/nfl-exposure?trading_mode=live")

    assert resp.status_code == 200, resp.text
    assert captured == [{"trading_mode": "live"}]


def test_nfl_exposure_rejects_bad_trading_mode_with_400(
    monkeypatch, authed, patched_resolver
):
    captured: list[dict[str, Any]] = []

    async def _fake(*, trading_mode: str = "live") -> dict[str, Any]:
        captured.append({"trading_mode": trading_mode})
        return {"league": "NFL"}

    monkeypatch.setattr(dashboard, "get_nfl_exposure", _fake)
    client = TestClient(app)

    resp = client.get("/api/dashboard/nfl-exposure?trading_mode=demo")

    assert resp.status_code == 400, resp.text
    assert captured == []


def test_leagues_exposure_threads_trading_mode(
    monkeypatch, authed, patched_resolver
):
    captured: list[dict[str, Any]] = []

    async def _fake(*, trading_mode: str = "live") -> dict[str, Any]:
        captured.append({"trading_mode": trading_mode})
        return {"leagues": []}

    monkeypatch.setattr(dashboard, "get_leagues_exposure", _fake)
    client = TestClient(app)

    resp = client.get("/api/dashboard/leagues-exposure?trading_mode=paper")

    assert resp.status_code == 200, resp.text
    assert captured == [{"trading_mode": "paper"}]


def test_leagues_exposure_defaults_to_runtime_mode_paper(
    monkeypatch, authed, patched_resolver
):
    captured: list[dict[str, Any]] = []

    async def _fake(*, trading_mode: str = "live") -> dict[str, Any]:
        captured.append({"trading_mode": trading_mode})
        return {"leagues": []}

    monkeypatch.setattr(dashboard, "get_leagues_exposure", _fake)
    client = TestClient(app)

    resp = client.get("/api/dashboard/leagues-exposure")

    assert resp.status_code == 200, resp.text
    assert captured == [{"trading_mode": "paper"}]
