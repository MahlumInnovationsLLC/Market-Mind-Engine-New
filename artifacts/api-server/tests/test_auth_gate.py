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
    # Task #739 — per-league surrogate-vs-V1 Brier deltas surfaced on
    # the operator dashboard, plus the underlying-JSON download.
    "/api/health/surrogate-backtest",
    "/api/health/surrogate-backtest/report.json",
    # Task #814 — per-vertical safety-threshold tuner sweep + drift
    # surfaced on the operator dashboard plus the underlying JSON.
    "/api/health/safety-threshold-tuning",
    "/api/health/safety-threshold-tuning/report.json",
    "/api/dashboard/overview",
    "/api/dashboard/recent-trades",
    "/api/dashboard/swarm-personas",
    "/api/dashboard/swarm-personas-today",
    "/api/dashboard/swarm-fallbacks-today",
    # Task #361 — per-provider LLM fallback rate panel on the operator dashboard.
    "/api/dashboard/llm-fallback-rates",
    "/api/dashboard/recent-cycles",
    "/api/dashboard/vertical-performance",
    # Task #756 — weekly V1 backtest gate tile (red on per-league Brier regression).
    "/api/dashboard/v1-backtest-gate",
    "/api/dashboard/over-filled-orders-today",
    "/api/dashboard/crypto-ohlcv-retention",
    # Task #640 — backtest history retention status panel on the operator dashboard.
    "/api/dashboard/crypto/backtest-retention",
    # Task #699 — sportradar leaderboards history retention status panel on the operator dashboard.
    "/api/dashboard/sportradar-leaderboards-retention",
    # Task #757 — Supabase growth/retention loop status panel on the operator dashboard.
    "/api/dashboard/supabase-growth-retention",
    "/api/dashboard/bankroll/recent",
    "/api/dashboard/slippage-guard-rejects",
    "/api/dashboard/recent-debates",
    "/api/dashboard/debates/{debate_id}",
    # Task #614 — per-entrant Kalshi outright market resolution for the
    # Plackett-Luce field on the swarm-debates panel.
    "/api/dashboard/debates/{debate_id}/entrant-markets",
    # Live Games dashboard — read-model snapshot of in-progress markets.
    "/api/live-games",
    # Live Games chatbot model catalog (per-provider availability).
    "/api/live-games/models",
    # Today's (and near-future) scheduled sports matchups.
    "/api/upcoming-games",
    "/api/dashboard/uw-signals",
    "/api/dashboard/trades/{trade_ref}",
    "/api/search",
    "/api/storage/list",
    "/api/models/inventory",
    "/api/models/status",
    "/api/admin/users",
    "/api/admin/users/{clerk_user_id}",
    "/api/admin/watchdog",
    "/api/admin/rc-webhook/stats",
    # Task #283 — operator-driven retry of stuck mobile IAP webhooks.
    "/api/admin/rc-webhook/failed-events",
    "/api/admin/audit-log",
    "/api/admin/audit-log.csv",
    "/api/admin/audit-log/actions",
    "/api/admin/landing-leads",
    "/api/admin/landing-leads.csv",
    "/api/admin/landing-leads/interests",
    # Task #709 — operator drift-resolution console queue (GET).
    "/api/admin/reconciler/drifts",
    # Task #710 — operator order-submit dead-letter console queue (GET).
    "/api/admin/orders/dead-lettered",
    # Task #712 — operator per-trade alert opt-out preferences (GET).
    "/api/admin/operator-alerts/preferences",
    "/api/me/access",
    "/api/me/subscription",
    "/api/me/notification-prefs",
    "/api/me/notification-history",
    "/api/me/notifications",
    "/api/me/telegram",
    "/api/feeds/status",
    "/api/feeds/kalshi-crypto-candidates",
    "/api/features/registry",
    "/api/features/drift",
    "/api/notifications/status",
    "/api/notifications/daily-summary/preview",
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
    "/api/admin/access-requests/denied",
    "/api/orchestrator/live-execute",
    "/api/risk/daily-loss",
    "/api/market-title/{ticker}",
    "/api/polymarket/balance",
    "/api/polymarket/positions",
    "/api/polymarket/markets",
    "/api/polymarket/orders/resting",
    "/api/admin/runtime-settings",
    # Operator-tunable LLM gatekeeper provider/model selector.
    "/api/admin/runtime-settings/gatekeeper-model",
    "/api/admin/tech-ai/promotion-status",
    "/api/dashboard/tech-ai/markets/{ticker}/evidence",
    "/api/admin/elections/promotion-status",
    "/api/admin/events-econ/promotion-status",
    "/api/admin/entertainment/promotion-status",
    # Task #225 — historical entertainment-signal backfill status surface.
    "/api/admin/entertainment/backfill-status",
    "/api/dashboard/elections/markets/{ticker}/arb",
    # Task #823 — operator preview of today's tradable WNBA player props.
    "/api/dashboard/wnba/props/today",
    # Task #829 — per-league player-prop dashboard previews.
    "/api/dashboard/nba/props/today",
    "/api/dashboard/nhl/props/today",
    "/api/dashboard/mlb/props/today",
    "/api/dashboard/nfl/props/today",
    "/api/dashboard/runtime-settings",
    "/api/admin/risk-knobs",
    "/api/admin/risk-knobs/history",
    # Task #714 — per-vertical gatekeeper-knob tuning (effective view + audit).
    "/api/admin/gatekeeper/effective",
    "/api/admin/gatekeeper/history",
    "/api/admin/vertical-visibility",
    "/api/admin/verticals/halts",
    # Task #713 — recurring halt schedules + audit history.
    "/api/admin/verticals/halts/schedule",
    "/api/admin/verticals/halts/history",
    "/api/dashboard/verticals",
    "/api/dashboard/vertical-visibility",
    # Task #644 — operator-console rollup of realised crypto-market
    # accuracy (brier / hit rate over 24h + 7d) computed from the
    # ``predictions.outcome`` / ``settle_price`` columns.
    "/api/dashboard/crypto-accuracy",
    "/api/admin/models",
    "/api/admin/models/mirror/status",
    # Task #181 — operator-only crypto-binary backtest panel.
    "/api/admin/crypto/backtest",
    "/api/admin/crypto/backtest/history",
    # Task #580 — rolling-window realised accuracy / Brier / log-loss
    # for settled Kalshi crypto predictions.
    "/api/admin/crypto/accuracy",
    # Task #646 — backfill status for historical crypto model probabilities.
    "/api/admin/crypto/backfill-probabilities-status",
    "/api/admin/autoresearch/features",
    "/api/admin/margin-regressor/status",
    # Task #245 — operator-only calibration & research dashboards.
    "/api/calibration/snapshots",
    "/api/research/directives",
    # Task #155 — per-league calibration panel on the operator dashboard.
    "/api/dashboard/calibration/per-league",
    # Task #755 — champion-challenger calibrator A/B history tile.
    "/api/dashboard/calibration/ab",
    # Task #208 — NFL per-league + Sunday-window exposure panel.
    "/api/dashboard/nfl-exposure",
    # Task #437 — multi-league exposure panel (NFL panel generalised).
    "/api/dashboard/leagues-exposure",
    # Task #650 — saved tournament leaderboard history (PGA / F1).
    "/api/dashboard/tournament-leaderboards",
    # Task #697 — operator-dashboard view of recent tournament-replay
    # backtest runs + admin-only listing of the same persisted runs.
    "/api/dashboard/tournament-backtest/recent",
    "/api/admin/tournament-backtest/recent",
    # Task #698 — per-event tournament leaderboard timeline scrubber.
    "/api/dashboard/tournament-leaderboard-timeline",
    # Per-source weather freshness badge — operator dashboard only.
    "/api/weather/freshness",
    # Task #769 — backup soccer settlement source: operator review queue
    # listing + dashboard tile feed.  Covers Sportradar/ESPN
    # disagreements and primary-stale + secondary-down enqueues.
    "/api/admin/soccer-settlement-review",
    "/api/dashboard/soccer-settlement-review",
    # Task #832 — Top-Edges + bookmaker-grammar tiles (web + mobile).
    "/api/dashboard/top-edges",
    "/api/dashboard/bookmaker-metrics",
    # Task #837 — per-sport Live-Games-Now tile on the operator dashboard.
    "/api/dashboard/live-now",
    # Task #844 — Edge Terminal scraper input mode.
    "/api/edge-terminal/input-mode",
    "/api/edge-terminal/status",
    "/api/dashboard/edge-terminal-pnl",
    "/api/dashboard/edge-terminal-signals",
)

# Mutating endpoints — auth-protected, accept POST / PUT / DELETE.  These
# get the same missing-header / malformed / expired / wrong-issuer /
# wrong-key rejection coverage as the GET-based dashboard endpoints, just
# parametrised over (method, route) so we exercise the right HTTP verb.
# Listed as (method, route) tuples; the inventory check derives the path
# set from this list as well.
PROTECTED_MUTATING_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/api/health/model-alerts/{alert_id}/ack"),
    ("POST", "/api/health/acknowledge-alert"),
    ("POST", "/api/admin/users/{clerk_user_id}/approved"),
    ("POST", "/api/admin/users/{clerk_user_id}/admin"),
    ("POST", "/api/billing/checkout-session"),
    ("POST", "/api/billing/portal-session"),
    ("POST", "/api/notifications/test"),
    ("POST", "/api/me/push-token"),
    ("DELETE", "/api/me/push-token"),
    ("PUT", "/api/me/notification-prefs"),
    ("POST", "/api/me/notification-prefs/sms/start-verification"),
    ("POST", "/api/me/notification-prefs/sms/verify"),
    ("POST", "/api/me/notifications/read"),
    ("DELETE", "/api/me/notifications"),
    ("DELETE", "/api/me/notifications/{notification_id}"),
    ("DELETE", "/api/me/telegram"),
    ("POST", "/api/me/telegram/link"),
    ("POST", "/api/me/telegram/notify-push"),
    ("POST", "/api/orchestrator/run-cycle"),
    ("POST", "/api/orchestrator/live-execute"),
    ("POST", "/api/settlement/run-cycle"),
    ("POST", "/api/settlement/settle-market"),
    ("POST", "/api/settlement/backfill"),
    ("POST", "/api/swarm/debate"),
    # Live Games chatbot — single-shot Anthropic Q&A grounded in the
    # per-market live context.
    ("POST", "/api/live-games/{market_id}/ask"),
    ("POST", "/api/kalshi/paper-order"),
    ("POST", "/api/kalshi/resume"),
    ("POST", "/api/models/reload"),
    ("POST", "/api/models/predict"),
    ("POST", "/api/admin/access-requests/{clerk_user_id}"),
    ("POST", "/api/admin/persona-toggles"),
    ("POST", "/api/admin/emergency-stop"),
    ("POST", "/api/admin/rearm-trading"),
    # Task #756 — admin-triggered manual fire of the daily honesty report.
    ("POST", "/api/admin/honesty-report/fire-now"),
    ("POST", "/api/polymarket/paper-order"),
    ("POST", "/api/admin/runtime-settings"),
    ("DELETE", "/api/admin/runtime-settings/revert"),
    # Task #356 — saved aggression-mode reason presets.
    ("POST", "/api/admin/runtime-settings/presets"),
    ("DELETE", "/api/admin/runtime-settings/presets/{preset_id}"),
    ("POST", "/api/admin/risk-knobs"),
    # Task #714 — per-vertical gatekeeper-knob tuning (set / clear).
    ("POST", "/api/admin/gatekeeper/override"),
    ("DELETE", "/api/admin/gatekeeper/override"),
    ("POST", "/api/admin/vertical-visibility"),
    ("POST", "/api/admin/tech-ai/promote-kelly"),
    ("POST", "/api/admin/elections/promote-kelly"),
    ("POST", "/api/admin/events-econ/promote-kelly"),
    ("POST", "/api/admin/entertainment/promote-kelly"),
    # Task #225 — admin-triggered historical entertainment-signal backfill.
    ("POST", "/api/admin/entertainment/backfill"),
    # Task #646 — admin-triggered historical crypto model-probability backfill.
    ("POST", "/api/admin/crypto/backfill-probabilities"),
    ("POST", "/api/admin/orchestrator/run-cycle"),
    # Admin-triggered out-of-band feed poll (commit 6899fc1).
    ("POST", "/api/admin/feeds/{name}/poll-now"),
    ("GET",  "/api/admin/elo/status"),
    ("POST", "/api/admin/elo/run-cycle"),
    ("POST", "/api/admin/models/active"),
    ("POST", "/api/admin/models/mirror/push"),
    ("POST", "/api/admin/autoresearch/features/{name}/decision"),
    ("POST", "/api/admin/autoresearch/run"),
    # Task #425 — operator-triggered backfill of uw_signals.ticker.
    ("POST", "/api/admin/uw-signals/backfill-ticker"),
    ("POST", "/api/admin/margin-regressor/refit"),
    # Task #271 — per-vertical halt/resume toggles.
    ("POST", "/api/admin/verticals/{vertical}/halt"),
    ("POST", "/api/admin/verticals/{vertical}/resume"),
    # Task #713 — recurring halt schedule create / toggle / delete.
    ("POST", "/api/admin/verticals/halts/schedule"),
    ("PATCH", "/api/admin/verticals/halts/schedule/{schedule_id}"),
    ("DELETE", "/api/admin/verticals/halts/schedule/{schedule_id}"),
    # Task #245 — research-directives admin gating + drift rollback workflow.
    ("POST", "/api/research/directives/{directive_id}/decision"),
    # Task #283 — operator-driven retry of stuck mobile IAP webhooks.
    ("POST", "/api/admin/rc-webhook/retry/{event_id}"),
    # Task #697 — admin-triggered tournament backtest replay
    # (single-event or date-range sweep).
    ("POST", "/api/admin/tournament-backtest/replay"),
    # Task #709 — operator drift-resolution actions: accept exchange
    # truth (closes local rows + inserts synthetic) and snooze (silence
    # the alert until an absolute UTC timestamp).
    ("POST", "/api/admin/reconciler/drifts/{ticker}/accept-exchange-truth"),
    ("POST", "/api/admin/reconciler/drifts/{ticker}/snooze"),
    # Task #710 — operator dead-letter resolution actions
    # (mark_cancelled / force_retry / claim_filled).
    ("POST", "/api/admin/orders/{client_order_id}/resolve"),
    # Task #712 — operator per-trade alert opt-out preferences (POST).
    ("POST", "/api/admin/operator-alerts/preferences"),
    # Task #769 — backup soccer settlement source: operator one-click
    # resolve endpoint that routes the chosen 3-way result back through
    # ``settle_market`` after writing an admin_audit_log row.
    ("POST", "/api/admin/soccer-settlement-review/{review_id}/resolve"),
    # Task #828 — best-line cross-book router operator endpoints.
    ("GET",  "/api/admin/best-line-alerts/full"),
    ("POST", "/api/admin/best-line-alerts/mark-placed"),
    # Task #832 — Top-Edges tile operator actions (snooze / fire).
    ("POST", "/api/dashboard/candidates/{prediction_id}/snooze"),
    ("POST", "/api/dashboard/candidates/{prediction_id}/fire"),
    # Task #844 — Edge Terminal input-mode toggle (Elite-only POST).
    ("POST", "/api/edge-terminal/input-mode"),
)

# Path-only view of the mutating-endpoint inventory, used by the
# route-inventory check below.
PROTECTED_MUTATING_ROUTES: tuple[str, ...] = tuple(
    {route for _method, route in PROTECTED_MUTATING_ENDPOINTS}
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
    "/api/public/tail-risk-exposure",
    # Task #828 — public best-line alerts surface for the dashboard
    # tile.  Returns redacted data (no operator_email, no notes, no
    # amounts); full PII variant lives behind admin auth.
    "/api/public/best-line-alerts",
    "/api/public/stats",
    "/api/public/recent-signals",
    "/api/public/leads",
    # Events / macro-economics upcoming-release calendar — surfaces only
    # public release schedule + consensus, no auth required (task #202).
    "/api/events-econ/calendar",
    # Settled macro release surprises (task #222) — actual vs consensus
    # for the dashboard panel; release prints are public information.
    "/api/events-econ/release-history",
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
    # Postmark bounce webhook authenticates via HTTP Basic auth using
    # POSTMARK_WEBHOOK_USER / POSTMARK_WEBHOOK_PASSWORD (Postmark
    # forwards credentials embedded in the configured webhook URL),
    # not Bearer tokens. Dedicated coverage lives in
    # test_postmark_webhook_auth below.
    "/api/notifications/postmark-webhook",
    # One-click unsubscribe link uses an HMAC-signed token in the URL.
    "/api/notifications/unsubscribe",
    # Readiness probe — public, but returns 503 until the READY gate
    # flips, so it's not parameterized into PUBLIC_ROUTES (which asserts
    # 200). Exercised directly by tests/test_ready_gate.py.
    "/api/health/ready",
    # Localhost-only diagnostic endpoint that returns the in-process
    # cached health snapshot without requiring a Clerk JWT.  The handler
    # checks `request.client.host in {"127.0.0.1", "::1", "localhost"}`
    # and returns 404 for any non-loopback caller, so it is safe to leave
    # ungated; under the TestClient `request.client.host == "testclient"`
    # so it returns 404 there too.
    "/api/_debug/health-snapshot",
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
        | set(PROTECTED_MUTATING_ROUTES)
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
# Same rejection coverage for mutating endpoints (POST / PUT / DELETE).
#
# FastAPI resolves the path-operation function by method, so we have to hit
# each route with the verb it actually accepts — a GET against a POST-only
# route returns 405 before the auth dependency even runs, which would mask
# regressions where a new mutating endpoint forgets to depend on
# `require_auth`.  The auth dependency runs before request-body parsing, so
# we deliberately send no body here: a bare 401 is the contract we care
# about.
# ---------------------------------------------------------------------------


def _request_no_body(client: TestClient, method: str, route: str, **kwargs):
    # Some FastAPI test-client backends 400 on a missing Content-Length for
    # body-bearing verbs; pass an explicit empty JSON to keep the request
    # well-formed without committing to a particular schema.
    if method.upper() in {"POST", "PUT", "PATCH"} and "json" not in kwargs:
        kwargs["json"] = {}
    return client.request(method, route, **kwargs)


@pytest.mark.parametrize("method,route", PROTECTED_MUTATING_ENDPOINTS)
def test_protected_mutating_endpoints_reject_missing_header(client, method, route):
    _assert_unauthorized(_request_no_body(client, method, route))


@pytest.mark.parametrize("method,route", PROTECTED_MUTATING_ENDPOINTS)
@pytest.mark.parametrize(
    "header_value",
    [
        "",
        "Bearer",
        "Bearer ",
        "Token abc.def.ghi",
        "abc.def.ghi",
        "Bearer not-a-jwt",
    ],
)
def test_protected_mutating_endpoints_reject_malformed_header(
    client, method, route, header_value
):
    resp = _request_no_body(
        client, method, route, headers={"Authorization": header_value}
    )
    _assert_unauthorized(resp)


@pytest.mark.parametrize("method,route", PROTECTED_MUTATING_ENDPOINTS)
def test_protected_mutating_endpoints_reject_expired_token(
    client, method, route, legit_keypair
):
    token = _make_token(legit_keypair, expired=True)
    resp = _request_no_body(
        client, method, route, headers={"Authorization": f"Bearer {token}"}
    )
    _assert_unauthorized(resp)


@pytest.mark.parametrize("method,route", PROTECTED_MUTATING_ENDPOINTS)
def test_protected_mutating_endpoints_reject_wrong_issuer(
    client, method, route, legit_keypair
):
    token = _make_token(legit_keypair, issuer="https://clerk.attacker.example.com")
    resp = _request_no_body(
        client, method, route, headers={"Authorization": f"Bearer {token}"}
    )
    _assert_unauthorized(resp)


@pytest.mark.parametrize("method,route", PROTECTED_MUTATING_ENDPOINTS)
def test_protected_mutating_endpoints_reject_token_signed_by_wrong_key(
    client, method, route, attacker_keypair
):
    token = _make_token(attacker_keypair)
    resp = _request_no_body(
        client, method, route, headers={"Authorization": f"Bearer {token}"}
    )
    _assert_unauthorized(resp)


# ---------------------------------------------------------------------------
# Sanity check: with a valid token the gate at least *passes auth* (the
# handler may still 5xx because Postgres/Supabase aren't reachable in tests,
# which is fine — what we care about is that we are no longer getting 401).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Postmark bounce webhook — public to Bearer-token auth, but locked down
# behind HTTP Basic auth keyed off POSTMARK_WEBHOOK_USER / _PASSWORD.
# Forged events would let an attacker silently suppress real customer
# email deliveries, so this is gated explicitly.
# ---------------------------------------------------------------------------


_POSTMARK_PATH = "/api/notifications/postmark-webhook"
_POSTMARK_USER = "pm-test-user"
_POSTMARK_PASS = "pm-test-pass"
_POSTMARK_PAYLOAD = {"RecordType": "Bounce", "Email": "x@example.com"}


def _basic(user: str, password: str) -> str:
    import base64

    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


@pytest.fixture
def _postmark_creds(monkeypatch):
    monkeypatch.setenv("POSTMARK_WEBHOOK_USER", _POSTMARK_USER)
    monkeypatch.setenv("POSTMARK_WEBHOOK_PASSWORD", _POSTMARK_PASS)
    yield


def test_postmark_webhook_refuses_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("POSTMARK_WEBHOOK_USER", raising=False)
    monkeypatch.delenv("POSTMARK_WEBHOOK_PASSWORD", raising=False)
    resp = client.post(_POSTMARK_PATH, json=_POSTMARK_PAYLOAD)
    assert resp.status_code == 503, resp.text


@pytest.mark.parametrize(
    "header_value",
    [
        None,                         # missing entirely
        "",                           # empty
        "Bearer abc.def.ghi",         # wrong scheme
        "Basic",                      # no credentials
        "Basic !!!not-base64!!!",     # malformed encoding
        _basic("wrong-user", _POSTMARK_PASS),
        _basic(_POSTMARK_USER, "wrong-pass"),
        _basic("", ""),
    ],
)
def test_postmark_webhook_rejects_bad_auth(client, _postmark_creds, header_value):
    headers = {} if header_value is None else {"Authorization": header_value}
    resp = client.post(_POSTMARK_PATH, json=_POSTMARK_PAYLOAD, headers=headers)
    assert resp.status_code == 401, (
        f"expected 401 but got {resp.status_code}: {resp.text[:300]}"
    )


def test_postmark_webhook_accepts_valid_basic_auth(client, _postmark_creds, monkeypatch):
    """Valid credentials get past the auth gate and reach the handler.

    We stub the suppression apply call so the test never touches the DB —
    the assertion we care about is that valid Basic auth is no longer 401.
    """
    from app import main as main_module

    async def _ok(payload):  # noqa: ANN001
        return {"ok": True, "applied": True, "echo": payload}

    monkeypatch.setattr(
        main_module.notif_suppression, "apply_postmark_event", _ok
    )

    resp = client.post(
        _POSTMARK_PATH,
        json=_POSTMARK_PAYLOAD,
        headers={"Authorization": _basic(_POSTMARK_USER, _POSTMARK_PASS)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("applied") is True


def test_valid_token_passes_auth_gate(client, legit_keypair):
    token = _make_token(legit_keypair)
    resp = client.get(
        "/api/dashboard/overview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code != 401, (
        f"valid token was rejected ({resp.status_code}): {resp.text[:300]}"
    )
