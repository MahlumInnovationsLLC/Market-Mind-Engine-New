"""Tests for the RevenueCat webhook integration (``app.rc_billing``).

Why this exists
---------------
The Expo mobile app drives in-app purchases via RevenueCat.  RC only
talks to the api-server over its webhook, which means every paid mobile
upgrade — and every renewal, cancellation, restore and refund — flows
through :func:`app.rc_billing.handle_webhook` and lands in the same
``subscriptions`` table the rest of the app reads.

Genuine end-to-end purchase / restore tests against the App Store and
Play Store sandboxes have to run on real iOS / Android devices behind
TestFlight + Internal Testing — see ``artifacts/mobile/docs/REVENUECAT_SANDBOX_QA.md``
for that runbook.  This suite covers everything we *can* exercise from
inside Replit: the entitlement-plumbing path the webhook drives.

What's locked down here
-----------------------
* Auth header gating: missing config and wrong header both refuse.
* Malformed payloads (bad JSON, missing ``event``) return 400.
* INITIAL_PURCHASE grants ``pro`` (and ``elite`` from the elite
  entitlement) with the right status, interval, expiration and a
  ``rc:``-prefixed subscription id so the mobile UI suppresses the
  Stripe portal link.
* RENEWAL refreshes the period end without flipping the tier.
* PRODUCT_CHANGE upgrades pro → elite.
* CANCELLATION with a future expiration keeps the user paid and only
  flips ``cancel_at_period_end``.
* CANCELLATION + EXPIRATION with no entitlement downgrades to free.
* BILLING_ISSUE keeps the tier alive in ``past_due`` so a transient
  card decline doesn't lock the user out mid-cycle.
* TRANSFER applies entitlements to the new ``app_user_id`` — this is
  the path RC uses for the restore-purchases / sign-out-sign-in flow
  the device-only tests can't cover here.
* Idempotent replay: same ``event_id`` twice = single derived write.
* End-to-end through ``/api/me/subscription``: an INITIAL_PURCHASE
  webhook for a Clerk user id results in ``tier=pro`` on the GET that
  the mobile subscription screen renders.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import billing, rc_billing
from app.auth import AuthedUser


# ---------------------------------------------------------------------------
# In-memory fake of the asyncpg pool
# ---------------------------------------------------------------------------


class _FakeDB:
    """Holds the three tables ``rc_billing`` touches.

    A small dispatching ``_FakeConn`` runs queries by substring match so
    a new SQL statement in ``rc_billing.py`` fails loudly here rather
    than silently mutating production behaviour.
    """

    def __init__(self) -> None:
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.rc_subscriptions: dict[str, dict[str, Any]] = {}
        self.rc_events: dict[str, dict[str, Any]] = {}
        self.deliveries: list[dict[str, Any]] = []
        self.execute_log: list[tuple[str, tuple]] = []


class _FakeTx:
    async def __aenter__(self) -> "_FakeTx":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def transaction(self) -> _FakeTx:
        return _FakeTx()

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        self._db.execute_log.append((q, args))

        if q.startswith("CREATE TABLE") or q.startswith("CREATE INDEX"):
            return "OK"

        if q.startswith("INSERT INTO rc_subscriptions"):
            (
                key,
                app_user_id,
                user_id,
                product_id,
                entitlement,
                tier,
                event_type,
                event_id,
                store,
                expires_at,
                raw,
            ) = args
            self._db.rc_subscriptions[key] = {
                "key": key,
                "app_user_id": app_user_id,
                "user_id": user_id,
                "product_id": product_id,
                "entitlement": entitlement,
                "tier": tier,
                "event_type": event_type,
                "event_id": event_id,
                "store": store,
                "expires_at": expires_at,
                "raw": raw,
            }
            return "OK"

        if q.startswith("INSERT INTO rc_webhook_deliveries"):
            # Audit-only table — capture into self._db.deliveries so the
            # retry/list-failed-events helpers can be exercised end to end.
            status_bucket, http_status, event_id, event_type, error = args
            self._db.deliveries.append(
                {
                    "status": status_bucket,
                    "http_status": http_status,
                    "event_id": event_id,
                    "event_type": event_type,
                    "error": error,
                    "received_at": datetime.now(tz=timezone.utc)
                    + timedelta(microseconds=len(self._db.deliveries)),
                }
            )
            return "OK"

        if q.startswith("INSERT INTO rc_events"):
            event_id, event_type, payload = args
            self._db.rc_events.setdefault(
                event_id, {"event_type": event_type, "payload": payload}
            )
            return "OK"

        if q.startswith(
            "INSERT INTO subscriptions ( user_id, tier, status,"
        ):
            # rc_billing path: 8 args (stripe_customer_id is hard-coded NULL).
            (
                user_id,
                tier,
                status,
                sub_id,
                price_id,
                interval,
                period_end,
                cancel_at_period_end,
            ) = args
            existing = self._db.subscriptions.get(user_id, {})
            existing_interval = existing.get("interval")
            row = self._db.subscriptions.setdefault(
                user_id, {"user_id": user_id, "email": None}
            )
            row.update(
                {
                    "tier": tier,
                    "status": status,
                    "stripe_customer_id": None,
                    "stripe_subscription_id": sub_id,
                    "stripe_price_id": price_id,
                    # COALESCE(EXCLUDED.interval, subscriptions.interval)
                    "interval": interval if interval is not None else existing_interval,
                    "current_period_end": period_end,
                    "cancel_at_period_end": cancel_at_period_end,
                }
            )
            return "OK"

        raise AssertionError(f"unhandled SQL in fake DB: {q!r}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split())
        if q.startswith("SELECT * FROM subscriptions WHERE user_id="):
            row = self._db.subscriptions.get(args[0])
            return dict(row) if row else None
        if q.startswith("SELECT raw, user_id FROM rc_subscriptions WHERE event_id"):
            (event_id,) = args
            for row in self._db.rc_subscriptions.values():
                if row.get("event_id") == event_id:
                    return {"raw": row.get("raw"), "user_id": row.get("user_id")}
            return None
        raise AssertionError(f"unhandled fetchrow in fake DB: {q!r}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        if q.startswith("WITH per_event AS"):
            # list_failed_events — synthesise from the in-memory delivery
            # audit rows the test set up via insert_delivery().
            limit = args[1] if len(args) >= 2 else 20
            failed_status = args[0]
            latest: dict[str, dict[str, Any]] = {}
            attempts: dict[str, int] = {}
            for d in getattr(self._db, "deliveries", []):
                eid = d.get("event_id")
                if not eid:
                    continue
                attempts[eid] = attempts.get(eid, 0) + 1
                cur = latest.get(eid)
                if cur is None or d["received_at"] > cur["received_at"]:
                    latest[eid] = d
            out: list[dict[str, Any]] = []
            for eid, d in latest.items():
                if d["status"] != failed_status:
                    continue
                raw_row = None
                for r in self._db.rc_subscriptions.values():
                    if r.get("event_id") == eid:
                        raw_row = r
                        break
                out.append(
                    {
                        "event_id": eid,
                        "event_type": d.get("event_type"),
                        "last_failed_at": d["received_at"],
                        "attempts": attempts[eid],
                        "last_error": d.get("error"),
                        "raw_available": raw_row is not None
                        and raw_row.get("raw") is not None,
                        "user_id": raw_row.get("user_id") if raw_row else None,
                        "app_user_id": raw_row.get("app_user_id") if raw_row else None,
                    }
                )
            out.sort(key=lambda r: r["last_failed_at"], reverse=True)
            return out[: int(limit)]
        raise AssertionError(f"unhandled fetch in fake DB: {q!r}")

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if q.startswith("SELECT 1 FROM rc_events WHERE event_id="):
            return 1 if args[0] in self._db.rc_events else None
        raise AssertionError(f"unhandled fetchval in fake DB: {q!r}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch) -> _FakeDB:
    """Wire ``rc_billing.db`` and ``billing.db`` to the in-memory fake."""
    db_obj = _FakeDB()

    @asynccontextmanager
    async def _fake_acquire():
        yield _FakeConn(db_obj)

    async def _fake_fetchrow(query: str, *args: Any):
        async with _fake_acquire() as c:
            return await c.fetchrow(query, *args)

    monkeypatch.setattr(rc_billing.db, "acquire", _fake_acquire)
    monkeypatch.setattr(billing.db, "acquire", _fake_acquire)
    monkeypatch.setattr(billing.db, "fetchrow", _fake_fetchrow)

    rc_billing._schema_ready = False
    billing._schema_ready = False

    monkeypatch.setenv("REVENUECAT_WEBHOOK_AUTH_HEADER", "Bearer rc-test-secret")
    monkeypatch.delenv("STRIPE_FOUNDER_USERS", raising=False)
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)
    return db_obj


class _FakeRequest:
    """Just the two attributes ``handle_webhook`` reads."""

    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def json(self) -> Any:
        return json.loads(self._body.decode("utf-8"))


def _post(payload: dict[str, Any], *, auth: str = "Bearer rc-test-secret") -> _FakeRequest:
    return _FakeRequest(
        json.dumps(payload).encode("utf-8"),
        {"authorization": auth},
    )


def _run(coro):
    return asyncio.run(coro)


def _future_ms(days: int = 30) -> int:
    return int(
        (datetime.now(tz=timezone.utc) + timedelta(days=days)).timestamp() * 1000
    )


def _past_ms(days: int = 1) -> int:
    return int(
        (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000
    )


def _evt(
    *,
    event_id: str,
    event_type: str,
    app_user_id: str,
    product_id: str | None = "pro_monthly",
    entitlements: list[str] | None = ("pro",),
    expiration_ms: int | None = None,
    store: str = "APP_STORE",
) -> dict[str, Any]:
    """Build a webhook payload that mirrors the RC v1 event shape."""
    inner: dict[str, Any] = {
        "id": event_id,
        "type": event_type,
        "app_user_id": app_user_id,
        "original_app_user_id": app_user_id,
        "product_id": product_id,
        "store": store,
    }
    if entitlements is not None:
        inner["entitlement_ids"] = list(entitlements)
    if expiration_ms is not None:
        inner["expiration_at_ms"] = expiration_ms
    return {"event": inner}


# ---------------------------------------------------------------------------
# Auth header gating
# ---------------------------------------------------------------------------


def test_webhook_refuses_when_secret_unset(fake_db, monkeypatch):
    """Unset secret = endpoint refuses every delivery — the docstring
    promises this so an attacker can't forge tier upgrades against any
    Clerk user id by guessing the URL."""
    monkeypatch.delenv("REVENUECAT_WEBHOOK_AUTH_HEADER", raising=False)
    req = _post(_evt(event_id="e1", event_type="INITIAL_PURCHASE", app_user_id="u1"))
    with pytest.raises(Exception) as exc:
        _run(rc_billing.handle_webhook(req))  # type: ignore[arg-type]
    assert getattr(exc.value, "status_code", None) == 503
    assert fake_db.subscriptions == {}


def test_webhook_rejects_wrong_auth_header(fake_db):
    req = _post(
        _evt(event_id="e1", event_type="INITIAL_PURCHASE", app_user_id="u1"),
        auth="Bearer wrong",
    )
    with pytest.raises(Exception) as exc:
        _run(rc_billing.handle_webhook(req))  # type: ignore[arg-type]
    assert getattr(exc.value, "status_code", None) == 401
    assert fake_db.subscriptions == {}


def test_webhook_rejects_invalid_json(fake_db):
    req = _FakeRequest(b"not-json", {"authorization": "Bearer rc-test-secret"})
    with pytest.raises(Exception) as exc:
        _run(rc_billing.handle_webhook(req))  # type: ignore[arg-type]
    assert getattr(exc.value, "status_code", None) == 400


def test_webhook_rejects_missing_event_block(fake_db):
    req = _post({"not_event": {}})
    with pytest.raises(Exception) as exc:
        _run(rc_billing.handle_webhook(req))  # type: ignore[arg-type]
    assert getattr(exc.value, "status_code", None) == 400


# ---------------------------------------------------------------------------
# Initial purchase — the path every new mobile subscriber walks
# ---------------------------------------------------------------------------


def test_initial_purchase_grants_pro_tier_with_rc_subscription_id(fake_db):
    """The mobile subscription screen hides the Stripe ``Manage`` button
    when the stored subscription id starts with ``rc:`` — locking that
    prefix in here so the UI keeps doing the right thing."""
    expires = _future_ms(30)
    req = _post(
        _evt(
            event_id="evt_init_pro",
            event_type="INITIAL_PURCHASE",
            app_user_id="user_clerk_pro",
            product_id="pro_monthly",
            entitlements=["pro"],
            expiration_ms=expires,
        )
    )
    res = _run(rc_billing.handle_webhook(req))  # type: ignore[arg-type]
    assert res == {"status": "ok", "event_id": "evt_init_pro"}

    row = fake_db.subscriptions["user_clerk_pro"]
    assert row["tier"] == billing.TIER_PRO
    assert row["status"] == "active"
    assert row["interval"] == "month"
    assert row["cancel_at_period_end"] is False
    assert row["stripe_customer_id"] is None
    assert row["stripe_subscription_id"].startswith("rc:")
    assert row["stripe_price_id"] == "revenuecat:pro_monthly"
    assert row["current_period_end"] == datetime.fromtimestamp(
        expires / 1000.0, tz=timezone.utc
    )
    # And the raw mirror also got the event.
    rc_row = next(iter(fake_db.rc_subscriptions.values()))
    assert rc_row["entitlement"] == "pro"
    assert rc_row["event_type"] == "INITIAL_PURCHASE"
    # Idempotency table is populated only after the apply succeeds.
    assert "evt_init_pro" in fake_db.rc_events


def test_initial_purchase_elite_entitlement_grants_elite_tier(fake_db):
    req = _post(
        _evt(
            event_id="evt_init_elite",
            event_type="INITIAL_PURCHASE",
            app_user_id="user_elite",
            product_id="elite_annual",
            entitlements=["elite"],
            expiration_ms=_future_ms(365),
        )
    )
    _run(rc_billing.handle_webhook(req))  # type: ignore[arg-type]
    row = fake_db.subscriptions["user_elite"]
    assert row["tier"] == billing.TIER_ELITE
    assert row["interval"] == "year"


def test_renewal_refreshes_period_end_and_keeps_tier(fake_db):
    first_exp = _future_ms(5)
    second_exp = _future_ms(35)

    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_init",
                    event_type="INITIAL_PURCHASE",
                    app_user_id="user_renew",
                    expiration_ms=first_exp,
                )
            )
        )
    )
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_renew",
                    event_type="RENEWAL",
                    app_user_id="user_renew",
                    expiration_ms=second_exp,
                )
            )
        )
    )
    row = fake_db.subscriptions["user_renew"]
    assert row["tier"] == billing.TIER_PRO
    assert row["status"] == "active"
    assert row["current_period_end"] == datetime.fromtimestamp(
        second_exp / 1000.0, tz=timezone.utc
    )


def test_product_change_upgrades_pro_to_elite(fake_db):
    """Upgrading mid-cycle in the App Store fires a PRODUCT_CHANGE; the
    derived row must follow the new entitlement."""
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_init",
                    event_type="INITIAL_PURCHASE",
                    app_user_id="user_up",
                    product_id="pro_monthly",
                    entitlements=["pro"],
                    expiration_ms=_future_ms(15),
                )
            )
        )
    )
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_change",
                    event_type="PRODUCT_CHANGE",
                    app_user_id="user_up",
                    product_id="elite_monthly",
                    entitlements=["elite"],
                    expiration_ms=_future_ms(30),
                )
            )
        )
    )
    row = fake_db.subscriptions["user_up"]
    assert row["tier"] == billing.TIER_ELITE
    assert row["stripe_price_id"] == "revenuecat:elite_monthly"


# ---------------------------------------------------------------------------
# Cancellation, expiration, billing issues
# ---------------------------------------------------------------------------


def test_cancellation_with_future_expiration_keeps_access(fake_db):
    """Auto-renew off mid-cycle: tier stays paid and ``cancel_at_period_end``
    flips to True so the mobile UI can show the right copy."""
    expires = _future_ms(20)
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_init",
                    event_type="INITIAL_PURCHASE",
                    app_user_id="user_cancel",
                    expiration_ms=expires,
                )
            )
        )
    )
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_cancel",
                    event_type="CANCELLATION",
                    app_user_id="user_cancel",
                    expiration_ms=expires,
                )
            )
        )
    )
    row = fake_db.subscriptions["user_cancel"]
    assert row["tier"] == billing.TIER_PRO
    assert row["status"] == "active"
    assert row["cancel_at_period_end"] is True


def test_cancellation_after_expiration_downgrades_to_free(fake_db):
    """If RC sends CANCELLATION with no remaining entitlement and no
    future expiration, the user goes back to free immediately."""
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_init",
                    event_type="INITIAL_PURCHASE",
                    app_user_id="user_dead",
                    expiration_ms=_future_ms(10),
                )
            )
        )
    )
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_cancel",
                    event_type="CANCELLATION",
                    app_user_id="user_dead",
                    entitlements=[],
                    expiration_ms=_past_ms(1),
                )
            )
        )
    )
    row = fake_db.subscriptions["user_dead"]
    assert row["tier"] == billing.TIER_FREE
    assert row["status"] == "canceled"


def test_expiration_event_revokes_access(fake_db):
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_init",
                    event_type="INITIAL_PURCHASE",
                    app_user_id="user_exp",
                    expiration_ms=_future_ms(2),
                )
            )
        )
    )
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_exp",
                    event_type="EXPIRATION",
                    app_user_id="user_exp",
                    entitlements=[],
                    expiration_ms=_past_ms(1),
                )
            )
        )
    )
    row = fake_db.subscriptions["user_exp"]
    assert row["tier"] == billing.TIER_FREE
    assert row["status"] == "inactive"


def test_billing_issue_keeps_tier_in_past_due(fake_db):
    """A transient card decline on renewal must not kick a paying
    subscriber out of paid features — RC's grace period mirrors
    Stripe's ``past_due`` semantics."""
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_init",
                    event_type="INITIAL_PURCHASE",
                    app_user_id="user_grace",
                    expiration_ms=_future_ms(7),
                )
            )
        )
    )
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_billing",
                    event_type="BILLING_ISSUE",
                    app_user_id="user_grace",
                    entitlements=["pro"],
                    expiration_ms=_future_ms(7),
                )
            )
        )
    )
    row = fake_db.subscriptions["user_grace"]
    assert row["status"] == "past_due"
    assert row["tier"] == billing.TIER_PRO


# ---------------------------------------------------------------------------
# Restore / sign-out-sign-in surrogate (TRANSFER event)
# ---------------------------------------------------------------------------


def test_transfer_event_grants_entitlement_to_new_app_user_id(fake_db):
    """The on-device flow:

      1. User A buys pro on iOS.
      2. They sign out (RC ``logOut``) and sign back in as user B —
         the same Apple ID is now linked to a new Clerk user id.
      3. RC fires a TRANSFER event tagging the new ``app_user_id``.

    From the api-server's perspective the only observable signal is the
    TRANSFER webhook, which must grant the entitlement to the new
    Clerk user id.  This is the closest thing to the device-only
    restore-purchases test we can run inside Replit.
    """
    expires = _future_ms(30)
    req = _post(
        _evt(
            event_id="evt_transfer",
            event_type="TRANSFER",
            app_user_id="user_clerk_B",
            product_id="pro_monthly",
            entitlements=["pro"],
            expiration_ms=expires,
        )
    )
    _run(rc_billing.handle_webhook(req))  # type: ignore[arg-type]
    assert "user_clerk_B" in fake_db.subscriptions
    row = fake_db.subscriptions["user_clerk_B"]
    assert row["tier"] == billing.TIER_PRO
    assert row["status"] == "active"


# ---------------------------------------------------------------------------
# Idempotent replay
# ---------------------------------------------------------------------------


def test_duplicate_event_id_short_circuits_with_no_extra_writes(fake_db):
    """RC re-delivers events on transient failures.  The second delivery
    must NOT produce another upsert into ``subscriptions``."""
    payload = _evt(
        event_id="evt_dup",
        event_type="INITIAL_PURCHASE",
        app_user_id="user_dup",
        expiration_ms=_future_ms(30),
    )

    # The audit-only `rc_webhook_deliveries` table is appended to on
    # every delivery (including duplicates) so the operator dashboard
    # can show the dedupe traffic. Exclude it when asserting that no
    # *derived* state was mutated by the second delivery.
    def _derived_writes() -> list[tuple[str, tuple]]:
        return [
            row
            for row in fake_db.execute_log
            if not row[0].startswith("INSERT INTO rc_webhook_deliveries")
        ]

    first = _run(rc_billing.handle_webhook(_post(payload)))  # type: ignore[arg-type]
    assert first == {"status": "ok", "event_id": "evt_dup"}
    derived_after_first = len(_derived_writes())

    second = _run(rc_billing.handle_webhook(_post(payload)))  # type: ignore[arg-type]
    assert second == {"status": "duplicate", "event_id": "evt_dup"}
    assert len(_derived_writes()) == derived_after_first


# ---------------------------------------------------------------------------
# Per-status delivery audit (powers the operator dashboard tile)
# ---------------------------------------------------------------------------


def _delivery_rows(fake_db: _FakeDB) -> list[tuple[str, tuple]]:
    return [
        row
        for row in fake_db.execute_log
        if row[0].startswith("INSERT INTO rc_webhook_deliveries")
    ]


def test_delivery_audit_records_ok_outcome(fake_db):
    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_audit_ok",
                    event_type="INITIAL_PURCHASE",
                    app_user_id="user_audit_ok",
                    expiration_ms=_future_ms(30),
                )
            )
        )
    )
    rows = _delivery_rows(fake_db)
    assert len(rows) == 1
    status_bucket, http_status, event_id, event_type, _err = rows[0][1]
    assert status_bucket == "ok"
    assert http_status == 200
    assert event_id == "evt_audit_ok"
    assert event_type == "INITIAL_PURCHASE"


def test_delivery_audit_records_duplicate_outcome(fake_db):
    payload = _evt(
        event_id="evt_audit_dup",
        event_type="INITIAL_PURCHASE",
        app_user_id="user_audit_dup",
        expiration_ms=_future_ms(30),
    )
    _run(rc_billing.handle_webhook(_post(payload)))  # type: ignore[arg-type]
    _run(rc_billing.handle_webhook(_post(payload)))  # type: ignore[arg-type]
    rows = _delivery_rows(fake_db)
    statuses = [r[1][0] for r in rows]
    assert statuses == ["ok", "duplicate"]


def test_delivery_audit_records_4xx_on_bad_auth(fake_db):
    req = _post(
        _evt(event_id="evt_bad", event_type="INITIAL_PURCHASE", app_user_id="u"),
        auth="Bearer wrong",
    )
    with pytest.raises(Exception):
        _run(rc_billing.handle_webhook(req))  # type: ignore[arg-type]
    rows = _delivery_rows(fake_db)
    assert len(rows) == 1
    status_bucket, http_status, *_ = rows[0][1]
    assert status_bucket == "http_4xx"
    assert http_status == 401


def test_delivery_audit_records_5xx_when_secret_unset(fake_db, monkeypatch):
    monkeypatch.delenv("REVENUECAT_WEBHOOK_AUTH_HEADER", raising=False)
    req = _post(_evt(event_id="evt_unset", event_type="INITIAL_PURCHASE", app_user_id="u"))
    with pytest.raises(Exception):
        _run(rc_billing.handle_webhook(req))  # type: ignore[arg-type]
    rows = _delivery_rows(fake_db)
    assert len(rows) == 1
    status_bucket, http_status, *_ = rows[0][1]
    assert status_bucket == "http_5xx"
    assert http_status == 503


def test_event_with_no_app_user_id_is_ignored_safely(fake_db):
    """RC occasionally fires events with no app_user_id (anonymous
    receipts).  We log and drop — no tier update, no crash."""
    payload = {
        "event": {
            "id": "evt_no_user",
            "type": "INITIAL_PURCHASE",
            "product_id": "pro_monthly",
            "entitlement_ids": ["pro"],
        }
    }
    res = _run(rc_billing.handle_webhook(_post(payload)))  # type: ignore[arg-type]
    assert res["status"] == "ok"
    assert fake_db.subscriptions == {}
    assert fake_db.rc_subscriptions == {}


# ---------------------------------------------------------------------------
# End-to-end: webhook → /api/me/subscription
# ---------------------------------------------------------------------------


def _user(uid: str = "user_e2e", email: str | None = "e2e@example.com") -> AuthedUser:
    claims: dict[str, Any] = {"sub": uid}
    if email:
        claims["email"] = email
    return AuthedUser(user_id=uid, session_id="sess_test", claims=claims)


# ---------------------------------------------------------------------------
# Operator-driven retry of a stuck delivery (task #283)
# ---------------------------------------------------------------------------


def _force_apply_failure(monkeypatch, count: int = 1) -> dict[str, int]:
    """Make the next ``count`` calls to ``_apply_event`` raise.

    Returns a counter dict the test can read after the run so it can
    assert how many calls actually went through the failing wrapper.
    """
    calls = {"n": 0, "max": int(count)}
    real_apply = rc_billing._apply_event

    async def _flaky(event):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] <= calls["max"]:
            raise RuntimeError("simulated apply failure")
        await real_apply(event)

    monkeypatch.setattr(rc_billing, "_apply_event", _flaky)
    return calls


def test_failed_event_can_be_listed_and_retried_clears_the_stuck_delivery(
    fake_db, monkeypatch
):
    """The full operator path: a delivery 5xxs, the dashboard surfaces
    it, and retry re-runs the stored payload — clearing the row from
    the failed-events list and writing the derived subscription."""
    expires = _future_ms(30)
    payload = _evt(
        event_id="evt_stuck",
        event_type="INITIAL_PURCHASE",
        app_user_id="user_stuck",
        product_id="pro_monthly",
        entitlements=["pro"],
        expiration_ms=expires,
    )

    # First delivery: apply fails — but the raw mirror is written by
    # ``_apply_event`` *before* the derived row, so we still have a
    # stored payload to retry from. Force a failure inside the derived
    # write step to exercise that contract.
    real_apply = rc_billing._apply_event

    async def _apply_then_fail(event):  # type: ignore[no-untyped-def]
        # Run the real raw-mirror INSERT first by calling the real
        # function but hijack the second INSERT to throw.
        original_execute = _FakeConn.execute
        seen = {"derived": 0}

        async def _flaky_execute(self, query, *args):  # type: ignore[no-untyped-def]
            q = " ".join(query.split())
            if q.startswith("INSERT INTO subscriptions ( user_id, tier, status,"):
                seen["derived"] += 1
                raise RuntimeError("simulated derived write failure")
            return await original_execute(self, query, *args)

        monkeypatch.setattr(_FakeConn, "execute", _flaky_execute)
        try:
            await real_apply(event)
        finally:
            monkeypatch.setattr(_FakeConn, "execute", original_execute)

    monkeypatch.setattr(rc_billing, "_apply_event", _apply_then_fail)

    with pytest.raises(Exception) as exc:
        _run(rc_billing.handle_webhook(_post(payload)))  # type: ignore[arg-type]
    assert getattr(exc.value, "status_code", None) == 500

    # Restore the real apply so retry can succeed.
    monkeypatch.setattr(rc_billing, "_apply_event", real_apply)

    # Dashboard sees the stuck event.
    failed = _run(rc_billing.list_failed_events(limit=10))
    assert any(r["event_id"] == "evt_stuck" for r in failed)
    row = next(r for r in failed if r["event_id"] == "evt_stuck")
    assert row["raw_available"] is True
    assert row["event_type"] == "INITIAL_PURCHASE"
    assert row["attempts"] >= 1

    # Retry runs the same code path — derived row is now written and
    # the idempotency table is populated.
    result = _run(rc_billing.retry_event("evt_stuck"))
    assert result["status"] == "ok"
    assert result["event_id"] == "evt_stuck"
    assert "user_stuck" in fake_db.subscriptions
    assert fake_db.subscriptions["user_stuck"]["tier"] == billing.TIER_PRO
    assert "evt_stuck" in fake_db.rc_events

    # The retry wrote an `ok` delivery row so the failed-events list no
    # longer surfaces this event.
    cleared = _run(rc_billing.list_failed_events(limit=10))
    assert all(r["event_id"] != "evt_stuck" for r in cleared)


def test_retry_event_with_no_stored_payload_returns_missing(fake_db):
    """If we only have the delivery audit row but no raw payload (e.g.
    the very first INSERT into rc_subscriptions failed), retry must
    not silently no-op — it returns ``status=missing`` so the operator
    sees a clear error in the UI."""
    fake_db.deliveries.append(
        {
            "status": rc_billing.DELIVERY_STATUS_5XX,
            "http_status": 500,
            "event_id": "evt_no_raw",
            "event_type": "INITIAL_PURCHASE",
            "error": "boom",
            "received_at": datetime.now(tz=timezone.utc),
        }
    )
    result = _run(rc_billing.retry_event("evt_no_raw"))
    assert result["status"] == "missing"
    assert "no stored payload" in (result.get("error") or "").lower()


def test_retry_event_records_failure_and_increments_attempts_when_apply_still_throws(
    fake_db, monkeypatch
):
    """A retry that itself throws must produce a fresh 5xx delivery
    row so the operator can see the new attempt and the latest error."""
    # Seed a stored raw payload by running a successful initial purchase.
    payload = _evt(
        event_id="evt_retry_fail",
        event_type="INITIAL_PURCHASE",
        app_user_id="user_retry_fail",
        expiration_ms=_future_ms(30),
    )
    _run(rc_billing.handle_webhook(_post(payload)))  # type: ignore[arg-type]
    assert "evt_retry_fail" in fake_db.rc_events

    # Make the next apply throw.
    _force_apply_failure(monkeypatch, count=1)

    result = _run(rc_billing.retry_event("evt_retry_fail"))
    assert result["status"] == "failed"
    assert "simulated apply failure" in (result.get("error") or "")

    # The 5xx delivery row from the retry attempt is recorded with
    # the marker so it's distinguishable in the audit log.
    retry_5xx = [
        d
        for d in fake_db.deliveries
        if d["event_id"] == "evt_retry_fail"
        and d["status"] == rc_billing.DELIVERY_STATUS_5XX
    ]
    assert retry_5xx, "expected a 5xx delivery row from the retry"
    assert any("retry:" in (d.get("error") or "") for d in retry_5xx)


def test_initial_purchase_surfaces_through_me_subscription_endpoint(fake_db):
    """End-to-end: an INITIAL_PURCHASE webhook for Clerk user id
    ``user_e2e`` must land at ``GET /api/me/subscription`` as
    ``tier=pro`` — this is what the mobile app actually reads to render
    the subscription screen."""
    from app.auth import require_auth

    user = _user("user_e2e", email="e2e@example.com")
    expires = _future_ms(30)

    _run(
        rc_billing.handle_webhook(  # type: ignore[arg-type]
            _post(
                _evt(
                    event_id="evt_e2e",
                    event_type="INITIAL_PURCHASE",
                    app_user_id="user_e2e",
                    expiration_ms=expires,
                )
            )
        )
    )

    app = FastAPI()

    @app.get("/api/me/subscription")
    async def _me(u: AuthedUser = Depends(require_auth)) -> dict[str, Any]:
        sub = await billing.get_user_subscription(u)
        return sub.as_dict()

    app.dependency_overrides[require_auth] = lambda: user
    client = TestClient(app)
    resp = client.get("/api/me/subscription")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "pro"
    assert body["status"] == "active"
    assert body["interval"] == "month"
    # The mobile UI keys off the ``rc:`` prefix to hide the Stripe
    # portal button — exercise that contract here.
    assert body["stripe_subscription_id"].startswith("rc:")
    assert body["stripe_customer_id"] is None
