"""Axiom-Predict FastAPI application — foundation slice.

This is the minimum viable backend that:
  * Connects to the existing Supabase Postgres (149-table production schema)
  * Connects to Supabase Storage (axiom-predict-artifacts bucket)
  * Exposes `/api/healthz` and read-only dashboard endpoints
  * Reports which trained models are present in storage
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import dashboard, db, storage
from .config import get_settings


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def require_api_token(
    authorization: str | None = Header(default=None),
    x_api_token: str | None = Header(default=None, alias="X-API-Token"),
) -> None:
    """Gate protected endpoints behind AXIOM_API_TOKEN.

    Accepts the token via either `Authorization: Bearer <token>` or the
    `X-API-Token` header.  When `AXIOM_API_TOKEN` is unset the gate is
    open (dev convenience only — production deployments MUST set it).

    No "same-origin" or referer-based bypass exists by design: those are
    trivially forgeable by any non-browser client.  The bundled web
    artifact is expected to inject the token via the X-API-Token header
    using `setAuthTokenGetter` in lib/api-client-react.
    """
    settings = get_settings()
    expected = settings.api_token
    if not expected:
        return
    presented: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization.split(" ", 1)[1].strip()
    elif x_api_token:
        presented = x_api_token.strip()
    if presented and _constant_time_eq(presented, expected):
        return
    raise HTTPException(status_code=401, detail="missing or invalid API token")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("axiom.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("axiom-predict api starting")
    try:
        await db.get_pool()
    except Exception as e:
        log.error("Postgres pool init failed: %s", e)
    yield
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
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health + diagnostics
# ---------------------------------------------------------------------------


@app.get("/api/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe (always returns ok if the process is up)."""
    return {"status": "ok"}


@app.get("/api/health", dependencies=[Depends(require_api_token)])
async def health_full() -> dict[str, object]:
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
    return out


# ---------------------------------------------------------------------------
# Dashboard read endpoints
# ---------------------------------------------------------------------------


@app.get("/api/dashboard/overview", dependencies=[Depends(require_api_token)])
async def dashboard_overview() -> dict[str, object]:
    return await dashboard.get_overview()


@app.get("/api/dashboard/recent-trades", dependencies=[Depends(require_api_token)])
async def dashboard_recent_trades(limit: int = 25) -> dict[str, object]:
    limit = max(1, min(200, int(limit)))
    return {"items": await dashboard.get_recent_trades(limit)}


@app.get("/api/dashboard/swarm-personas", dependencies=[Depends(require_api_token)])
async def dashboard_swarm_personas() -> dict[str, object]:
    return {"items": await dashboard.get_swarm_personas()}


@app.get("/api/dashboard/recent-cycles", dependencies=[Depends(require_api_token)])
async def dashboard_recent_cycles(limit: int = 10) -> dict[str, object]:
    limit = max(1, min(100, int(limit)))
    return {"items": await dashboard.get_recent_cycles(limit)}


@app.get("/api/dashboard/recent-debates", dependencies=[Depends(require_api_token)])
async def dashboard_recent_debates(limit: int = 10) -> dict[str, object]:
    limit = max(1, min(100, int(limit)))
    return {"items": await dashboard.get_recent_swarm_debates(limit)}


# ---------------------------------------------------------------------------
# Storage / model artifact discovery
# ---------------------------------------------------------------------------


@app.get("/api/storage/list", dependencies=[Depends(require_api_token)])
async def storage_list(prefix: str = "", limit: int = 50, offset: int = 0) -> dict[str, object]:
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


@app.get("/api/models/inventory", dependencies=[Depends(require_api_token)])
async def models_inventory() -> dict[str, object]:
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
