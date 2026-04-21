"""Axiom-Predict FastAPI application — foundation slice.

This is the minimum viable backend that:
  * Connects to the existing Supabase Postgres (149-table production schema)
  * Connects to Supabase Storage (axiom-predict-artifacts bucket)
  * Exposes `/api/healthz` (open) plus Clerk-gated dashboard endpoints
  * Reports which trained models are present in storage
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import billing, dashboard, db, models_loader, storage
from .auth import (
    AuthedUser,
    auth_configured,
    check_approval,
    require_approved_user,
    require_auth,
)
from .config import get_settings
from .feeds import registry as feed_registry, scheduler as feed_scheduler
from .feeds.odds_api import register_odds_feeds
from .feeds.weather import register_weather_feeds
from .orchestrator import pipeline_event_hub, run_cycle, scheduler as orchestrator_scheduler
from .settlement import (
    run_cycle as run_settlement_cycle,
    scheduler as settlement_scheduler,
    settle_market,
    settlement_event_hub,
)
from .swarm import llm_log_hub, refresh_persona_weights, run_debate
from .kalshi import (
    OrderRequest,
    auth as kalshi_auth,
    bus as kalshi_bus,
    client as kalshi_client_mod,
    lifecycle as kalshi_lifecycle,
    ws as kalshi_ws,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("axiom.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("axiom-predict api starting")
    settings = get_settings()  # noqa: F841 — early validation
    if not auth_configured():
        # No Clerk publishable key means no way to verify tokens, which means
        # every protected endpoint will return 503.  Fail fast in production
        # rather than silently serving 503s; in dev, log loudly.
        is_prod = (
            os.environ.get("REPLIT_DEPLOYMENT") == "1"
            or (os.environ.get("ENV") or os.environ.get("APP_ENV") or "").lower()
            in ("prod", "production")
        )
        msg = (
            "CLERK_PUBLISHABLE_KEY is not set — operator console auth is "
            "disabled and every protected endpoint will return 503"
        )
        if is_prod:
            raise RuntimeError(msg)
        log.warning(msg)
    try:
        await db.get_pool()
    except Exception as e:
        log.error("Postgres pool init failed: %s", e)
    # Register + start the external data feed pollers.  Feeds with missing
    # API keys register themselves as `enabled=false` so they show up in
    # /api/feeds/status with a clear skipped_reason but never poll.
    try:
        register_odds_feeds()
        register_weather_feeds()
        await feed_scheduler.start()
    except Exception as e:
        log.error("feed scheduler start failed: %s", e)
    # Bootstrap persona weights from swarm_analyst_metrics so the swarm
    # is ready to vote with calibrated weights on the very first request.
    try:
        n = await refresh_persona_weights()
        log.info("swarm persona weights bootstrapped from %d rows", n)
    except Exception as e:
        log.warning("swarm persona weight bootstrap failed: %s", e)

    # Validate Kalshi credentials at startup (non-fatal — degraded mode allowed).
    cred = kalshi_auth.validate_credentials(
        settings.kalshi_api_key_id, settings.kalshi_private_key
    )
    if cred.get("ok"):
        log.info("Kalshi credentials OK (key fingerprint=%s, env=%s, mode=%s)",
                 cred.get("key_fingerprint"), settings.kalshi_env, settings.trading_mode)
    else:
        log.warning("Kalshi credentials NOT validated: %s", cred.get("error"))

    # Start WS subscriber + bus->lifecycle bridge if creds are present.
    fill_task = None
    try:
        await kalshi_ws.maybe_start_ws()
        fill_task = await kalshi_lifecycle.attach_ws_fill_handler()
    except Exception as e:
        log.warning("Kalshi WS startup skipped: %s", e)

    # Start the trading orchestrator scheduler.  Honors
    # AXIOM_ORCHESTRATOR_ENABLED + AXIOM_TRADING_MODE.  Cycle failures
    # are caught inside the loop so a sick DB doesn't stop the process.
    try:
        await orchestrator_scheduler.start()
    except Exception as e:
        log.warning("orchestrator scheduler start failed: %s", e)

    # Settlement loop: closes finalized markets, writes post-mortems,
    # updates persona Brier, accumulates daily PnL, raises drift alerts.
    # Runs independent of trading-mode halts so feedback never stalls.
    try:
        await settlement_scheduler.start()
    except Exception as e:
        log.warning("settlement scheduler start failed: %s", e)

    # Kick off model artifact reassembly in the background so a cold cache
    # (~240 MiB download) doesn't block the HTTP server from accepting
    # traffic. Health endpoints surface progress while it warms up.
    models_task = asyncio.create_task(models_loader.ensure_artifacts())

    try:
        yield
    finally:
        try:
            await settlement_scheduler.stop()
        except Exception as e:
            log.warning("settlement scheduler stop error: %s", e)
        try:
            await orchestrator_scheduler.stop()
        except Exception as e:
            log.warning("orchestrator scheduler stop error: %s", e)
        try:
            await feed_scheduler.stop()
        except Exception as e:
            log.warning("feed scheduler stop error: %s", e)
        if not models_task.done():
            models_task.cancel()
            try:
                await models_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await kalshi_ws.stop_ws()
        except Exception:
            pass
        if fill_task is not None:
            fill_task.cancel()
        await db.close_pool()
        log.info("axiom-predict api stopped")


app = FastAPI(
    title="Axiom-Predict API",
    version="0.1.0",
    description="Backend for the Axiom-Predict prediction-market trading platform.",
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_settings.cors_origins) or ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["authorization", "content-type", "accept"],
)


# ---------------------------------------------------------------------------
# Health + diagnostics
# ---------------------------------------------------------------------------


@app.get("/api/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe (always returns ok if the process is up). Public."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Billing: Stripe Checkout + Customer Portal + webhook + tier resolution
# ---------------------------------------------------------------------------


class CheckoutSessionRequest(BaseModel):
    price_id: str = Field(..., min_length=1, max_length=200)


@app.get("/api/billing/pricing")
async def billing_pricing() -> dict[str, object]:
    """Public — the pricing-page renders against this without needing auth."""
    return billing.public_pricing_catalog()


@app.get("/api/me/subscription")
async def me_subscription(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    sub = await billing.get_user_subscription(user)
    return sub.as_dict()


@app.post("/api/billing/checkout-session")
async def billing_checkout_session(
    body: CheckoutSessionRequest,
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    return await billing.create_checkout_session(user, body.price_id)


@app.post("/api/billing/portal-session")
async def billing_portal_session(
    user: AuthedUser = Depends(require_auth),
) -> dict[str, object]:
    return await billing.create_portal_session(user)


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request) -> dict[str, object]:
    """Stripe webhook receiver.  Signature-verified, idempotent."""
    return await billing.handle_webhook(request)


@app.get("/api/me/access")
async def me_access(user: AuthedUser = Depends(require_auth)) -> dict[str, object]:
    """Report whether the signed-in user is on the operator allow-list.

    Authenticated but unapproved users hit this endpoint to detect that
    they should see the "awaiting approval" screen instead of the
    dashboard (which would otherwise just throw 403s).
    """
    approved, email = await check_approval(user)
    return {
        "approved": approved,
        "email": email,
        "user_id": user.user_id,
    }


@app.get("/api/health")
async def health_full(_: object = Depends(require_approved_user)) -> dict[str, object]:
    """Full readiness check across DB and Storage."""
    settings = get_settings()
    out: dict[str, object] = {
        "status": "ok",
        "supabase_project": settings.supabase_url,
        "paper_mode": settings.paper_mode,
        "kalshi_env": settings.kalshi_env,
        "secrets_present": {
            "openai": bool(settings.openai_api_key),
            "anthropic": bool(settings.anthropic_api_key),
            "kalshi": bool(settings.kalshi_api_key_id and settings.kalshi_private_key),
        },
    }
    # Postgres
    try:
        ver = await db.fetchval("SELECT version()")
        n_tables = await db.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        )
        out["postgres"] = {"ok": True, "version": ver, "public_tables": int(n_tables)}
    except Exception as e:
        out["status"] = "degraded"
        out["postgres"] = {"ok": False, "error": str(e)}
    # Kalshi reachability
    out["kalshi"] = await kalshi_client_mod.health_probe()
    if not out["kalshi"].get("ok"):
        out["status"] = "degraded"
    out["trading_mode"] = settings.trading_mode
    ws = kalshi_ws.get_ws_subscriber()
    out["kalshi_ws"] = {
        "connected": bool(ws and ws.connected),
        "last_error": ws.last_error if ws else None,
        "subscribers": kalshi_bus.subscriber_count,
    }
    out["kill_switch"] = kalshi_lifecycle.kill_switch_state()
    # Storage
    try:
        entries = await storage.list_path("", limit=5)
        out["storage"] = {
            "ok": True,
            "bucket": settings.supabase_bucket,
            "sample": [{"name": e.name, "folder": e.is_folder} for e in entries],
        }
    except Exception as e:
        out["status"] = "degraded"
        out["storage"] = {"ok": False, "error": str(e)}
    # External data feeds — render a compact summary so /api/health stays
    # one screen.  Full per-feed detail lives at /api/feeds/status.
    try:
        snap = feed_registry.snapshot()
        any_failing = any(
            (s.get("failure_count") or 0) > 0
            and (s.get("last_ok_at") is None
                 or (s.get("last_error_at") or "") > (s.get("last_ok_at") or ""))
            for s in snap
        )
        out["feeds"] = {
            "ok": not any_failing,
            "registered": len(snap),
            "enabled": sum(1 for s in snap if s.get("enabled")),
            "skipped": sum(1 for s in snap if not s.get("enabled")),
            # Per-feed detail (last poll time + last error) so an operator
            # can read /api/health alone and see *which* feed is sick;
            # /api/feeds/status carries the same info plus durations and
            # success/failure counters.
            "items": [
                {
                    "name": s["name"],
                    "enabled": s["enabled"],
                    "skipped_reason": s.get("skipped_reason"),
                    "last_started_at": s.get("last_started_at"),
                    "last_finished_at": s.get("last_finished_at"),
                    "last_ok_at": s.get("last_ok_at"),
                    "last_error_at": s.get("last_error_at"),
                    "last_error": s.get("last_error"),
                    "last_records_written": s.get("last_records_written"),
                    "rate_limited_until": s.get("rate_limited_until"),
                    "rate_limited_count": s.get("rate_limited_count"),
                }
                for s in snap
            ],
        }
        if any_failing:
            out["status"] = "degraded"
    except Exception as e:
        out["feeds"] = {"ok": False, "error": str(e)}
    # Models — startup smoke test: report ready/in-progress/error, the
    # predictor catalog versions, the canonical-predictor load results
    # (each model is actually deserialized at boot), and disk usage.
    state = models_loader.get_state()
    canonical_ok = sum(1 for v in state.loaded.values() if v.get("ok"))
    out["models"] = {
        "ok": state.ready and state.error is None and canonical_ok > 0,
        "ready": state.ready,
        "in_progress": state.in_progress,
        "error": state.error,
        "elapsed_seconds": (
            round(state.completed_at - state.started_at, 2)
            if state.started_at and state.completed_at else None
        ),
        "predictor_count": len(state.predictors),
        "loaded_canonical": state.loaded,
        "loaded_canonical_ok": canonical_ok,
        "versions": models_loader.list_predictor_versions(),
        "disk_usage": models_loader.disk_usage_summary(),
    }
    if not state.ready and state.error:
        out["status"] = "degraded"
    if state.ready and canonical_ok == 0:
        out["status"] = "degraded"
    return out


@app.get("/api/feeds/status")
async def feeds_status(_: object = Depends(require_auth)) -> dict[str, object]:
    """Per-feed liveness snapshot for the operator console."""
    return {"items": feed_registry.snapshot()}


# ---------------------------------------------------------------------------
# Dashboard read endpoints
# ---------------------------------------------------------------------------


@app.get("/api/dashboard/overview")
async def dashboard_overview(_: object = Depends(require_approved_user)) -> dict[str, object]:
    return await dashboard.get_overview()


@app.get("/api/dashboard/recent-trades")
async def dashboard_recent_trades(
    limit: int = 25, _: object = Depends(require_approved_user)
) -> dict[str, object]:
    limit = max(1, min(200, int(limit)))
    return {"items": await dashboard.get_recent_trades(limit)}


@app.get("/api/dashboard/swarm-personas")
async def dashboard_swarm_personas(
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    return {"items": await dashboard.get_swarm_personas()}


@app.get("/api/dashboard/recent-cycles")
async def dashboard_recent_cycles(
    limit: int = 10, _: object = Depends(require_approved_user)
) -> dict[str, object]:
    limit = max(1, min(100, int(limit)))
    return {"items": await dashboard.get_recent_cycles(limit)}


@app.get("/api/dashboard/recent-debates")
async def dashboard_recent_debates(
    limit: int = 10,
    _: object = Depends(billing.require_tier(billing.TIER_PRO)),
) -> dict[str, object]:
    """Full LLM swarm transcripts — gated to Pro+ subscribers (founders bypass)."""
    limit = max(1, min(100, int(limit)))
    return {"items": await dashboard.get_recent_swarm_debates(limit)}


# ---------------------------------------------------------------------------
# Swarm: 6-persona LLM debate + gatekeeper
# ---------------------------------------------------------------------------


class SwarmDebateRequest(BaseModel):
    game_id: str = Field(..., min_length=1, max_length=128)
    matchup: str | None = None
    league: str | None = None
    market_type: str | None = "moneyline"
    side: str | None = "YES"
    current_price: float | None = None
    context: str | None = None
    # Aggression-mode bias: conservative | balanced | aggressive.  Drives
    # both the moderator's edge/agreement thresholds and the gatekeeper's
    # veto criteria; see app/swarm/personas.py.
    aggression_mode: str | None = "balanced"


@app.post("/api/swarm/debate")
async def swarm_debate(
    body: SwarmDebateRequest,
    _: object = Depends(billing.require_tier(billing.TIER_PRO)),
) -> dict[str, object]:
    settings = get_settings()
    if not (settings.openai_api_key and settings.anthropic_api_key):
        raise HTTPException(
            status_code=503,
            detail=(
                "swarm requires both OPENAI_API_KEY and ANTHROPIC_API_KEY; "
                f"openai={bool(settings.openai_api_key)} "
                f"anthropic={bool(settings.anthropic_api_key)}"
            ),
        )
    try:
        return await run_debate(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("swarm debate failed")
        raise HTTPException(status_code=502, detail=f"swarm error: {e}")


@app.websocket("/api/ws/llm-logs")
async def ws_llm_logs(ws: WebSocket) -> None:
    """Live stream of swarm events: persona starts/deltas/verdicts,
    moderator/gatekeeper outcomes, and final debate completion.

    Auth: Clerk JWT passed via the `?token=` query param (browsers cannot
    set Authorization headers on WebSocket upgrades).  When auth is not
    configured (dev mode), the socket is allowed open.
    """
    from .auth import ws_authenticate  # local import to avoid cycle on boot

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    q = llm_log_hub.subscribe()
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws send failed: %s", e)
    finally:
        llm_log_hub.unsubscribe(q)


# ---------------------------------------------------------------------------
# Trading orchestrator: cycle scheduler + 12 risk gates + paper lifecycle
# ---------------------------------------------------------------------------


class OrchestratorRunCycleRequest(BaseModel):
    vertical: str = Field("sports", min_length=1, max_length=64)


@app.get("/api/orchestrator/status")
async def orchestrator_status(_: object = Depends(require_auth)) -> dict[str, object]:
    return orchestrator_scheduler.status()


@app.post("/api/orchestrator/run-cycle")
async def orchestrator_run_cycle(
    body: OrchestratorRunCycleRequest,
    _: object = Depends(billing.require_tier(billing.TIER_PRO)),
) -> dict[str, object]:
    """Manually fire a single cycle.  Useful for ops + smoke testing
    without waiting for the next scheduler tick."""
    try:
        cr = await run_cycle(body.vertical)
    except Exception as e:
        log.exception("manual cycle failed")
        raise HTTPException(status_code=502, detail=f"cycle error: {e}")
    return {
        "cycle_id": cr.cycle_id,
        "vertical": cr.vertical,
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "picks_evaluated": cr.picks_evaluated,
        "trades_placed": cr.trades_placed,
        "fresh_debates": cr.fresh_debates,
        "errors": cr.errors,
    }


@app.websocket("/api/ws/pipeline")
async def ws_pipeline(ws: WebSocket) -> None:
    """Live stream of orchestrator pipeline events: cycle_start, gate
    pass/fail, candidate_complete (placed/rejected/place_error),
    cycle_complete.  Auth via Clerk JWT in `?token=` (same contract as
    `/api/ws/llm-logs` since browsers can't set Authorization on WS upgrades).
    """
    from .auth import ws_authenticate

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    q = pipeline_event_hub.subscribe()
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws pipeline send failed: %s", e)
    finally:
        pipeline_event_hub.unsubscribe(q)


# ---------------------------------------------------------------------------
# Settlement endpoints
# ---------------------------------------------------------------------------


class SettlementSettleMarketRequest(BaseModel):
    market_id: str = Field(..., min_length=1, max_length=128)
    outcome_yes: int = Field(..., ge=0, le=1)
    settle_price: float | None = Field(None, ge=0.0, le=100.0)


@app.get("/api/settlement/status")
async def settlement_status(_: object = Depends(require_auth)) -> dict[str, object]:
    return settlement_scheduler.status()


@app.post("/api/settlement/run-cycle")
async def settlement_run_cycle(_: object = Depends(require_auth)) -> dict[str, object]:
    """Manually trigger one settlement sweep."""
    try:
        cr = await run_settlement_cycle()
    except Exception as e:
        log.exception("manual settlement cycle failed")
        raise HTTPException(status_code=502, detail=f"settlement cycle error: {e}")
    return {
        "started_at": cr.started_at,
        "completed_at": cr.completed_at,
        "duration_seconds": cr.duration_seconds,
        "markets_examined": cr.markets_examined,
        "markets_settled": cr.markets_settled,
        "trades_settled": cr.trades_settled,
        "persona_metrics_updated": cr.persona_metrics_updated,
        "alerts_raised": cr.alerts_raised,
        "errors": cr.errors,
    }


@app.post("/api/settlement/settle-market")
async def settlement_settle_market(
    body: SettlementSettleMarketRequest, _: object = Depends(require_auth)
) -> dict[str, object]:
    """Manual / backfill settlement of a specific market.  Used by ops
    when Kalshi's `/markets/{ticker}` lags or for paper-mode markets the
    exchange never resolves."""
    try:
        result = await settle_market(
            body.market_id,
            outcome_yes=body.outcome_yes,
            settle_price=body.settle_price,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("manual settle_market failed")
        raise HTTPException(status_code=502, detail=str(e))
    return {k: v for k, v in result.items() if k != "details"}


@app.websocket("/api/ws/settlement")
async def ws_settlement(ws: WebSocket) -> None:
    """Live stream of settlement events: cycle_start, market_settle_start,
    trade_settled, market_settle_complete, cycle_complete.  Auth via
    Clerk JWT in `?token=` (same contract as `/api/ws/pipeline`).
    """
    from .auth import ws_authenticate

    user = await ws_authenticate(ws)
    if user is None and auth_configured():
        return
    q = settlement_event_hub.subscribe()
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws settlement send failed: %s", e)
    finally:
        settlement_event_hub.unsubscribe(q)


# ---------------------------------------------------------------------------
# Kalshi exchange endpoints
# ---------------------------------------------------------------------------


def _kalshi_error_to_http(e: Exception) -> HTTPException:
    if isinstance(e, kalshi_client_mod.KalshiAPIError):
        status = 502 if e.status == 0 else min(max(e.status, 400), 599)
        return HTTPException(status_code=status, detail={"kalshi_status": e.status, "body": e.body})
    if isinstance(e, kalshi_auth.KalshiAuthError):
        return HTTPException(status_code=503, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@app.get("/api/kalshi/balance")
async def kalshi_balance(_: object = Depends(require_auth)) -> dict[str, object]:
    try:
        async with kalshi_client_mod.KalshiClient() as c:
            bal = await c.get_balance()
    except Exception as e:
        raise _kalshi_error_to_http(e)
    return {
        "balance_cents": bal.balance_cents,
        "balance_dollars": bal.balance_dollars,
        "payout_cents": bal.payout_cents,
        "raw": bal.raw,
    }


@app.get("/api/kalshi/positions")
async def kalshi_positions(limit: int = 100, _: object = Depends(require_auth)) -> dict[str, object]:
    limit = max(1, min(500, int(limit)))
    try:
        async with kalshi_client_mod.KalshiClient() as c:
            pos = await c.get_positions(limit=limit)
    except Exception as e:
        raise _kalshi_error_to_http(e)
    return {
        "items": [
            {
                "market_ticker": p.market_ticker,
                "position": p.position,
                "market_exposure_cents": p.market_exposure_cents,
                "realized_pnl_cents": p.realized_pnl_cents,
                "fees_paid_cents": p.fees_paid_cents,
            }
            for p in pos
        ],
        "count": len(pos),
    }


@app.get("/api/kalshi/markets")
async def kalshi_markets(
    limit: int = 50, status: str = "open", _: object = Depends(require_auth)
) -> dict[str, object]:
    try:
        async with kalshi_client_mod.KalshiClient() as c:
            data = await c.list_markets(limit=max(1, min(200, limit)), status=status or None)
    except Exception as e:
        raise _kalshi_error_to_http(e)
    return data


@app.get("/api/kalshi/orders/resting")
async def kalshi_resting_orders(_: object = Depends(require_auth)) -> dict[str, object]:
    rows = await kalshi_lifecycle.list_resting_orders()
    return {"items": [r.to_dict() for r in rows], "count": len(rows)}


@app.post("/api/kalshi/paper-order")
async def kalshi_paper_order(
    payload: dict, _: object = Depends(require_auth)
) -> dict[str, object]:
    """Submit a paper order via the lifecycle. Always paper, even when
    AXIOM_TRADING_MODE=live, so this endpoint is safe to expose to operators.
    """
    try:
        req = OrderRequest(
            market_ticker=str(payload.get("market_ticker") or payload.get("ticker") or "").strip(),
            side=str(payload.get("side") or "yes").lower(),  # type: ignore[arg-type]
            action=str(payload.get("action") or "buy").lower(),  # type: ignore[arg-type]
            count=int(payload.get("count") or payload.get("contracts") or 1),
            limit_price_cents=(int(payload["limit_price_cents"])
                               if payload.get("limit_price_cents") is not None else None),
            type=str(payload.get("type") or "limit"),  # type: ignore[arg-type]
            time_in_force=str(payload.get("time_in_force") or "GTC"),  # type: ignore[arg-type]
            vertical=str(payload.get("vertical") or "sports"),
            context=dict(payload.get("context") or {}),
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"bad order payload: {e}")
    if not req.market_ticker:
        raise HTTPException(status_code=422, detail="market_ticker is required")
    try:
        rec = await kalshi_lifecycle.submit_order(req, force_paper=True)
    except kalshi_lifecycle.OrderRejected as e:
        raise HTTPException(status_code=409, detail=str(e))
    return rec.to_dict()


@app.post("/api/kalshi/halt")
async def kalshi_halt(reason: str | None = None) -> dict[str, object]:
    """Trip the kill switch. Intentionally NO middleware deps — operators must
    be able to halt trading even when downstream auth is broken.
    """
    return kalshi_lifecycle.halt(reason)


@app.post("/api/kalshi/resume")
async def kalshi_resume(_: object = Depends(require_auth)) -> dict[str, object]:
    return kalshi_lifecycle.resume()


@app.get("/api/kalshi/kill-switch")
async def kalshi_kill_state(_: object = Depends(require_auth)) -> dict[str, object]:
    return kalshi_lifecycle.kill_switch_state()


@app.websocket("/api/ws/kalshi")
async def kalshi_event_stream(websocket: WebSocket):  # pragma: no cover - covered by manual integration
    """Stream the in-process Kalshi event bus to a connected client.

    Auth: pass a Clerk session JWT as the `?token=` query parameter (browsers
    can't set headers on the WS upgrade, so the query parameter is the
    practical path). Verified with the same JWKS used by `require_auth`.
    """
    from .auth import ws_authenticate

    user = await ws_authenticate(websocket)
    if user is None and auth_configured():
        return
    q = await kalshi_bus.subscribe()
    try:
        while True:
            msg = await q.get()
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws/kalshi error: %s", e)
    finally:
        await kalshi_bus.unsubscribe(q)


# ---------------------------------------------------------------------------
# Storage / model artifact discovery
# ---------------------------------------------------------------------------


@app.get("/api/storage/list")
async def storage_list(
    prefix: str = "",
    limit: int = 50,
    offset: int = 0,
    _: object = Depends(require_approved_user),
) -> dict[str, object]:
    try:
        entries = await storage.list_path(prefix, limit=limit, offset=offset)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"storage error: {e}")
    return {
        "prefix": prefix,
        "items": [
            {"name": e.name, "is_folder": e.is_folder, "size": e.size, "updated_at": e.updated_at}
            for e in entries
        ],
    }


@app.get("/api/models/status")
async def models_status(_: object = Depends(require_auth)) -> dict[str, object]:
    """Detailed loader state: archives, predictor catalog, disk usage."""
    state = models_loader.get_state()
    return {
        **state.as_dict(),
        "disk_usage": models_loader.disk_usage_summary(),
    }


@app.post("/api/models/reload")
async def models_reload(_: object = Depends(require_auth)) -> dict[str, object]:
    """Force a full reload: clears the in-memory state + cached predictor
    objects and re-runs the reassemble/extract/scan/smoke-load pipeline.
    Cached on-disk archives are reused if intact."""
    asyncio.create_task(models_loader.ensure_artifacts(force=True))
    return {"started": True, "state": models_loader.get_state().as_dict()}


@app.get("/api/models/inventory")
async def models_inventory(_: object = Depends(require_approved_user)) -> dict[str, object]:
    """Report the artifact archives present in the storage bucket.

    The production bucket layout is:
        exports/models_part_{aa,ab,ac}   trained-model archive (multi-part)
        exports/source_part_{aa,ab,ac}   full Python source archive (multi-part)
        cutover/                         cutover scripts
    """
    try:
        roots = await storage.list_path("", limit=200)
        exports = await storage.list_path("exports", limit=500)
        cutover = await storage.list_path("cutover", limit=200)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"storage error: {e}")

    def total(items, prefix):
        return sum((it.size or 0) for it in items if it.name.startswith(prefix))

    return {
        "buckets": [{"name": e.name, "is_folder": e.is_folder} for e in roots],
        "exports": {
            "files": [
                {"name": e.name, "size": e.size, "updated_at": e.updated_at}
                for e in exports if not e.is_folder
            ],
            "models_archive_total_bytes": total(exports, "models_part_"),
            "source_archive_total_bytes": total(exports, "source_part_"),
        },
        "cutover": [
            {"name": e.name, "size": e.size, "updated_at": e.updated_at}
            for e in cutover if not e.is_folder
        ],
    }
