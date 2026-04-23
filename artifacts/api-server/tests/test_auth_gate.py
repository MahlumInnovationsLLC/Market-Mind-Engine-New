"""Regression suite proving telemetry endpoints stay private without sign-in.

The lockdown was previously only verified by hand with curl. These tests
enumerate every protected route and assert each one rejects:
  * a request with no Authorization header
  * a request with a malformed Authorization header
  * a request bearing an expired JWT signed by the legitimate key
  * a request bearing a JWT signed by the wrong issuer (key swap)

They also assert that `/api/healthz` stays public.

The Clerk JWKS is replaced with an in-memory RSA keypair so the tests can
exercise the real `app.auth.require_auth` code path — including signature
validation, expiry, and issuer checks — without any network access.
"""
from __future__ import annotations

import time
from typing import Iterable

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth as auth_module
from app.main import app
from tests.conftest import TEST_CLERK_HOST


# ---------------------------------------------------------------------------
# Route inventory — kept in sync with app/main.py
# ---------------------------------------------------------------------------

# GET routes that require auth — exercised by the parameterized
# auth-rejection tests below (which all use HTTP GET).
PROTECTED_ROUTES: tuple[str, ...] = (
    "/api/health",
    "/api/health/snapshot",
    "/api/health/errors",
    "/api/health/model-alerts",
    "/api/dashboard/overview",
    "/api/dashboard/recent-trades",
    "/api/dashboard/swarm-personas",
    "/api/dashboard/recent-cycles",
    "/api/dashboard/vertical-performance",
    "/api/dashboard/recent-debates",
    "/api/dashboard/uw-signals",
    "/api/dashboard/trades/{trade_ref}",
    "/api/search",
    "/api/storage/list",
    "/api/models/inventory",
    "/api/models/status",
    "/api/admin/users",
    "/api/admin/users/{clerk_user_id}",
    "/api/admin/audit-log",
    "/api/me/access",
    "/api/me/subscription",
    "/api/me/notification-prefs",
    "/api/me/telegram",
    "/api/feeds/status",
    "/api/features/registry",
    "/api/features/drift",
    "/api/notifications/status",
    "/api/orchestrator/status",
    "/api/orchestrator/recent-events",
    "/api/settlement/status",
    "/api/settlement/backfill-status",
    "/api/kalshi/balance",
    "/api/kalshi/positions",
    "/api/kalshi/markets",
    "/api/kalshi/orders/resting",
    "/api/kalshi/kill-switch",
    "/api/admin/access-requests",
    "/api/orchestrator/live-execute",
    "/api/risk/daily-loss",
    "/api/market-title/{ticker}",
    "/api/polymarket/balance",
    "/api/polymarket/positions",
    "/api/polymarket/markets",
    "/api/polymarket/orders/resting",
    "/api/admin/runtime-settings",
    "/api/admin/tech-ai/promotion-status",
    "/api/dashboard/tech-ai/markets/{ticker}/evidence",
    "/api/admin/elections/promotion-status",
    "/api/admin/events-econ/promotion-status",
    "/api/admin/entertainment/promotion-status",
    "/api/dashboard/elections/markets/{ticker}/arb",
    "/api/dashboard/runtime-settings",
    "/api/admin/risk-knobs",
    "/api/admin/vertical-visibility",
    "/api/dashboard/verticals",
    "/api/dashboard/vertical-visibility",
    "/api/admin/models",
    "/api/admin/models/mirror/status",
    "/api/admin/autoresearch/features",
    # Task #245 — operator-only calibration & research dashboards.
    "/api/calibration/snapshots",
    "/api/research/directives",
)

# Mutating endpoints — auth-protected but only accept POST.  We don't
# parametrize them through the GET-based rejection tests (FastAPI would
# answer 405 before the auth dependency runs); they're listed here so
# the inventory check still catches missing coverage.
PROTECTED_POST_ROUTES: tuple[str, ...] = (
    "/api/health/model-alerts/{alert_id}/ack",
    "/api/admin/users/{clerk_user_id}/approved",
    "/api/admin/users/{clerk_user_id}/admin",
    "/api/billing/checkout-session",
    "/api/billing/portal-session",
    "/api/notifications/test",
    "/api/me/push-token",
    "/api/me/telegram/link",
    "/api/orchestrator/run-cycle",
    "/api/settlement/run-cycle",
    "/api/settlement/settle-market",
    "/api/settlement/backfill",
    "/api/swarm/debate",
    "/api/kalshi/paper-order",
    "/api/kalshi/resume",
    "/api/models/reload",
    "/api/models/predict",
    "/api/admin/access-requests/{clerk_user_id}",
    "/api/admin/persona-toggles",
    "/api/admin/emergency-stop",
    "/api/health/acknowledge-alert",
    "/api/polymarket/paper-order",
    "/api/admin/runtime-settings",
    "/api/admin/risk-knobs",
    "/api/admin/tech-ai/promote-kelly",
    "/api/admin/elections/promote-kelly",
    "/api/admin/events-econ/promote-kelly",
    "/api/admin/entertainment/promote-kelly",
    "/api/admin/orchestrator/run-cycle",
    "/api/admin/models/active",
    "/api/admin/models/mirror/push",
    "/api/admin/autoresearch/features/{name}/decision",
    "/api/admin/autoresearch/run",
    # Task #245 — research-directives admin gating + drift rollback workflow.
    "/api/research/directives/{directive_id}/decision",
)

# Liveness probes — must answer 200 with no auth (parameterized below).
PUBLIC_ROUTES: tuple[str, ...] = (
    "/api/healthz",
)

# Other unauthenticated surface — covered by the inventory check only.
#   * /api/billing/pricing + /api/public/*: marketing-page surface.
#   * /api/billing/{webhook,revenuecat-webhook}: webhooks authenticated
#     via HMAC signature, not Bearer tokens.
#   * /api/kalshi/halt: kill switch — operators must be able to halt
#     trading even when downstream auth is broken.
#   * /api/ws/*: WebSockets authenticate via the `?token=` query param
#     inside the handler, not the require_auth dependency.
OTHER_PUBLIC_ROUTES: tuple[str, ...] = (
    "/api/billing/pricing",
    "/api/billing/webhook",
    "/api/billing/revenuecat-webhook",
    "/api/public/equity-curve",
    "/api/public/stats",
    "/api/public/recent-signals",
    "/api/public/leads",
    # Events / macro-economics upcoming-release calendar — surfaces only
    # public release schedule + consensus, no auth required (task #202).
    "/api/events-econ/calendar",
    # Entertainment vertical (task #203) — public release calendar
    # + awards leaderboard.  No auth: schedule + scores are public.
    "/api/entertainment/calendar",
    "/api/entertainment/awards-leaderboard",
    "/api/kalshi/halt",
    "/api/ws/kalshi",
    "/api/ws/llm-logs",
    "/api/ws/pipeline",
    # Task #245 — per-channel WS split + Clerk webhook.  WebSockets
    # auth via `?token=` inside the handler; Clerk webhook auth via
    # svix-signature HMAC, not Bearer tokens.
    "/api/ws/positions",
    "/api/ws/trades",
    "/api/ws/alerts",
    "/api/ws/orderbook/{ticker}",
    "/api/ws/temperature/{city}",
    "/api/webhooks/clerk",
    "/api/ws/settlement",
    "/api/ws/polymarket",
    # Postmark bounce webhook authenticates via shared-secret in body.
    "/api/notifications/postmark-webhook",
    # One-click unsubscribe link uses an HMAC-signed token in the URL.
    "/api/notifications/unsubscribe",
    # Readiness probe — public, but returns 503 until the READY gate
    # flips, so it's not parameterized into PUBLIC_ROUTES (which asserts
    # 200). Exercised directly by tests/test_ready_gate.py.
    "/api/health/ready",
)


def test_route_inventory_matches_app() -> None:
    """Fail loudly if a new /api/* route is added without test coverage.

    Catches the exact regression this suite exists to prevent: a future PR
    adding a dashboard endpoint and forgetting to gate it (or to list it
    here).
    """
    declared = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/")}
    covered = (
        set(PROTECTED_ROUTES)
        | set(PROTECTED_POST_ROUTES)
        | set(PUBLIC_ROUTES)
        | set(OTHER_PUBLIC_ROUTES)
    )
    missing = declared - covered
    assert not missing, (
        f"New /api/* route(s) not covered by the auth-gate tests: {sorted(missing)}. "
        "Add them to PROTECTED_ROUTES or PUBLIC_ROUTES in tests/test_auth_gate.py."
    )


# ---------------------------------------------------------------------------
# In-memory JWKS plumbing
# ---------------------------------------------------------------------------


class _StubSigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _StubJWKSClient:
    """Stand-in for `jwt.PyJWKClient` — returns a pre-baked public key."""

    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> _StubSigningKey:  # noqa: D401
        return _StubSigningKey(self._public_key)


@pytest.fixture(scope="module")
def legit_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def attacker_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch, legit_keypair):
    """Force `app.auth` to verify against our in-memory keypair."""
    auth_module._frontend_api_host.cache_clear()
    auth_module._issuer.cache_clear()
    auth_module._jwks_client.cache_clear()
    monkeypatch.setattr(
        auth_module,
        "_jwks_client",
        lambda: _StubJWKSClient(legit_keypair.public_key()),
    )
    yield
    # monkeypatch restores `_jwks_client` automatically; just bust the
    # other caches so a later non-patched test sees fresh state.
    auth_module._frontend_api_host.cache_clear()
    auth_module._issuer.cache_clear()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


ISSUER = f"https://{TEST_CLERK_HOST}"


def _make_token(
    private_key,
    *,
    issuer: str = ISSUER,
    expired: bool = False,
    sub: str = "user_test_123",
) -> str:
    now = int(time.time())
    iat = now - (3600 if expired else 60)
    exp = now - 60 if expired else now + 3600
    return jwt.encode(
        {"iss": issuer, "sub": sub, "iat": iat, "nbf": iat, "exp": exp, "sid": "sess_test"},
        private_key,
        algorithm="RS256",
    )


# ---------------------------------------------------------------------------
# Public route stays public
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", PUBLIC_ROUTES)
def test_public_routes_are_open(client, route):
    """`/api/healthz` MUST stay reachable without any credentials so that
    uptime checks and the load balancer can probe liveness."""
    resp = client.get(route)
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") == "ok"


# ---------------------------------------------------------------------------
# Protected routes reject the four ways auth can be missing/forged
# ---------------------------------------------------------------------------


def _assert_unauthorized(resp) -> None:
    assert resp.status_code == 401, (
        f"expected 401 but got {resp.status_code}: {resp.text[:300]}"
    )


@pytest.mark.parametrize("route", PROTECTED_ROUTES)
def test_protected_routes_reject_missing_header(client, route):
    _assert_unauthorized(client.get(route))


@pytest.mark.parametrize("route", PROTECTED_ROUTES)
@pytest.mark.parametrize(
    "header_value",
    [
        "",                       # empty header
        "Bearer",                 # scheme only, no token
        "Bearer ",                # scheme + whitespace
        "Token abc.def.ghi",      # wrong scheme
        "abc.def.ghi",            # missing scheme entirely
        "Bearer not-a-jwt",       # well-formed scheme, garbage token
    ],
)
def test_protected_routes_reject_malformed_header(client, route, header_value):
    resp = client.get(route, headers={"Authorization": header_value})
    _assert_unauthorized(resp)


@pytest.mark.parametrize("route", PROTECTED_ROUTES)
def test_protected_routes_reject_expired_token(client, route, legit_keypair):
    token = _make_token(legit_keypair, expired=True)
    resp = client.get(route, headers={"Authorization": f"Bearer {token}"})
    _assert_unauthorized(resp)


@pytest.mark.parametrize("route", PROTECTED_ROUTES)
def test_protected_routes_reject_wrong_issuer(client, route, legit_keypair):
    """A JWT minted for a different Clerk instance must not be accepted,
    even if it is otherwise well-formed and within its validity window."""
    token = _make_token(legit_keypair, issuer="https://clerk.attacker.example.com")
    resp = client.get(route, headers={"Authorization": f"Bearer {token}"})
    _assert_unauthorized(resp)


@pytest.mark.parametrize("route", PROTECTED_ROUTES)
def test_protected_routes_reject_token_signed_by_wrong_key(
    client, route, attacker_keypair, monkeypatch, legit_keypair
):
    """Token has the right issuer string but is signed by a key the JWKS
    does not vouch for — i.e. the attacker forged the payload but lacks the
    private key."""
    # JWKS still serves the legitimate public key (set up by autouse fixture);
    # we just sign with a key the server has never seen.
    token = _make_token(attacker_keypair)
    resp = client.get(route, headers={"Authorization": f"Bearer {token}"})
    _assert_unauthorized(resp)


# ---------------------------------------------------------------------------
# Sanity check: with a valid token the gate at least *passes auth* (the
# handler may still 5xx because Postgres/Supabase aren't reachable in tests,
# which is fine — what we care about is that we are no longer getting 401).
# ---------------------------------------------------------------------------


def test_valid_token_passes_auth_gate(client, legit_keypair):
    token = _make_token(legit_keypair)
    resp = client.get(
        "/api/dashboard/overview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code != 401, (
        f"valid token was rejected ({resp.status_code}): {resp.text[:300]}"
    )
