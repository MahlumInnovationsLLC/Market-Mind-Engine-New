"""Tier-gating tests for the swarm transcript endpoint and the
Elite-only live-execute toggle.

Why this exists
---------------
Two product-critical gates that previously had no automated coverage:

  * ``/api/dashboard/recent-debates`` must redact per-persona reasoning
    + the moderator narrative for sub-Pro callers, but still return the
    consensus / verdict so the dashboard can render an upsell.  Pro+
    callers get the full transcript verbatim.

  * ``/api/orchestrator/live-execute`` (POST) must 403 sub-Elite callers
    via the standard ``tier_required`` payload from
    :func:`billing.require_tier`.  Elite callers must succeed and the
    write must land in ``system_settings.risk.trading_mode`` — the same
    key the kill-switch gate honours.

The asyncpg pool is replaced with an in-memory fake that emulates the
two queries these endpoints actually run; ``billing.get_user_subscription``
is replaced wholesale so the suite never touches the real billing schema.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import billing, dashboard
from app.auth import AuthedUser, require_auth
from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(uid: str = "user_test", email: str | None = None) -> AuthedUser:
    claims = {"sub": uid}
    if email:
        claims["email"] = email
    return AuthedUser(user_id=uid, session_id="sess_test", claims=claims)


def _sub(tier: str, *, uid: str = "user_test") -> billing.UserSubscription:
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


_SAMPLE_DEBATE: dict[str, Any] = {
    "id": 42,
    "league": "NBA",
    "matchup": "LAL vs BOS",
    "market_type": "moneyline",
    "consensus_probability": 0.55,
    "confidence_band_low": 0.50,
    "confidence_band_high": 0.60,
    "agreement_rate": 0.83,
    "approved": True,
    "recommended_action": "BUY_YES",
    "outcome": None,
    "cost_estimate": 0.0123,
    "latency_ms": 1234.0,
    "analyst_verdicts": [
        {"persona": "quant_value", "lean": "YES", "reasoning": "edge vs market"},
        {"persona": "contrarian", "lean": "NO", "reasoning": "books are sharp"},
    ],
    "moderator_synthesis": {"reasoning": "weighted majority lands YES"},
    # Per-debate audit blob.  For tournament-market debates the
    # orchestrator merges a Plackett-Luce per-entrant sketch in here so
    # the analyst dashboard can render the full field of probabilities.
    # Sub-Pro callers see this stripped along with the rest of the
    # transcript (it can identify the kalshi_market_id).
    "game_context": {
        "kalshi_market_id": "KX-NBA-001",
        "plackett_luce_sketch": {
            "model": "plackett_luce",
            "entrant": "alcaraz",
            "entrant_rating": 2200,
            "entrant_probability": 0.45,
            "field_size": 3,
            "ratings": {"alcaraz": 2200, "sinner": 2150, "djokovic": 2100},
            "probabilities": {"alcaraz": 0.45, "sinner": 0.35, "djokovic": 0.20},
        },
    },
    # Pinned to a far-past timestamp so the free-tier 15-minute signal
    # delay (see app/free_tier.py) never drops this fixture row.
    "created_at": "2020-01-01T00:00:00Z",
}


@pytest.fixture
def patch_recent_debates(monkeypatch):
    """Replace the DB-backed reader with a deterministic single row."""
    async def _fake_recent(limit: int = 10) -> list[dict[str, Any]]:
        return [dict(_SAMPLE_DEBATE)]

    monkeypatch.setattr(dashboard, "get_recent_swarm_debates", _fake_recent)
    return _fake_recent


@pytest.fixture
def patch_debate_by_id(monkeypatch):
    """Replace the per-id reader so the tests exercise the endpoint
    without round-tripping through the real ``swarm_debates`` table."""
    state: dict[str, Any] = {"row": dict(_SAMPLE_DEBATE)}

    async def _fake_get(debate_id: int) -> dict[str, Any] | None:
        row = state["row"]
        if row is None:
            return None
        if int(debate_id) != int(row["id"]):
            return None
        return dict(row)

    monkeypatch.setattr(dashboard, "get_swarm_debate_by_id", _fake_get)
    return state


@pytest.fixture
def patch_subscription(monkeypatch):
    """Stub ``billing.get_user_subscription`` so each test picks the tier
    without round-tripping through the real subscriptions table."""
    holder: dict[str, billing.UserSubscription] = {
        "value": _sub(billing.TIER_FREE)
    }

    async def _fake_get(user: AuthedUser) -> billing.UserSubscription:
        return holder["value"]

    monkeypatch.setattr(billing, "get_user_subscription", _fake_get)
    return holder


@pytest.fixture
def override_auth():
    """Bypass the real Clerk JWT validation; the per-test ``user`` is
    injected directly into the dependency tree."""
    user = _user()
    app.dependency_overrides[require_auth] = lambda: user
    yield user
    app.dependency_overrides.pop(require_auth, None)


# ---------------------------------------------------------------------------
# Pro redaction on /api/dashboard/recent-debates
# ---------------------------------------------------------------------------


def test_recent_debates_redacts_transcripts_for_free_callers(
    patch_recent_debates, patch_subscription, override_auth
):
    patch_subscription["value"] = _sub(billing.TIER_FREE)
    client = TestClient(app)
    resp = client.get("/api/dashboard/recent-debates?limit=5")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == billing.TIER_FREE
    assert len(body["items"]) == 1
    item = body["items"][0]
    # Public-safe fields still present.
    assert item["id"] == 42
    assert item["matchup"] == "LAL vs BOS"
    assert item["consensus_probability"] == 0.55
    assert item["recommended_action"] == "BUY_YES"
    # Persona reasoning + moderator narrative are gone — and the
    # game_context audit blob (which carries the Plackett-Luce sketch
    # and the kalshi market id) is stripped along with them.
    assert "analyst_verdicts" not in item
    assert "moderator_synthesis" not in item
    assert "game_context" not in item
    assert item["redacted"] is True


@pytest.mark.parametrize(
    "tier", [billing.TIER_PRO, billing.TIER_ELITE, billing.TIER_FOUNDER]
)
def test_recent_debates_returns_full_transcript_for_pro_plus(
    tier, patch_recent_debates, patch_subscription, override_auth
):
    patch_subscription["value"] = _sub(tier)
    client = TestClient(app)
    resp = client.get("/api/dashboard/recent-debates?limit=5")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == tier
    item = body["items"][0]
    # Persona reasoning is preserved verbatim.
    assert isinstance(item["analyst_verdicts"], list)
    assert {v["persona"] for v in item["analyst_verdicts"]} == {
        "quant_value",
        "contrarian",
    }
    assert "reasoning" in item["analyst_verdicts"][0]
    assert item["moderator_synthesis"]["reasoning"].startswith("weighted")
    # game_context (Plackett-Luce per-entrant sketch + market id) is
    # passed through verbatim so the analyst tournament view can render
    # the full field.
    assert item["game_context"]["kalshi_market_id"] == "KX-NBA-001"
    sketch = item["game_context"]["plackett_luce_sketch"]
    assert sketch["entrant"] == "alcaraz"
    assert sketch["field_size"] == 3
    assert set(sketch["probabilities"]) == {"alcaraz", "sinner", "djokovic"}
    assert sum(sketch["probabilities"].values()) == pytest.approx(1.0, abs=1e-9)
    assert item["redacted"] is False


# ---------------------------------------------------------------------------
# Per-id /api/dashboard/debates/{id} fallback for the mobile detail screen
# ---------------------------------------------------------------------------


def test_debate_by_id_redacts_transcripts_for_free_callers(
    patch_debate_by_id, patch_subscription, override_auth
):
    """Sub-Pro callers see the same redacted shape the list endpoint
    produces — public-safe fields only, ``redacted=true``, no
    per-persona reasoning, no moderator narrative."""
    patch_subscription["value"] = _sub(billing.TIER_FREE)
    client = TestClient(app)
    resp = client.get("/api/dashboard/debates/42")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == billing.TIER_FREE
    item = body["item"]
    assert item["id"] == 42
    assert item["matchup"] == "LAL vs BOS"
    assert item["consensus_probability"] == 0.55
    assert item["recommended_action"] == "BUY_YES"
    assert "analyst_verdicts" not in item
    assert "moderator_synthesis" not in item
    assert item["redacted"] is True
    assert body["delayed"] is True


@pytest.mark.parametrize(
    "tier", [billing.TIER_PRO, billing.TIER_ELITE, billing.TIER_FOUNDER]
)
def test_debate_by_id_returns_full_transcript_for_pro_plus(
    tier, patch_debate_by_id, patch_subscription, override_auth
):
    """Pro+ (and founders) get the full transcript verbatim — same
    contract as the list endpoint, exposed one row at a time so the
    mobile detail screen can render even when the requested debate
    has fallen off the cached recent feed."""
    patch_subscription["value"] = _sub(tier)
    client = TestClient(app)
    resp = client.get("/api/dashboard/debates/42")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == tier
    item = body["item"]
    assert isinstance(item["analyst_verdicts"], list)
    assert {v["persona"] for v in item["analyst_verdicts"]} == {
        "quant_value",
        "contrarian",
    }
    assert item["moderator_synthesis"]["reasoning"].startswith("weighted")
    assert item["redacted"] is False
    assert body["delayed"] is False


def test_debate_by_id_returns_404_for_unknown_id(
    patch_debate_by_id, patch_subscription, override_auth
):
    """The reader returns ``None`` for a missing row → endpoint 404s
    instead of leaking an empty payload."""
    patch_subscription["value"] = _sub(billing.TIER_PRO)
    client = TestClient(app)
    resp = client.get("/api/dashboard/debates/999")
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "debate_not_found"}


def test_debate_by_id_404s_within_free_tier_delay_window(
    patch_debate_by_id, patch_subscription, override_auth
):
    """A debate younger than the free-tier signal-delay window must
    look like it doesn't exist yet to sub-Pro callers — otherwise the
    per-id endpoint would be a trivial way to side-step the delay
    that the list endpoint already enforces."""
    fresh_row = dict(_SAMPLE_DEBATE)
    fresh_row["created_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    patch_debate_by_id["row"] = fresh_row

    patch_subscription["value"] = _sub(billing.TIER_FREE)
    client = TestClient(app)
    resp = client.get("/api/dashboard/debates/42")
    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "debate_not_found"}

    # Pro callers see the fresh row immediately — the delay only
    # applies below the Pro tier.
    patch_subscription["value"] = _sub(billing.TIER_PRO)
    resp = client.get("/api/dashboard/debates/42")
    assert resp.status_code == 200, resp.text
    assert resp.json()["item"]["id"] == 42


# ---------------------------------------------------------------------------
# Elite-only POST /api/orchestrator/live-execute
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_live_execute_db(monkeypatch):
    """In-memory fake of the two queries the live-execute endpoints run.

    Also stubs :func:`app.orchestrator.preflight.run_preflight` to return
    a passing result so the tier-gating assertions don't get masked by
    a (legitimately) failing pre-flight on a stub DB.  Tests that want
    to exercise the pre-flight path live in ``test_live_execute_preflight.py``.
    """
    # ``executes`` records *only* the trading-mode flips so the existing
    # assertions stay tight; the canary seed writes (Task #752 step 5)
    # are tracked separately in ``canary_executes``.
    state: dict[str, Any] = {
        "value": None, "executes": [], "canary": {"remaining": None},
        "canary_executes": [],
    }

    async def _fake_fetchval(query: str, *args: Any) -> Any:
        # Trading-mode override.
        if "system_settings" in query and "risk.trading_mode" in query:
            return state["value"]
        # Canary counter lookup (Task #752 step 5).  Live cutover calls
        # ``ensure_initialized`` which does ``SELECT value FROM
        # system_settings WHERE key = $1`` with key='risk.canary_remaining'.
        if (
            "system_settings" in query
            and "WHERE key" in query
            and args
            and args[0] == "risk.canary_remaining"
        ):
            return state["canary"]["remaining"]
        raise AssertionError(f"unexpected fetchval: {query!r} args={args!r}")

    async def _fake_execute(query: str, *args: Any) -> str:
        upper = query.lstrip().upper()
        if not upper.startswith("INSERT INTO SYSTEM_SETTINGS"):
            raise AssertionError(f"unexpected execute: {query!r}")
        # First positional arg is either the literal trading_mode value
        # (legacy single-arg insert) or the key (two-arg insert used by
        # the canary helpers).  Discriminate so legacy assertions only
        # see paper/live values.
        if args and args[0] in ("paper", "live", "halt"):
            state["value"] = args[0]
            state["executes"].append(args[0])
        else:
            # Canary seed / init-at write.
            state["canary_executes"].append(args)
            if args and args[0] == "risk.canary_remaining" and len(args) >= 2:
                state["canary"]["remaining"] = args[1]
        return "OK"

    from app import db as db_module
    from app import main as main_module
    from app.orchestrator import preflight as _preflight

    async def _passing_preflight() -> _preflight.PreflightResult:
        return _preflight.PreflightResult(
            passed=True,
            checks=[
                _preflight.PreflightCheck(
                    id="trading_mode_target",
                    label="Target mode = live",
                    passed=True,
                ),
            ],
        )

    monkeypatch.setattr(db_module, "fetchval", _fake_fetchval)
    monkeypatch.setattr(db_module, "execute", _fake_execute)
    monkeypatch.setattr(_preflight, "run_preflight", _passing_preflight)
    # main.py uses ``db.fetchval`` / ``db.execute`` via module-level
    # ``from . import db``; the same patch above covers that import.
    assert main_module.db is db_module
    return state


@pytest.mark.parametrize("tier", [billing.TIER_FREE, billing.TIER_PRO])
def test_live_execute_toggle_rejects_sub_elite(
    tier, patch_subscription, override_auth, patch_live_execute_db
):
    patch_subscription["value"] = _sub(tier)
    client = TestClient(app)
    resp = client.post(
        "/api/orchestrator/live-execute", json={"enabled": True}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json() == {
        "detail": {
            "error": "tier_required",
            "required_tier": billing.TIER_ELITE,
            "current_tier": tier,
        }
    }
    # Crucially: the gate fires before the write.  No DB execute.
    assert patch_live_execute_db["executes"] == []


@pytest.mark.parametrize(
    "tier", [billing.TIER_ELITE, billing.TIER_FOUNDER]
)
def test_live_execute_toggle_allows_elite_and_persists(
    tier, patch_subscription, override_auth, patch_live_execute_db
):
    patch_subscription["value"] = _sub(tier)
    client = TestClient(app)
    resp = client.post(
        "/api/orchestrator/live-execute", json={"enabled": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["effective_mode"] == "live"
    assert body["db_mode"] == "live"
    assert body["source"] == "db"
    assert body["caller_tier"] == tier
    assert body["can_toggle"] is True
    assert patch_live_execute_db["executes"] == ["live"]

    # Flipping back to paper persists too.
    resp2 = client.post(
        "/api/orchestrator/live-execute", json={"enabled": False}
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["enabled"] is False
    assert body2["effective_mode"] == "paper"
    assert body2["db_mode"] == "paper"
    assert patch_live_execute_db["executes"] == ["live", "paper"]


def test_live_execute_state_reports_can_toggle_per_tier(
    patch_subscription, override_auth, patch_live_execute_db
):
    """``GET`` is readable by everyone signed in, but ``can_toggle`` only
    flips true for Elite+.  The dashboard relies on this flag to decide
    whether to render the toggle at all."""
    client = TestClient(app)

    patch_subscription["value"] = _sub(billing.TIER_FREE)
    resp = client.get("/api/orchestrator/live-execute")
    assert resp.status_code == 200
    assert resp.json()["can_toggle"] is False

    patch_subscription["value"] = _sub(billing.TIER_PRO)
    resp = client.get("/api/orchestrator/live-execute")
    assert resp.json()["can_toggle"] is False

    patch_subscription["value"] = _sub(billing.TIER_ELITE)
    resp = client.get("/api/orchestrator/live-execute")
    assert resp.json()["can_toggle"] is True
