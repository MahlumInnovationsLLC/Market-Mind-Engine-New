"""Free-tier signal gating: 15-minute delay + 5 markets/day quota.

Covers the three fixture cases called out by the spec:

1. ``recent-trades`` and ``recent-debates`` drop sub-15-min rows for
   free callers and serve them verbatim to Pro+ callers.
2. A free caller can evaluate up to five distinct markets in a UTC
   day; re-opening a market they already viewed is free.
3. The 6th distinct market in a UTC day returns HTTP 402 with the
   ``upgrade_required`` payload, and the underlying ``viewed_today``
   counter is read off ``free_tier_market_views``, which keys on a
   UTC ``DATE`` column (so the counter resets at UTC midnight by
   construction).

The asyncpg pool is replaced with an in-memory fake; the billing
subscription lookup is stubbed per-test so the suite never touches
the real Postgres or Stripe.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import billing, dashboard, free_tier
from app.auth import AuthedUser, require_approved_user
from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(uid: str = "user_free") -> AuthedUser:
    return AuthedUser(
        user_id=uid, session_id="sess_test", claims={"sub": uid}
    )


def _sub(tier: str, *, uid: str = "user_free") -> billing.UserSubscription:
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


def _iso(dt: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` — matches the dashboard's ``to_char``."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def patch_subscription(monkeypatch):
    holder: dict[str, billing.UserSubscription] = {
        "value": _sub(billing.TIER_FREE)
    }

    async def _fake_get(user: AuthedUser) -> billing.UserSubscription:
        return holder["value"]

    monkeypatch.setattr(billing, "get_user_subscription", _fake_get)
    return holder


@pytest.fixture
def override_auth():
    user = _user()
    app.dependency_overrides[require_approved_user] = lambda: user
    yield user
    app.dependency_overrides.pop(require_approved_user, None)


# ---------------------------------------------------------------------------
# In-memory fakes for the quota DB
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal stand-in for an asyncpg connection used by
    :func:`free_tier.enforce_market_view_quota`.

    Tracks ``(user_id, view_date, market_id)`` rows.  The "view date"
    is taken from a single ``today`` knob on the parent fake so tests
    can simulate a UTC-midnight rollover without sleeping.
    """

    def __init__(self, store: "_FakeQuotaStore") -> None:
        self._store = store

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        today = self._store.today
        if "SELECT 1 FROM free_tier_market_views" in q:
            user_id, market_id = args
            return 1 if (user_id, today, market_id) in self._store.rows else None
        if "SELECT count(*)::int FROM free_tier_market_views" in q:
            user_id = args[0]
            return sum(
                1 for (u, d, _) in self._store.rows if u == user_id and d == today
            )
        raise AssertionError(f"unexpected fetchval: {q!r}")

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        if "pg_advisory_xact_lock" in q:
            # Serialize concurrent quota checks for the same
            # (user_id, UTC day).  The real Postgres lock is
            # transaction-scoped; in-process we model it as an
            # asyncio.Lock per (user, day) acquired for the lifetime
            # of the surrounding ``transaction()`` context.
            user_id = args[0]
            await self._store.acquire_user_day_lock(user_id, self)
            return "OK"
        if q.startswith("INSERT INTO free_tier_market_views"):
            user_id, market_id = args
            self._store.rows.add((user_id, self._store.today, market_id))
            return "OK"
        raise AssertionError(f"unexpected execute: {q!r}")


class _FakeQuotaStore:
    def __init__(self) -> None:
        import asyncio

        self.rows: set[tuple[str, str, str]] = set()
        self.today: str = "2026-04-22"
        self._asyncio = asyncio
        self._user_day_locks: dict[tuple[str, str], "asyncio.Lock"] = {}
        self._held_by: dict[int, tuple[str, str]] = {}

    def _lock_for(self, user_id: str):
        key = (user_id, self.today)
        lock = self._user_day_locks.get(key)
        if lock is None:
            lock = self._asyncio.Lock()
            self._user_day_locks[key] = lock
        return key, lock

    async def acquire_user_day_lock(self, user_id: str, conn: "_FakeConn") -> None:
        key, lock = self._lock_for(user_id)
        await lock.acquire()
        self._held_by[id(conn)] = key

    def release_user_day_lock(self, conn: "_FakeConn") -> None:
        key = self._held_by.pop(id(conn), None)
        if key is None:
            return
        lock = self._user_day_locks.get(key)
        if lock is not None and lock.locked():
            lock.release()

    def acquire(self):
        store = self

        @asynccontextmanager
        async def _ctx():
            conn = _FakeConn(store)
            try:
                yield conn
            finally:
                store.release_user_day_lock(conn)

        return _ctx()


@pytest.fixture
def fake_quota_db(monkeypatch):
    store = _FakeQuotaStore()
    from app import db as db_module

    monkeypatch.setattr(db_module, "acquire", store.acquire)

    async def _fake_fetchval(query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if "SELECT count(*)::int FROM free_tier_market_views" in q:
            user_id = args[0]
            return sum(
                1 for (u, d, _) in store.rows if u == user_id and d == store.today
            )
        raise AssertionError(f"unexpected db.fetchval: {q!r}")

    monkeypatch.setattr(db_module, "fetchval", _fake_fetchval)
    return store


# ---------------------------------------------------------------------------
# Fixture case #1 — 15-minute signal delay
# ---------------------------------------------------------------------------


def _seed_trades(monkeypatch, now: datetime) -> list[dict[str, Any]]:
    """Three trades: 2 min ago (fresh), 16 min ago (delayed-eligible),
    and 1 hour ago (definitely eligible)."""
    rows = [
        {"id": 1, "market_id": "m-fresh",  "executed_at": _iso(now - timedelta(minutes=2))},
        {"id": 2, "market_id": "m-edge",   "executed_at": _iso(now - timedelta(minutes=16))},
        {"id": 3, "market_id": "m-old",    "executed_at": _iso(now - timedelta(hours=1))},
    ]

    async def _fake(
        limit: int = 25, placed_by: str | None = None
    ) -> list[dict[str, Any]]:
        # ``placed_by`` was added by migration 0039 + the trade-provenance
        # filter (Manual / System / Both toggle).  This stub ignores the
        # value because these free-tier tests assert the 15-minute delay
        # behaviour, not the placed_by filter — the dedicated
        # `test_dashboard_placed_by.py` covers that filter.
        return [dict(r) for r in rows]

    monkeypatch.setattr(dashboard, "get_recent_trades", _fake)
    return rows


def test_free_caller_only_sees_signals_older_than_15_min(
    monkeypatch, patch_subscription, override_auth
):
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(free_tier, "datetime", _FixedDatetime)
    _seed_trades(monkeypatch, now)
    patch_subscription["value"] = _sub(billing.TIER_FREE)

    client = TestClient(app)
    resp = client.get("/api/dashboard/recent-trades?limit=10")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delayed"] is True
    assert body["delay_minutes"] == 15
    ids = [it["id"] for it in body["items"]]
    # The 2-min-old row is hidden; the 16-min and 1h rows survive.
    assert ids == [2, 3]


def test_pro_caller_sees_every_signal_unfiltered(
    monkeypatch, patch_subscription, override_auth
):
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    _seed_trades(monkeypatch, now)
    patch_subscription["value"] = _sub(billing.TIER_PRO)

    client = TestClient(app)
    resp = client.get("/api/dashboard/recent-trades?limit=10")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delayed"] is False
    assert [it["id"] for it in body["items"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Fixture case #2 — quota counts distinct markets, repeats are free
# ---------------------------------------------------------------------------


def _seed_trade_detail(
    monkeypatch,
    market_id: str,
    *,
    executed_at: str = "2020-01-01T00:00:00Z",
    series: list[dict[str, Any]] | None = None,
) -> None:
    """Stub :func:`dashboard.get_trade_detail` with a deterministic row.

    ``executed_at`` defaults to a far-past timestamp so the free-tier
    15-minute delay never drops the row in tests that aren't asserting
    on the delay itself.
    """
    async def _fake(trade_ref: str) -> dict[str, Any]:
        return {
            "trade": {
                "id": int(trade_ref) if trade_ref.isdigit() else 1,
                "market_id": market_id,
                "executed_at": executed_at,
            },
            "series": list(series) if series is not None else [],
        }

    monkeypatch.setattr(dashboard, "get_trade_detail", _fake)


def test_free_caller_can_evaluate_five_distinct_markets_and_repeats_are_free(
    monkeypatch, patch_subscription, override_auth, fake_quota_db
):
    patch_subscription["value"] = _sub(billing.TIER_FREE)
    client = TestClient(app)

    for i, market in enumerate(["m1", "m2", "m3", "m4", "m5"], start=1):
        _seed_trade_detail(monkeypatch, market)
        resp = client.get(f"/api/dashboard/trades/{i}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["quota"]["viewed_today"] == i

    # Re-opening m3 does not consume additional quota.
    _seed_trade_detail(monkeypatch, "m3")
    resp = client.get("/api/dashboard/trades/3")
    assert resp.status_code == 200, resp.text
    assert resp.json()["quota"]["viewed_today"] == 5


# ---------------------------------------------------------------------------
# Fixture case #3 — 6th distinct market returns 402, and the counter
# is keyed on a UTC date so it resets at midnight.
# ---------------------------------------------------------------------------


def test_sixth_distinct_market_returns_402_then_resets_at_utc_midnight(
    monkeypatch, patch_subscription, override_auth, fake_quota_db
):
    patch_subscription["value"] = _sub(billing.TIER_FREE)
    client = TestClient(app)

    # Burn through the day's quota.
    for i, market in enumerate(["m1", "m2", "m3", "m4", "m5"], start=1):
        _seed_trade_detail(monkeypatch, market)
        resp = client.get(f"/api/dashboard/trades/{i}")
        assert resp.status_code == 200, resp.text

    # Sixth distinct market — locked out with the 402 payload the
    # frontend renders an upsell against.
    _seed_trade_detail(monkeypatch, "m6")
    resp = client.get("/api/dashboard/trades/6")
    assert resp.status_code == 402, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "upgrade_required"
    assert detail["reason"] == "free_tier_daily_market_limit"
    assert detail["limit"] == 5
    assert detail["viewed_today"] == 5
    assert detail["market_id"] == "m6"

    # UTC midnight rolls the partition forward; the quota resets.
    fake_quota_db.today = "2026-04-23"
    _seed_trade_detail(monkeypatch, "m6")
    resp = client.get("/api/dashboard/trades/6")
    assert resp.status_code == 200, resp.text
    assert resp.json()["quota"]["viewed_today"] == 1


# ---------------------------------------------------------------------------
# Concurrency — two parallel quota checks at the boundary can't both win.
# ---------------------------------------------------------------------------


def test_quota_is_race_safe_at_the_5th_to_6th_boundary(fake_quota_db):
    """Burn 4 markets, then fire two parallel calls for two *new*
    markets.  Exactly one must succeed; the other must hit the 402.

    Without the per-(user, UTC day) advisory lock, both would observe
    ``count_today == 4`` and both would insert, ending the day at 6
    distinct markets.  This test pins the invariant.
    """
    import asyncio

    user_id = "user_race"

    async def _scenario():
        for m in ["m1", "m2", "m3", "m4"]:
            await free_tier.enforce_market_view_quota(user_id, m)
        return await asyncio.gather(
            free_tier.enforce_market_view_quota(user_id, "m5"),
            free_tier.enforce_market_view_quota(user_id, "m6"),
            return_exceptions=True,
        )

    results = asyncio.run(_scenario())

    successes = [r for r in results if isinstance(r, int)]
    rejections = [r for r in results if isinstance(r, free_tier.FreeTierQuotaError)]
    assert len(successes) == 1, results
    assert len(rejections) == 1, results
    assert successes[0] == 5

    today_rows = {
        market for (uid, _, market) in fake_quota_db.rows if uid == user_id
    }
    assert len(today_rows) == 5


# ---------------------------------------------------------------------------
# Fail-closed: a quota-DB outage must NOT silently let free callers
# bypass the cap.  We surface 503 so the dashboard can show a retry
# state instead of quietly serving the Pro view.
# ---------------------------------------------------------------------------


def test_quota_check_fails_closed_when_db_unavailable(monkeypatch):
    from contextlib import asynccontextmanager
    from app import db as db_module

    @asynccontextmanager
    async def _broken_acquire():
        raise RuntimeError("postgres unreachable")
        yield  # pragma: no cover

    monkeypatch.setattr(db_module, "acquire", lambda: _broken_acquire())

    import asyncio
    from fastapi import HTTPException

    async def _go():
        return await free_tier.enforce_market_view_quota("user_x", "m-any")

    with pytest.raises(HTTPException) as ei:
        asyncio.run(_go())
    assert ei.value.status_code == 503
    assert ei.value.detail["error"] == "quota_check_unavailable"


# ---------------------------------------------------------------------------
# Trade-detail surface honours the same 15-minute delay so free callers
# can't deep-link past the gate on the list endpoints.
# ---------------------------------------------------------------------------


def test_trade_detail_filters_sub_15_minute_series_for_free_callers(
    monkeypatch, patch_subscription, override_auth, fake_quota_db
):
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(free_tier, "datetime", _FixedDatetime)

    series = [
        {"t": _iso(now - timedelta(minutes=2)),  "model_prob": 0.61, "market_price": 0.55},
        {"t": _iso(now - timedelta(minutes=14)), "model_prob": 0.60, "market_price": 0.56},
        {"t": _iso(now - timedelta(minutes=20)), "model_prob": 0.58, "market_price": 0.57},
        {"t": _iso(now - timedelta(hours=2)),    "model_prob": 0.55, "market_price": 0.58},
    ]
    _seed_trade_detail(
        monkeypatch,
        "m-detail",
        executed_at=_iso(now - timedelta(hours=3)),
        series=series,
    )
    patch_subscription["value"] = _sub(billing.TIER_FREE)

    client = TestClient(app)
    resp = client.get("/api/dashboard/trades/1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Free tier sees only the >=15-min-old points.
    times = [p["t"] for p in body["series"]]
    assert times == [
        _iso(now - timedelta(minutes=20)),
        _iso(now - timedelta(hours=2)),
    ]
    assert body["delayed"] is True
    assert body["delay_minutes"] == 15


# ---------------------------------------------------------------------------
# Dashboard landing tab — emit ``delayed``/``delay_minutes``/``tier`` so the
# mobile + web dashboards can render the upgrade banner on the very first
# screen, before the user navigates to a list endpoint.
# ---------------------------------------------------------------------------


def _stub_overview(monkeypatch) -> None:
    # ``placed_by`` was added by migration 0039 + the trade-provenance
    # filter (Manual / System / Both toggle).  This stub accepts and
    # ignores it because these free-tier tests assert the delayed-banner
    # behaviour, not the placed_by filter.
    async def _fake_overview(placed_by: str | None = None) -> dict[str, Any]:
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

    monkeypatch.setattr(dashboard, "get_overview", _fake_overview)


def test_dashboard_overview_marks_free_caller_as_delayed(
    monkeypatch, patch_subscription, override_auth
):
    _stub_overview(monkeypatch)
    patch_subscription["value"] = _sub(billing.TIER_FREE)

    client = TestClient(app)
    resp = client.get("/api/dashboard/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delayed"] is True
    assert body["delay_minutes"] == free_tier.FREE_TIER_DELAY_MINUTES
    assert body["tier"] == billing.TIER_FREE


def test_dashboard_overview_does_not_mark_pro_caller_as_delayed(
    monkeypatch, patch_subscription, override_auth
):
    _stub_overview(monkeypatch)
    patch_subscription["value"] = _sub(billing.TIER_PRO)

    client = TestClient(app)
    resp = client.get("/api/dashboard/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delayed"] is False
    assert body["delay_minutes"] == 0
    assert body["tier"] == billing.TIER_PRO


def test_trade_detail_404s_when_trade_itself_is_too_fresh_for_free(
    monkeypatch, patch_subscription, override_auth, fake_quota_db
):
    """A trade executed 30 seconds ago is sub-15-min: free callers
    must not be able to read it via the detail endpoint either."""
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(free_tier, "datetime", _FixedDatetime)
    _seed_trade_detail(
        monkeypatch,
        "m-fresh",
        executed_at=_iso(now - timedelta(seconds=30)),
    )
    patch_subscription["value"] = _sub(billing.TIER_FREE)

    client = TestClient(app)
    resp = client.get("/api/dashboard/trades/1")
    assert resp.status_code == 404, resp.text
    # Crucially, the 404 must fire *before* the quota write so a free
    # caller can't burn their daily quota on a signal they're not even
    # allowed to see yet.
    assert all(uid != "user_free" for (uid, _, _) in fake_quota_db.rows)
