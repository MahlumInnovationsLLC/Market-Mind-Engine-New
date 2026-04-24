"""Task #297: bot-initiated `OrderRequest`s must carry the operator's
clerk_user_id in `context["user_id"]` so the lifecycle's per-user
trade-confirmation email path fires on live fills.

Covers two layers:
  * Unit: ``_dispatch_via_lifecycle`` stamps the resolved operator id
    onto the ``OrderRequest.context`` it builds for both Kalshi and
    Polymarket dispatches; resolution honors the env var and falls back
    to the bootstrap admin in ``app_users``.
  * Smoke / e2e-ish: ``place_order`` -> Kalshi lifecycle -> WS fill ->
    trade-confirmation email pipeline produces a single confirmation
    addressed to the operator without the caller passing user_id
    explicitly. Confirms the wiring change actually unblocks the
    confirmation flow that task #269 set up.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app import db as _db
from app.kalshi import lifecycle as _kalshi_lifecycle
from app.notifications import alerts as _alerts
from app.orchestrator import paper as _paper
from app.risk.gates import RiskContext


# ---------------------------------------------------------------------------
# Resolver: env var wins
# ---------------------------------------------------------------------------


def test_resolve_operator_user_id_prefers_env_var(monkeypatch):
    _paper._reset_operator_user_id_cache()
    monkeypatch.setattr(
        _paper, "get_settings",
        lambda: SimpleNamespace(operator_user_id="user_env_42"),
    )
    # DB fallback should never be consulted when the env var resolves.
    called = {"db": False}

    async def _boom(*_a, **_kw):
        called["db"] = True
        raise AssertionError("db.fetchval should not be called")

    monkeypatch.setattr(_paper.db, "fetchval", _boom)

    out = asyncio.run(_paper._resolve_operator_user_id())
    assert out == "user_env_42"
    assert called["db"] is False
    # Cached: a second call returns the same value without re-querying.
    out2 = asyncio.run(_paper._resolve_operator_user_id())
    assert out2 == "user_env_42"


# ---------------------------------------------------------------------------
# Resolver: DB fallback
# ---------------------------------------------------------------------------


def test_resolve_operator_user_id_falls_back_to_bootstrap_admin(monkeypatch):
    _paper._reset_operator_user_id_cache()
    monkeypatch.setattr(
        _paper, "get_settings",
        lambda: SimpleNamespace(operator_user_id=None),
    )
    captured: dict[str, Any] = {}

    async def _fake_fetchval(query: str, *args: Any) -> Any:
        captured["query"] = query
        return "user_bootstrap_admin"

    monkeypatch.setattr(_paper.db, "fetchval", _fake_fetchval)
    out = asyncio.run(_paper._resolve_operator_user_id())
    assert out == "user_bootstrap_admin"
    assert "app_users" in captured["query"]
    assert "is_admin" in captured["query"]


def test_resolve_operator_user_id_transient_db_error_is_not_cached(monkeypatch):
    """A transient `db.fetchval` failure must not pin the cache to None
    for the process lifetime — the next bot order should retry. Without
    this, a brief DB blip during startup could silently disable
    trade-confirmation emails until the API server is restarted.
    """
    _paper._reset_operator_user_id_cache()
    monkeypatch.setattr(
        _paper, "get_settings",
        lambda: SimpleNamespace(operator_user_id=None),
    )
    calls = {"n": 0}

    async def _flaky(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB outage")
        return "user_recovered"

    monkeypatch.setattr(_paper.db, "fetchval", _flaky)
    out1 = asyncio.run(_paper._resolve_operator_user_id())
    assert out1 is None
    out2 = asyncio.run(_paper._resolve_operator_user_id())
    assert out2 == "user_recovered"
    assert calls["n"] == 2


def test_resolve_operator_user_id_returns_none_when_nothing_known(monkeypatch):
    _paper._reset_operator_user_id_cache()
    monkeypatch.setattr(
        _paper, "get_settings",
        lambda: SimpleNamespace(operator_user_id=""),
    )

    async def _none(*_a, **_kw):
        return None

    monkeypatch.setattr(_paper.db, "fetchval", _none)
    out = asyncio.run(_paper._resolve_operator_user_id())
    assert out is None


# ---------------------------------------------------------------------------
# Stamping: Kalshi dispatch
# ---------------------------------------------------------------------------


def _ctx() -> RiskContext:
    return RiskContext(
        market_id="KXBTC-99",
        side="YES",
        vertical="crypto",
        league=None,
        market_type=None,
        matchup=None,
        current_price=0.42,
        model_probability=0.5,
        edge=0.08,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=10,
        intended_notional_usd=4.2,
        bankroll_usd=1000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )


def test_kalshi_dispatch_stamps_operator_user_id(monkeypatch):
    _paper._reset_operator_user_id_cache()
    monkeypatch.setattr(
        _paper, "get_settings",
        lambda: SimpleNamespace(
            operator_user_id="user_operator_xyz",
            trading_mode="paper",
        ),
    )

    captured: dict[str, Any] = {}

    async def _fake_submit(req, *, force_paper=None):
        captured["req"] = req
        captured["force_paper"] = force_paper
        return SimpleNamespace(
            exchange_order_id="exo-1",
            state="filled",
            filled_count=req.count,
            avg_fill_price_cents=42,
            order_id=req.client_order_id,
        )

    from app.kalshi import lifecycle as kalshi_lc
    monkeypatch.setattr(kalshi_lc, "submit_order", _fake_submit)

    result = asyncio.run(_paper._dispatch_via_lifecycle(
        exchange="kalshi",
        ctx=_ctx(),
        cand=None,
        side_norm="yes",
        quantity=5,
        price=0.42,
        paper=True,
        consensus=0.5,
        edge=0.08,
    ))
    req = captured["req"]
    assert isinstance(req.context, dict)
    assert req.context.get("user_id") == "user_operator_xyz"
    # Other context fields preserved.
    assert req.context.get("model_consensus") == 0.5
    assert req.context.get("vertical") == "crypto"
    assert result["exchange_order_id"] == "exo-1"


def test_polymarket_dispatch_stamps_operator_user_id(monkeypatch):
    _paper._reset_operator_user_id_cache()
    monkeypatch.setattr(
        _paper, "get_settings",
        lambda: SimpleNamespace(
            operator_user_id="user_operator_poly",
            trading_mode="paper",
        ),
    )

    captured: dict[str, Any] = {}

    async def _fake_submit(req, *, force_paper=None):
        captured["req"] = req
        return SimpleNamespace(
            exchange_order_id="poly-1",
            state="filled",
            filled_count=req.count,
            avg_fill_price_cents=42,
            order_id=req.client_order_id,
        )

    from app.polymarket import lifecycle as poly_lc
    monkeypatch.setattr(poly_lc, "submit_order", _fake_submit)

    cand = {
        "polymarket_market_id": "0xabc",
        "polymarket_token_id": "tok-yes",
    }
    asyncio.run(_paper._dispatch_via_lifecycle(
        exchange="polymarket",
        ctx=_ctx(),
        cand=cand,
        side_norm="yes",
        quantity=5,
        price=0.42,
        paper=True,
        consensus=0.5,
        edge=0.08,
    ))
    req = captured["req"]
    assert isinstance(req.context, dict)
    assert req.context.get("user_id") == "user_operator_poly"


def test_dispatch_omits_user_id_when_unresolved(monkeypatch):
    _paper._reset_operator_user_id_cache()
    monkeypatch.setattr(
        _paper, "get_settings",
        lambda: SimpleNamespace(
            operator_user_id=None, trading_mode="paper",
        ),
    )

    async def _none(*_a, **_kw):
        return None

    monkeypatch.setattr(_paper.db, "fetchval", _none)

    captured: dict[str, Any] = {}

    async def _fake_submit(req, *, force_paper=None):
        captured["req"] = req
        return SimpleNamespace(
            exchange_order_id="exo-x",
            state="filled",
            filled_count=req.count,
            avg_fill_price_cents=42,
            order_id=req.client_order_id,
        )

    from app.kalshi import lifecycle as kalshi_lc
    monkeypatch.setattr(kalshi_lc, "submit_order", _fake_submit)

    asyncio.run(_paper._dispatch_via_lifecycle(
        exchange="kalshi",
        ctx=_ctx(),
        cand=None,
        side_norm="yes",
        quantity=1,
        price=0.42,
        paper=True,
        consensus=0.5,
        edge=0.08,
    ))
    # Missing user_id is the lifecycle's documented silent no-op for
    # trade-confirmation; surface that explicitly so a regression that
    # accidentally re-introduces a default sentinel would fail loudly.
    assert "user_id" not in captured["req"].context


# ---------------------------------------------------------------------------
# End-to-end smoke: bot-initiated live fill emails the operator
# ---------------------------------------------------------------------------


class _EmailHarness:
    """Tiny stand-in for Postgres + Postmark scoped to one test."""

    def __init__(self, operator_user_id: str, operator_email: str) -> None:
        self.operator_user_id = operator_user_id
        self.operator_email = operator_email
        self.dedupe: dict[str, datetime] = {}
        self.sent: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "notification_dedupe" in query:
            key, ttl = args[0], int(args[1])
            now = datetime.now(timezone.utc)
            existing = self.dedupe.get(key)
            if existing is None or existing <= now:
                self.dedupe[key] = now + timedelta(seconds=ttl)
                return {"ok": 1}
            return None
        if "FROM app_users" in query:
            user_id = args[0]
            if user_id != self.operator_user_id:
                return None
            return {
                "email": self.operator_email,
                "conf": True,
                "digest": False,
            }
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, *_a: Any, **_kw: Any) -> str:
        return "OK"


def test_bot_initiated_live_fill_emails_operator(monkeypatch):
    """The whole point of task #297: a bot order goes through the
    Kalshi lifecycle as live, the WS fill arrives, and the per-user
    confirmation lands in the operator's inbox without the caller
    having to know the operator's user_id."""
    operator_user_id = "user_operator_e2e"
    operator_email = "operator@example.com"
    harness = _EmailHarness(operator_user_id, operator_email)

    # --- Stub the resolver so we don't depend on env / DB shape -------
    _paper._reset_operator_user_id_cache()
    monkeypatch.setattr(
        _paper, "get_settings",
        lambda: SimpleNamespace(
            operator_user_id=operator_user_id, trading_mode="live",
        ),
    )

    # --- Stub Postgres + Postmark like test_trade_confirmation does ---
    monkeypatch.setattr(_db, "fetchrow", harness.fetchrow)
    monkeypatch.setattr(_db, "fetch", harness.fetch)
    monkeypatch.setattr(_db, "execute", harness.execute)

    async def _fake_send(**kwargs: Any) -> list[dict[str, Any]]:
        harness.sent.append(kwargs)
        return [{"MessageID": f"m-{len(harness.sent)}"}]

    from app.notifications import postmark as _pm
    monkeypatch.setattr(_pm, "send", _fake_send)

    from app.notifications import suppression as _supp

    async def _not_suppressed(_email: str) -> bool:
        return False

    monkeypatch.setattr(_supp, "is_suppressed", _not_suppressed)

    from app.kalshi import market_titles as _titles

    async def _title(_t: str) -> str | None:
        return "Will Bitcoin close above $50,000?"

    monkeypatch.setattr(_titles, "get_title", _title)

    # --- Stub lifecycle side effects we don't exercise here -----------
    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    async def _always_fresh(**_kw: Any) -> bool:
        return True

    monkeypatch.setattr(_kalshi_lifecycle, "_record", _noop)
    monkeypatch.setattr(_kalshi_lifecycle, "_emit", _noop)
    monkeypatch.setattr(
        _kalshi_lifecycle, "_record_fill_idempotent", _always_fresh
    )
    monkeypatch.setattr(_kalshi_lifecycle, "_upsert_position_rollup", _noop)
    monkeypatch.setattr(_alerts, "live_trade", lambda **_kw: {"stub": True})

    # --- Capture the OrderRequest the bot ships to lifecycle ----------
    captured_req: dict[str, Any] = {}

    async def _fake_kalshi_submit(req, *, force_paper=None):
        captured_req["req"] = req
        # Mimic a live submit that goes to resting; the test then
        # injects a WS fill referencing this client_order_id.
        rec = _kalshi_lifecycle.OrderRecord(
            order_id=req.client_order_id,
            request=req, paper=False, state="resting",
            exchange_order_id=f"exo_{req.client_order_id}",
        )
        async with _kalshi_lifecycle._resting_lock:
            _kalshi_lifecycle._resting[req.client_order_id] = rec
        return rec

    monkeypatch.setattr(
        _kalshi_lifecycle, "submit_order", _fake_kalshi_submit
    )

    async def go() -> None:
        result = await _paper._dispatch_via_lifecycle(
            exchange="kalshi",
            ctx=_ctx(),
            cand=None,
            side_norm="yes",
            quantity=10,
            price=0.42,
            paper=False,
            consensus=0.5,
            edge=0.08,
        )
        coid = result["client_order_id"]
        # WS pushes a single full-fill against the resting order.
        await _kalshi_lifecycle.handle_ws_fill({
            "msg": {
                "type": "fill",
                "msg": {
                    "client_order_id": coid,
                    "count": 10, "yes_price": 42,
                    "trade_id": "fill_e2e_1",
                    "created_time": "2026-04-23T12:00:00Z",
                },
            }
        })
        # Drain the background email task.
        for _ in range(30):
            await asyncio.sleep(0)

    # Use a fresh loop so the lifecycle's lock binds to it.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        # Re-bind module asyncio primitives onto this loop.
        _kalshi_lifecycle._resting.clear()
        _kalshi_lifecycle._in_flight.clear()
        _kalshi_lifecycle._resting_lock = asyncio.Lock()
        loop.run_until_complete(go())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    # The bot order carried the operator id: required precondition for
    # the email path to engage.
    assert captured_req["req"].context.get("user_id") == operator_user_id

    confirmations = [s for s in harness.sent if s.get("tag") == "trade-confirmation"]
    assert len(confirmations) == 1, (
        "expected one trade-confirmation email for the bot fill, "
        f"got {len(confirmations)}: {harness.sent}"
    )
    assert confirmations[0]["to"] == [operator_email]
