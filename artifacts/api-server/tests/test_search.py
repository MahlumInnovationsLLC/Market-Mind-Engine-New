"""Unit tests for `dashboard.search_global`.

Stubs the asyncpg-style `db` accessor so each branch (ticker / market_id
substring, ISO-date filter, numeric cycle id, empty query, empty result)
can be exercised without a live database.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import dashboard


class _FakeDB:
    """Records every db.fetch call so tests can assert routing/params."""

    def __init__(
        self,
        trade_rows: list[dict[str, Any]] | None = None,
        debate_rows: list[dict[str, Any]] | None = None,
        cycle_rows: list[dict[str, Any]] | None = None,
        user_rows: list[dict[str, Any]] | None = None,
        audit_rows: list[dict[str, Any]] | None = None,
        rc_event_rows: list[dict[str, Any]] | None = None,
        stripe_customer_rows: list[dict[str, Any]] | None = None,
        rc_customer_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.trade_rows = trade_rows or []
        self.debate_rows = debate_rows or []
        self.cycle_rows = cycle_rows or []
        self.user_rows = user_rows or []
        self.audit_rows = audit_rows or []
        self.rc_event_rows = rc_event_rows or []
        self.stripe_customer_rows = stripe_customer_rows or []
        self.rc_customer_rows = rc_customer_rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        if "FROM trades" in query:
            return self.trade_rows
        if "FROM swarm_debates" in query:
            return self.debate_rows
        if "FROM trading_cycles" in query:
            return self.cycle_rows
        if "FROM app_users" in query and "rc_subscriptions" not in query:
            return self.user_rows
        if "FROM admin_audit_log" in query:
            return self.audit_rows
        if "rc_webhook_deliveries" in query:
            return self.rc_event_rows
        if "FROM rc_subscriptions" in query:
            return self.rc_customer_rows
        if "FROM subscriptions" in query:
            return self.stripe_customer_rows
        return []

    async def fetchrow(self, query: str, *args: Any) -> Any:  # pragma: no cover
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:  # pragma: no cover
        return None


def _run(coro):
    return asyncio.run(coro)


def test_empty_query_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(dashboard.search_global("   "))
    assert out == {
        "trades": [],
        "debates": [],
        "cycles": [],
        "users": [],
        "audit": [],
        "billing": [],
    }
    # No SQL fired — short-circuited before touching the db.
    assert fake.calls == []


def test_empty_query_short_circuits_for_admin_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(dashboard.search_global("   ", include_admin=True))
    assert out == {
        "trades": [],
        "debates": [],
        "cycles": [],
        "users": [],
        "audit": [],
        "billing": [],
    }
    assert fake.calls == []


def test_ticker_substring_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    trade = {
        "id": 1, "trade_id": "t-1", "market_id": "KXNFL-25SEP04SEAARI-SEA",
        "market_title": None, "side": "yes", "action": "buy",
        "price": 0.55, "pnl": 1.23, "executed_at": "2025-09-04T12:00:00Z",
    }
    fake = _FakeDB(trade_rows=[trade])
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("KXNFL", limit=5))

    assert out["trades"] == [trade]
    # First (trades) query must use the ILIKE substring branch with %query%.
    queries = [c[0] for c in fake.calls]
    trade_calls = [c for c in fake.calls if "FROM trades" in c[0]]
    assert trade_calls, "expected a trades query"
    q, args = trade_calls[0]
    assert "ILIKE" in q
    assert "executed_at::date = $1::date" not in q
    assert args[0] == "%KXNFL%"
    assert args[1] == 5
    # Debates and cycles also queried (no exception swallowing).
    assert any("FROM swarm_debates" in q for q in queries)
    assert any("FROM trading_cycles" in q for q in queries)


def test_iso_date_branch_uses_date_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)

    _run(dashboard.search_global("2025-09-04", limit=10))

    # All three queries should take the ::date branch and pass the raw
    # date string (not a %like% pattern).
    for table in ("FROM trades", "FROM swarm_debates", "FROM trading_cycles"):
        matches = [c for c in fake.calls if table in c[0]]
        assert matches, f"expected query against {table}"
        q, args = matches[0]
        assert "::date = $1::date" in q
        assert args[0] == "2025-09-04"
        assert args[1] == 10


def test_numeric_query_hits_cycle_id_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB(cycle_rows=[{"id": 42, "vertical": "sports",
                                "picks_evaluated": 1, "trades_placed": 1,
                                "started_at": "2025-09-04T12:00:00Z",
                                "completed_at": "2025-09-04T12:01:00Z"}])
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("42"))

    cycle_calls = [c for c in fake.calls if "FROM trading_cycles" in c[0]]
    assert cycle_calls
    q, args = cycle_calls[0]
    assert "WHERE id = $1" in q
    assert args[0] == 42  # coerced to int
    assert out["cycles"][0]["id"] == 42


def test_market_id_substring_matches_debate_kalshi_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    debate = {
        "id": 7, "league": "NFL", "matchup": "SEA @ ARI",
        "market_type": "moneyline",
        "consensus_probability": 0.6, "agreement_rate": 0.8, "approved": True,
        "recommended_action": "buy",
        "created_at": "2025-09-04T12:00:00Z",
    }
    fake = _FakeDB(debate_rows=[debate])
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("SEAARI"))
    debate_calls = [c for c in fake.calls if "FROM swarm_debates" in c[0]]
    assert debate_calls
    q, args = debate_calls[0]
    assert "kalshi_market_id" in q  # game_context substring branch
    assert args[0] == "%SEAARI%"
    assert out["debates"] == [debate]


def test_empty_result_set_returns_empty_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB()  # all empty
    monkeypatch.setattr(dashboard, "db", fake)
    out = _run(dashboard.search_global("nope-no-match"))
    assert out == {
        "trades": [],
        "debates": [],
        "cycles": [],
        "users": [],
        "audit": [],
        "billing": [],
    }


def test_non_admin_caller_skips_user_and_audit_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-admin callers must never trigger app_users / admin_audit_log
    / billing scans, and the returned envelope must still expose the
    keys so the palette can render them without conditional access."""
    fake = _FakeDB(
        user_rows=[{"clerk_user_id": "u1"}],  # would be returned if asked
        audit_rows=[{"id": 1}],
        rc_event_rows=[{"event_id": "evt_1"}],
        stripe_customer_rows=[{"user_id": "u1", "email": "a@b"}],
        rc_customer_rows=[{"user_id": "u1"}],
    )
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("ops"))

    queries = [c[0] for c in fake.calls]
    assert not any("FROM app_users" in q for q in queries)
    assert not any("FROM admin_audit_log" in q for q in queries)
    assert not any("rc_webhook_deliveries" in q for q in queries)
    assert not any("FROM rc_subscriptions" in q for q in queries)
    assert not any("FROM subscriptions" in q for q in queries)
    assert out["users"] == []
    assert out["audit"] == []
    assert out["billing"] == []


def test_admin_caller_searches_users_by_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = {
        "clerk_user_id": "user_abc",
        "email": "ops@example.com",
        "display_name": "Ops",
        "approved": True,
        "is_admin": False,
        "last_seen_at": "2026-04-24T12:00:00Z",
    }
    fake = _FakeDB(user_rows=[user])
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("ops", limit=5, include_admin=True))

    user_calls = [c for c in fake.calls if "FROM app_users" in c[0]]
    assert user_calls, "expected an app_users query for admin caller"
    q, args = user_calls[0]
    assert "ILIKE" in q
    assert "last_seen_at::date = $1::date" not in q
    assert args[0] == "%ops%"
    assert args[1] == 5
    assert out["users"] == [user]


def test_admin_caller_searches_audit_by_action_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = {
        "id": 314,
        "action": "set_approved",
        "actor_email": "admin@example.com",
        "actor_user_id": "user_admin",
        "target_email": "ops@example.com",
        "target_user_id": "user_ops",
        "created_at": "2026-04-24T12:00:00Z",
    }
    fake = _FakeDB(audit_rows=[entry])
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("set_approved", include_admin=True))

    audit_calls = [c for c in fake.calls if "FROM admin_audit_log" in c[0]]
    assert audit_calls
    q, args = audit_calls[0]
    assert "ILIKE" in q
    assert args[0] == "%set_approved%"
    assert out["audit"] == [entry]


def test_admin_numeric_query_targets_audit_id_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB(audit_rows=[{"id": 99, "action": "x"}])
    monkeypatch.setattr(dashboard, "db", fake)

    _run(dashboard.search_global("99", include_admin=True))

    audit_calls = [c for c in fake.calls if "FROM admin_audit_log" in c[0]]
    assert audit_calls
    q, args = audit_calls[0]
    assert "WHERE id = $1" in q
    assert args[0] == 99  # coerced to int
    assert args[1] == "%99%"  # textual fallback retains the substring


def test_admin_iso_date_filters_users_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)

    _run(dashboard.search_global("2025-09-04", include_admin=True))

    for table, col in (
        ("FROM app_users", "last_seen_at"),
        ("FROM admin_audit_log", "created_at"),
    ):
        matches = [c for c in fake.calls if table in c[0]]
        assert matches, f"expected query against {table}"
        q, args = matches[0]
        assert f"{col}::date = $1::date" in q
        assert args[0] == "2025-09-04"


def test_limit_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)
    _run(dashboard.search_global("foo", limit=9999))
    # limit arg is clamped to 50 across all sub-queries.
    for _, args in fake.calls:
        assert args[-1] == 50

    fake2 = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake2)
    _run(dashboard.search_global("foo", limit=0))
    for _, args in fake2.calls:
        assert args[-1] == 1


# ---------------------------------------------------------------------------
# Billing search (task #632) — admin-only RC webhook events + Stripe / RC
# customer rows surface under the new ``billing`` envelope key.
# ---------------------------------------------------------------------------


def test_admin_billing_event_substring_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substring query against `event_id` / last error returns RC events
    tagged ``kind="event"`` and routed to the failed-events table."""
    event = {
        "event_id": "evt_abc123",
        "event_type": "RENEWAL",
        "last_failed_at": "2026-04-24T12:00:00Z",
        "last_error": "boom: 502",
        "attempts": 3,
    }
    fake = _FakeDB(rc_event_rows=[event])
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("evt_abc", include_admin=True))

    rc_calls = [c for c in fake.calls if "rc_webhook_deliveries" in c[0]]
    assert rc_calls, "expected an rc_webhook_deliveries query for admin caller"
    q, args = rc_calls[0]
    assert "ILIKE" in q
    assert "received_at::date = $1::date" not in q
    assert args[0] == "%evt_abc%"
    # Latest 5xx wins — the SQL must filter on the http_5xx status bucket.
    assert "http_5xx" in q

    assert out["billing"] == [
        {
            "kind": "event",
            "provider": "revenuecat",
            "event_id": "evt_abc123",
            "event_type": "RENEWAL",
            "last_failed_at": "2026-04-24T12:00:00Z",
            "last_error": "boom: 502",
            "attempts": 3,
        }
    ]


def test_admin_billing_customer_email_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substring query against `email` returns Stripe + RC customer rows
    tagged ``kind="customer"`` with the appropriate provider field."""
    stripe_row = {
        "user_id": "user_stripe",
        "email": "ops@example.com",
        "tier": "pro",
        "status": "active",
        "stripe_customer_id": "cus_xyz",
        "updated_at": "2026-04-24T12:00:00Z",
    }
    rc_row = {
        "user_id": "user_rc",
        "app_user_id": "rc_app_1",
        "tier": "elite",
        "event_type": "INITIAL_PURCHASE",
        "email": "ops@example.com",
        "updated_at": "2026-04-24T12:30:00Z",
    }
    fake = _FakeDB(
        stripe_customer_rows=[stripe_row],
        rc_customer_rows=[rc_row],
    )
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("ops@example", include_admin=True))

    # Stripe sub-query routed to `subscriptions`.
    sub_calls = [
        c for c in fake.calls
        if "FROM subscriptions" in c[0] and "rc_subscriptions" not in c[0]
    ]
    assert sub_calls, "expected a subscriptions query for admin caller"
    q, args = sub_calls[0]
    assert "ILIKE" in q
    assert args[0] == "%ops@example%"

    # RC sub-query routed to `rc_subscriptions` (joined to app_users for email).
    rc_calls = [c for c in fake.calls if "FROM rc_subscriptions" in c[0]]
    assert rc_calls, "expected an rc_subscriptions query for admin caller"
    q, args = rc_calls[0]
    assert "ILIKE" in q
    assert "app_users" in q
    assert args[0] == "%ops@example%"

    # Both customer rows surface; provider tags identify the source.
    customers = [b for b in out["billing"] if b["kind"] == "customer"]
    providers = sorted(b["provider"] for b in customers)
    assert providers == ["revenuecat", "stripe"]
    stripe_hit = next(b for b in customers if b["provider"] == "stripe")
    assert stripe_hit["email"] == "ops@example.com"
    assert stripe_hit["tier"] == "pro"
    assert stripe_hit["stripe_customer_id"] == "cus_xyz"
    rc_hit = next(b for b in customers if b["provider"] == "revenuecat")
    assert rc_hit["app_user_id"] == "rc_app_1"
    assert rc_hit["tier"] == "elite"
    # status mirrors the latest RC event_type so the operator can tell
    # whether the customer is mid-renewal vs initial purchase.
    assert rc_hit["status"] == "INITIAL_PURCHASE"


def test_admin_billing_iso_date_filters_by_calendar_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISO-date queries restrict billing tables to that calendar day."""
    fake = _FakeDB()
    monkeypatch.setattr(dashboard, "db", fake)

    _run(dashboard.search_global("2026-04-24", include_admin=True))

    rc_calls = [c for c in fake.calls if "rc_webhook_deliveries" in c[0]]
    assert rc_calls
    q, args = rc_calls[0]
    assert "received_at::date = $1::date" in q
    assert args[0] == "2026-04-24"

    # subscriptions (Stripe) — match `updated_at::date`.
    sub_calls = [
        c for c in fake.calls
        if "FROM subscriptions" in c[0] and "rc_subscriptions" not in c[0]
    ]
    assert sub_calls
    q, args = sub_calls[0]
    assert "updated_at::date = $1::date" in q
    assert args[0] == "2026-04-24"

    # rc_subscriptions (RC) — also `updated_at::date`.
    rc_sub_calls = [c for c in fake.calls if "FROM rc_subscriptions" in c[0]]
    assert rc_sub_calls
    q, args = rc_sub_calls[0]
    assert "updated_at::date = $1::date" in q
    assert args[0] == "2026-04-24"


def test_admin_billing_query_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A db.fetch exception on any billing branch must not bubble up —
    the rest of the search envelope (and the other billing branches)
    should still come back populated."""

    class _BoomDB(_FakeDB):
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            self.calls.append((query, args))
            if "rc_webhook_deliveries" in query:
                raise RuntimeError("simulated rc_webhook_deliveries outage")
            return await super().fetch(query, *args)

    fake = _BoomDB(
        stripe_customer_rows=[
            {
                "user_id": "u1",
                "email": "x@y",
                "tier": "free",
                "status": "inactive",
                "stripe_customer_id": None,
                "updated_at": "2026-04-24T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(dashboard, "db", fake)

    out = _run(dashboard.search_global("x@y", include_admin=True))

    # The Stripe customer hit still surfaces despite the RC events outage.
    assert any(b["provider"] == "stripe" for b in out["billing"])
    # No RC event hit (the failing branch silently returns nothing).
    assert not any(b["kind"] == "event" for b in out["billing"])
