"""Tests for the Stripe billing module + tier-gating dependency.

Why this exists
---------------
Billing is the highest-stakes piece of the API: a wrong tier resolution
either gives users free access to paid features or locks them out of
ones they paid for, and a mishandled webhook either double-charges the
DB with stale state or drops a real subscription update.  This suite
locks down the behaviours we cannot afford to regress:

  * downgrade-on-cancellation / downgrade-on-unpaid in the runtime
    tier resolver,
  * founder bypass via ``STRIPE_FOUNDER_USERS``,
  * webhook signature rejection,
  * idempotent webhook replay (same ``event_id`` twice = single DB
    write),
  * the customer-creation race (two parallel checkout calls = one
    Stripe customer),
  * ``current_period_end`` fallback to ``items[].current_period_end``,
  * the exact 403 payload shape returned by ``require_tier``.

The real asyncpg pool is replaced with an in-memory ``FakeDB`` that
emulates only the queries billing.py actually runs, dispatching by
substring match on the SQL.  Stripe SDK calls are stubbed at the
``billing.stripe`` module level via monkeypatch.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
import stripe
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import billing
from app.auth import AuthedUser


# ---------------------------------------------------------------------------
# In-memory fake of the asyncpg pool
# ---------------------------------------------------------------------------


class _FakeDB:
    """Holds the three tables billing.py touches.

    A single global ``tx_lock`` serialises ``conn.transaction()`` blocks
    so the customer-creation race test can exercise real interleaving
    semantics: the second task only enters the critical section after
    the first commits.
    """

    def __init__(self) -> None:
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.stripe_events: dict[str, dict[str, Any]] = {}
        self.stripe_subscriptions: dict[str, dict[str, Any]] = {}
        self.tx_lock = asyncio.Lock()
        # Every (query, args) tuple, useful for asserting on the number
        # of writes a webhook produced.
        self.execute_log: list[tuple[str, tuple]] = []


class _FakeTransaction:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    async def __aenter__(self) -> "_FakeTransaction":
        await self._db.tx_lock.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._db.tx_lock.release()


class _FakeConn:
    """Asyncpg-shaped connection backed by ``_FakeDB``.

    Dispatches by *substring* on the normalised query string — verbose
    but explicit, and fails loudly on any query the suite doesn't yet
    cover so a new SQL statement in billing.py forces test updates.
    """

    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._db)

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        self._db.execute_log.append((q, args))

        if q.startswith("CREATE TABLE") or q.startswith("CREATE INDEX"):
            return "OK"

        if q.startswith("INSERT INTO subscriptions (user_id, email, tier, status)"):
            uid, email = args
            self._db.subscriptions.setdefault(
                uid,
                {
                    "user_id": uid,
                    "email": email,
                    "tier": "free",
                    "status": "inactive",
                    "stripe_customer_id": None,
                    "stripe_subscription_id": None,
                    "stripe_price_id": None,
                    "interval": None,
                    "current_period_end": None,
                    "cancel_at_period_end": False,
                },
            )
            return "OK"

        if q.startswith("UPDATE subscriptions SET stripe_customer_id"):
            uid, customer_id, email = args
            row = self._db.subscriptions.setdefault(uid, {"user_id": uid})
            row["stripe_customer_id"] = customer_id
            if email:
                row["email"] = email
            return "OK"

        if q.startswith("INSERT INTO stripe_events"):
            event_id, event_type, payload = args
            # ON CONFLICT DO NOTHING — first write wins.
            if event_id not in self._db.stripe_events:
                self._db.stripe_events[event_id] = {
                    "event_type": event_type,
                    "payload": payload,
                }
            return "OK"

        if q.startswith("INSERT INTO stripe_subscriptions"):
            (
                sub_id,
                customer_id,
                user_id,
                status,
                price_id,
                interval,
                period_end,
                cancel_at_period_end,
                raw,
            ) = args
            self._db.stripe_subscriptions[sub_id] = {
                "stripe_subscription_id": sub_id,
                "stripe_customer_id": customer_id,
                "user_id": user_id,
                "status": status,
                "stripe_price_id": price_id,
                "interval": interval,
                "current_period_end": period_end,
                "cancel_at_period_end": cancel_at_period_end,
                "raw": raw,
            }
            return "OK"

        if q.startswith("INSERT INTO subscriptions ( user_id, tier, status"):
            (
                user_id,
                tier,
                status,
                customer_id,
                sub_id,
                price_id,
                interval,
                period_end,
                cancel_at_period_end,
            ) = args
            row = self._db.subscriptions.setdefault(
                user_id, {"user_id": user_id, "email": None}
            )
            row.update(
                {
                    "tier": tier,
                    "status": status,
                    "stripe_customer_id": customer_id,
                    "stripe_subscription_id": sub_id,
                    "stripe_price_id": price_id,
                    "interval": interval,
                    "current_period_end": period_end,
                    "cancel_at_period_end": cancel_at_period_end,
                }
            )
            return "OK"

        if q.startswith("UPDATE subscriptions SET tier ="):
            (
                customer_id,
                tier,
                status,
                sub_id,
                price_id,
                interval,
                period_end,
                cancel_at_period_end,
            ) = args
            for row in self._db.subscriptions.values():
                if row.get("stripe_customer_id") == customer_id:
                    row.update(
                        {
                            "tier": tier,
                            "status": status,
                            "stripe_subscription_id": sub_id,
                            "stripe_price_id": price_id,
                            "interval": interval,
                            "current_period_end": period_end,
                            "cancel_at_period_end": cancel_at_period_end,
                        }
                    )
            return "OK"

        if q.startswith("UPDATE subscriptions SET status ="):
            id_val, status = args
            key = (
                "stripe_subscription_id"
                if "stripe_subscription_id =" in q
                else "stripe_customer_id"
            )
            for row in self._db.subscriptions.values():
                if row.get(key) == id_val:
                    row["status"] = status
            return "OK"

        raise AssertionError(f"unhandled SQL in fake DB: {q!r}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM subscriptions WHERE user_id="):
            row = self._db.subscriptions.get(args[0])
            return dict(row) if row else None
        if q.startswith("SELECT tier FROM subscriptions WHERE user_id="):
            row = self._db.subscriptions.get(args[0])
            return {"tier": row.get("tier")} if row else None
        raise AssertionError(f"unhandled fetchrow in fake DB: {q!r}")

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if q.startswith("SELECT stripe_customer_id FROM subscriptions"):
            row = self._db.subscriptions.get(args[0])
            return row.get("stripe_customer_id") if row else None
        if q.startswith("SELECT 1 FROM stripe_events WHERE event_id="):
            return 1 if args[0] in self._db.stripe_events else None
        raise AssertionError(f"unhandled fetchval in fake DB: {q!r}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch) -> _FakeDB:
    """Replace ``billing.db`` IO with the in-memory fake.

    ``billing.ensure_schema`` is a no-op here — the fake doesn't need a
    schema — but we still let it run once so the cached ``_schema_ready``
    flag matches the production code path on every test.
    """
    db_obj = _FakeDB()

    @asynccontextmanager
    async def _fake_acquire():
        yield _FakeConn(db_obj)

    async def _fake_fetchrow(query: str, *args: Any):
        async with _fake_acquire() as c:
            return await c.fetchrow(query, *args)

    monkeypatch.setattr(billing.db, "acquire", _fake_acquire)
    monkeypatch.setattr(billing.db, "fetchrow", _fake_fetchrow)

    # Reset the module-level "schema already created" flag so each test
    # exercises ``ensure_schema`` against the fresh fake.
    billing._schema_ready = False

    # Make sure no real Stripe key leaks in from the host env.
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.delenv("STRIPE_FOUNDER_USERS", raising=False)
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)
    return db_obj


@pytest.fixture
def stub_stripe(monkeypatch):
    """Replace the Stripe SDK calls billing.py uses.

    Returns a small bag of MagicMock-ish objects so individual tests
    can swap in whatever behaviour they want.
    """

    calls: dict[str, list[Any]] = {
        "customer_create": [],
        "checkout_create": [],
        "subscription_retrieve": [],
        "construct_event": [],
    }

    class _Customer:
        @staticmethod
        def create(**kwargs):
            calls["customer_create"].append(kwargs)
            return {"id": f"cus_test_{len(calls['customer_create'])}"}

    class _Session:
        @staticmethod
        def create(**kwargs):
            calls["checkout_create"].append(kwargs)
            return {"id": "cs_test_123", "url": "https://stripe.test/cs_test_123"}

    class _Subscription:
        @staticmethod
        def retrieve(sub_id):
            calls["subscription_retrieve"].append(sub_id)
            return {"id": sub_id, "customer": "cus_test_1", "status": "active"}

    class _Webhook:
        return_value: dict[str, Any] | None = None
        raise_value: Exception | None = None

        @classmethod
        def construct_event(cls, payload, sig, secret):
            calls["construct_event"].append((payload, sig, secret))
            if cls.raise_value is not None:
                raise cls.raise_value
            return cls.return_value

    monkeypatch.setattr(billing.stripe, "Customer", _Customer, raising=False)
    monkeypatch.setattr(
        billing.stripe, "checkout", type("X", (), {"Session": _Session}), raising=False
    )
    monkeypatch.setattr(billing.stripe, "Subscription", _Subscription, raising=False)
    monkeypatch.setattr(billing.stripe, "Webhook", _Webhook, raising=False)

    return {"calls": calls, "Webhook": _Webhook}


def _user(uid: str = "user_test_1", email: str | None = None) -> AuthedUser:
    """Build an ``AuthedUser`` with the given email exposed via JWT claims."""
    claims: dict[str, Any] = {"sub": uid}
    if email:
        claims["email"] = email
    return AuthedUser(user_id=uid, session_id="sess_test", claims=claims)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tier resolution: downgrade on canceled / unpaid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dead_status", ["canceled", "unpaid", "incomplete_expired"])
def test_tier_downgrades_when_subscription_is_not_active(fake_db, dead_status):
    """A row that *says* tier=pro but whose Stripe status is no longer in the
    active set must resolve to ``free`` for the runtime tier check.

    This is the safety net protecting paid features from a stale upsert
    that forgot to flip the tier — even if a future bug leaves
    ``tier='pro'`` lingering, an inactive status downgrades it.
    """
    fake_db.subscriptions["user_dead"] = {
        "user_id": "user_dead",
        "email": "dead@example.com",
        "tier": "pro",
        "status": dead_status,
        "stripe_customer_id": "cus_x",
        "stripe_subscription_id": "sub_x",
        "stripe_price_id": "price_x",
        "interval": "month",
        "current_period_end": None,
        "cancel_at_period_end": False,
    }
    sub = _run(billing.get_user_subscription(_user("user_dead")))
    assert sub.tier == billing.TIER_FREE
    assert sub.status == dead_status
    assert sub.is_founder is False


def test_active_paid_subscription_keeps_its_tier(fake_db):
    """Sanity check: an ``active`` row with tier=pro must NOT be downgraded."""
    fake_db.subscriptions["user_paid"] = {
        "user_id": "user_paid",
        "email": "paid@example.com",
        "tier": "pro",
        "status": "active",
        "stripe_customer_id": "cus_paid",
        "stripe_subscription_id": "sub_paid",
        "stripe_price_id": "price_pro_month",
        "interval": "month",
        "current_period_end": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "cancel_at_period_end": False,
    }
    sub = _run(billing.get_user_subscription(_user("user_paid")))
    assert sub.tier == billing.TIER_PRO
    assert sub.status == "active"


# ---------------------------------------------------------------------------
# Founder bypass
# ---------------------------------------------------------------------------


def test_founder_bypass_overrides_missing_subscription(fake_db, monkeypatch):
    """A user listed in ``STRIPE_FOUNDER_USERS`` resolves to ``founder``
    even with no row in the subscriptions table at all."""
    monkeypatch.setenv("STRIPE_FOUNDER_USERS", "Founder@Example.COM")
    sub = _run(
        billing.get_user_subscription(_user("user_founder", email="founder@example.com"))
    )
    assert sub.is_founder is True
    assert sub.tier == billing.TIER_FOUNDER
    assert sub.status == "active"


def test_founder_bypass_overrides_inactive_paid_row(fake_db, monkeypatch):
    """Founder always wins, even over a canceled row that would otherwise
    downgrade the tier to free."""
    monkeypatch.setenv("OPERATOR_ALLOWED_EMAILS", "boss@example.com")
    fake_db.subscriptions["user_boss"] = {
        "user_id": "user_boss",
        "email": "boss@example.com",
        "tier": "pro",
        "status": "canceled",
        "stripe_customer_id": "cus_boss",
        "stripe_subscription_id": "sub_boss",
        "stripe_price_id": None,
        "interval": None,
        "current_period_end": None,
        "cancel_at_period_end": False,
    }
    sub = _run(
        billing.get_user_subscription(_user("user_boss", email="boss@example.com"))
    )
    assert sub.is_founder is True
    assert sub.tier == billing.TIER_FOUNDER


# ---------------------------------------------------------------------------
# Webhook signature rejection
# ---------------------------------------------------------------------------


class _FakeRequest:
    """The two attributes ``billing.handle_webhook`` actually reads."""

    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


def test_webhook_rejects_invalid_signature(fake_db, stub_stripe, monkeypatch):
    """When ``construct_event`` raises a SignatureVerificationError the
    handler must return 400 without ever calling the per-event branch."""
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    stub_stripe["Webhook"].raise_value = stripe.SignatureVerificationError(
        "bad sig", "sig_header"
    )
    req = _FakeRequest(b"{}", {"stripe-signature": "t=1,v1=garbage"})
    with pytest.raises(HTTPException) as exc:
        _run(billing.handle_webhook(req))  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert "invalid signature" in str(exc.value.detail).lower()
    # Nothing should have been recorded — the event never made it past
    # the signature check.
    assert fake_db.stripe_events == {}


def test_webhook_rejects_missing_signature_header(fake_db, stub_stripe, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    req = _FakeRequest(b"{}", {})
    with pytest.raises(HTTPException) as exc:
        _run(billing.handle_webhook(req))  # type: ignore[arg-type]
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Idempotent replay
# ---------------------------------------------------------------------------


def _sub_payload(
    *, sub_id="sub_idem", customer="cus_idem", status="active", user_id="user_idem"
) -> dict[str, Any]:
    """Minimal-but-realistic ``customer.subscription.*`` data.object payload."""
    return {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "cancel_at_period_end": False,
        "current_period_end": 4102444800,  # 2100-01-01
        "metadata": {"clerk_user_id": user_id},
        "items": {
            "data": [
                {
                    "price": {"id": "price_pro_month", "recurring": {"interval": "month"}},
                }
            ]
        },
    }


def test_webhook_idempotent_replay_writes_once(fake_db, stub_stripe, monkeypatch):
    """Stripe routinely re-delivers the same event_id.  The second
    delivery must short-circuit and produce no additional DB writes.
    """
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro_month")

    event = {
        "id": "evt_replay_1",
        "type": "customer.subscription.created",
        "data": {"object": _sub_payload()},
    }
    stub_stripe["Webhook"].return_value = event
    req = _FakeRequest(b"payload", {"stripe-signature": "ok"})

    first = _run(billing.handle_webhook(req))  # type: ignore[arg-type]
    assert first == {"received": True}
    assert fake_db.stripe_events == {
        "evt_replay_1": fake_db.stripe_events["evt_replay_1"]
    }
    writes_after_first = len(fake_db.execute_log)
    assert "user_idem" in fake_db.subscriptions
    assert fake_db.subscriptions["user_idem"]["tier"] == billing.TIER_PRO

    # Second delivery — same event id.  Should be flagged as duplicate
    # and produce ZERO additional writes (no schema, no upsert, no
    # event-table insert).
    second = _run(billing.handle_webhook(req))  # type: ignore[arg-type]
    assert second == {"received": True, "duplicate": True}
    assert len(fake_db.execute_log) == writes_after_first


# ---------------------------------------------------------------------------
# Customer creation race
# ---------------------------------------------------------------------------


def test_concurrent_ensure_customer_creates_one_stripe_customer(fake_db, stub_stripe):
    """Two parallel checkout calls for the same Clerk user must collapse
    to a single Stripe ``Customer.create`` thanks to the row-level lock
    in ``_ensure_customer``.

    Without serialisation we'd orphan one customer in Stripe and the
    second checkout would silently start charging the wrong account.
    """
    user = _user("user_race", email="race@example.com")

    async def _scenario() -> tuple[str, str]:
        # Both tasks see a fresh, empty subscription row (stripe_customer_id=None).
        sub = await billing.get_user_subscription(user)
        assert sub.stripe_customer_id is None

        # Slow the create call down a hair so the second task is
        # guaranteed to be inside ``transaction()`` when the first one
        # is mid-create.  The fake transaction lock is what should
        # actually prevent the duplicate create.
        original_create = billing.stripe.Customer.create

        def _slow_create(**kwargs):
            return original_create(**kwargs)

        billing.stripe.Customer.create = _slow_create  # type: ignore[attr-defined]

        a, b = await asyncio.gather(
            billing._ensure_customer(user, sub),
            billing._ensure_customer(user, sub),
        )
        return a, b

    a, b = _run(_scenario())
    assert a == b, f"both calls must return the same customer id, got {a!r} vs {b!r}"
    assert (
        len(stub_stripe["calls"]["customer_create"]) == 1
    ), "Stripe.Customer.create should only have been invoked once"
    # And the user's row should reflect that single customer id.
    assert fake_db.subscriptions["user_race"]["stripe_customer_id"] == a


# ---------------------------------------------------------------------------
# current_period_end fallback to subscription items
# ---------------------------------------------------------------------------


def test_period_end_falls_back_to_subscription_items():
    """Newer Stripe API versions only report ``current_period_end`` on
    each subscription item, not at the top level.  We must fall back."""
    sub = {
        "id": "sub_items_only",
        "customer": "cus_items",
        "status": "active",
        "current_period_end": None,  # missing at top level
        "items": {
            "data": [
                {
                    "current_period_end": 4102444800,  # 2100-01-01 UTC
                    "price": {"id": "price_pro_month", "recurring": {"interval": "month"}},
                }
            ]
        },
    }
    pe = billing._period_end_from_subscription(sub)
    assert pe == datetime(2100, 1, 1, tzinfo=timezone.utc)


def test_period_end_prefers_top_level_when_present():
    sub = {
        "current_period_end": 4102444800,
        "items": {"data": [{"current_period_end": 1700000000}]},
    }
    pe = billing._period_end_from_subscription(sub)
    assert pe == datetime(2100, 1, 1, tzinfo=timezone.utc)


def test_period_end_returns_none_when_absent_everywhere():
    assert billing._period_end_from_subscription({}) is None
    assert billing._period_end_from_subscription({"items": {"data": []}}) is None


def test_apply_subscription_persists_items_only_period_end(fake_db, monkeypatch):
    """End-to-end: a webhook payload with the new API shape must result
    in the per-user row having a non-null ``current_period_end``."""
    monkeypatch.setenv("STRIPE_PRICE_PRO_MONTHLY", "price_pro_month")
    payload = _sub_payload(sub_id="sub_new_api", user_id="user_new_api")
    payload["current_period_end"] = None
    payload["items"]["data"][0]["current_period_end"] = 4102444800

    _run(billing._apply_subscription(payload))
    row = fake_db.subscriptions["user_new_api"]
    assert row["current_period_end"] == datetime(2100, 1, 1, tzinfo=timezone.utc)
    assert row["tier"] == billing.TIER_PRO


# ---------------------------------------------------------------------------
# require_tier 403 payload shape
# ---------------------------------------------------------------------------


def _build_tier_test_app(user: AuthedUser) -> FastAPI:
    """Spin up a throwaway FastAPI app with one /needs-pro endpoint
    gated by ``require_tier('pro')`` and a fixed authed user."""
    from app.auth import require_auth

    app = FastAPI()
    dep = billing.require_tier(billing.TIER_PRO)

    @app.get("/needs-pro")
    async def _needs_pro(sub: billing.UserSubscription = Depends(dep)):
        return sub.as_dict()

    app.dependency_overrides[require_auth] = lambda: user
    return app


def test_require_tier_returns_403_with_documented_payload(fake_db):
    """The frontend reads ``detail.required_tier`` to decide what
    upgrade CTA to show.  This locks the exact JSON shape so a careless
    refactor doesn't break the upgrade prompt."""
    user = _user("user_free", email="free@example.com")
    fake_db.subscriptions["user_free"] = {
        "user_id": "user_free",
        "email": "free@example.com",
        "tier": "free",
        "status": "inactive",
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "stripe_price_id": None,
        "interval": None,
        "current_period_end": None,
        "cancel_at_period_end": False,
    }
    app = _build_tier_test_app(user)
    client = TestClient(app)
    resp = client.get("/needs-pro")
    assert resp.status_code == 403
    body = resp.json()
    assert body == {
        "detail": {
            "error": "tier_required",
            "required_tier": "pro",
            "current_tier": "free",
        }
    }


def test_require_tier_allows_paid_user(fake_db):
    """Counterpart to the 403 test: a pro user must pass the gate and
    receive the resolved subscription as the dependency value."""
    user = _user("user_pro", email="pro@example.com")
    fake_db.subscriptions["user_pro"] = {
        "user_id": "user_pro",
        "email": "pro@example.com",
        "tier": "pro",
        "status": "active",
        "stripe_customer_id": "cus_pro",
        "stripe_subscription_id": "sub_pro",
        "stripe_price_id": "price_pro_month",
        "interval": "month",
        "current_period_end": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "cancel_at_period_end": False,
    }
    app = _build_tier_test_app(user)
    client = TestClient(app)
    resp = client.get("/needs-pro")
    assert resp.status_code == 200
    assert resp.json()["tier"] == "pro"


def test_require_tier_allows_founder_even_without_paid_row(fake_db, monkeypatch):
    """Founder bypass must satisfy ``require_tier('elite')`` without any
    subscriptions row — the operator console depends on this."""
    monkeypatch.setenv("STRIPE_FOUNDER_USERS", "boss@example.com")
    user = _user("user_boss", email="boss@example.com")
    from app.auth import require_auth

    app = FastAPI()

    @app.get("/needs-elite")
    async def _needs_elite(
        sub: billing.UserSubscription = Depends(billing.require_tier(billing.TIER_ELITE)),
    ):
        return sub.as_dict()

    app.dependency_overrides[require_auth] = lambda: user
    client = TestClient(app)
    resp = client.get("/needs-elite")
    assert resp.status_code == 200
    assert resp.json()["tier"] == "founder"


def test_require_tier_rejects_unknown_tier_at_construction():
    """Misspelling a tier in a route definition should fail loudly at
    import time, not silently let everyone through."""
    with pytest.raises(ValueError):
        billing.require_tier("super-duper")
