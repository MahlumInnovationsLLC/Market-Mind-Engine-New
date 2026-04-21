"""Axiom-Predict FastAPI application — foundation slice.

This is the minimum viable backend that:
  * Connects to the existing Supabase Postgres (149-table production schema)
  * Connects to Supabase Storage (axiom-predict-artifacts bucket)
  * Exposes `/api/healthz` (open) plus Clerk-gated dashboard endpoints
  * Reports which trained models are present in storage
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import dashboard, db, storage
from .auth import auth_configured, require_auth
from .config import get_settings
from .feeds import registry as feed_registry, scheduler as feed_scheduler
from .feeds.odds_api import register_odds_feeds
from .feeds.weather import register_weather_feeds
from .swarm import llm_log_hub, refresh_persona_weights, run_debate


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
    yield
    try:
        await feed_scheduler.stop()
    except Exception as e:
        log.warning("feed scheduler stop error: %s", e)
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
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["authorization", "content-type", "accept"],
)


# ---------------------------------------------------------------------------
# Health + diagnostics
# ---------------------------------------------------------------------------


@app.get("/api/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe (always returns ok if the process is up). Public."""
    return {"status": "ok"}


@app.get("/api/health")
async def health_full(_: object = Depends(require_auth)) -> dict[str, object]:
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
async def dashboard_overview(_: object = Depends(require_auth)) -> dict[str, object]:
    return await dashboard.get_overview()


@app.get("/api/dashboard/recent-trades")
async def dashboard_recent_trades(
    limit: int = 25, _: object = Depends(require_auth)
) -> dict[str, object]:
    limit = max(1, min(200, int(limit)))
    return {"items": await dashboard.get_recent_trades(limit)}


@app.get("/api/dashboard/swarm-personas")
async def dashboard_swarm_personas(
    _: object = Depends(require_auth),
) -> dict[str, object]:
    return {"items": await dashboard.get_swarm_personas()}


@app.get("/api/dashboard/recent-cycles")
async def dashboard_recent_cycles(
    limit: int = 10, _: object = Depends(require_auth)
) -> dict[str, object]:
    limit = max(1, min(100, int(limit)))
    return {"items": await dashboard.get_recent_cycles(limit)}


@app.get("/api/dashboard/recent-debates")
async def dashboard_recent_debates(
    limit: int = 10, _: object = Depends(require_auth)
) -> dict[str, object]:
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


@app.post("/api/swarm/debate")
async def swarm_debate(
    body: SwarmDebateRequest, _: object = Depends(require_auth)
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


@app.websocket("/ws/llm-logs")
async def ws_llm_logs(ws: WebSocket) -> None:
    """Live stream of swarm events: persona starts/deltas/verdicts,
    moderator/gatekeeper outcomes, and final debate completion.

    Auth: Clerk JWT passed via the `?token=` query param (browsers cannot
    set Authorization headers on WebSocket upgrades).  When auth is not
    configured (dev mode), the socket is allowed open.
    """
    token = ws.query_params.get("token")
    if auth_configured():
        from .auth import verify_clerk_jwt  # local import to avoid cycle on boot

        if not token:
            await ws.close(code=4401)
            return
        try:
            verify_clerk_jwt(token)
        except Exception as e:
            log.info("ws auth failed: %s", e)
            await ws.close(code=4401)
            return

    await ws.accept()
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
# Storage / model artifact discovery
# ---------------------------------------------------------------------------


@app.get("/api/storage/list")
async def storage_list(
    prefix: str = "",
    limit: int = 50,
    offset: int = 0,
    _: object = Depends(require_auth),
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


@app.get("/api/models/inventory")
async def models_inventory(_: object = Depends(require_auth)) -> dict[str, object]:
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
