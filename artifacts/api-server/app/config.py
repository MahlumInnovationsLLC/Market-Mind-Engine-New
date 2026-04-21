"""Runtime configuration loaded from environment.

Critical aliases:
- The user has `OPENAI_APY_KEY` (typo) set in Replit Secrets. We read it
  as-is and expose it under `openai_api_key` rather than asking them to rename.
"""
from __future__ import annotations

import os
from functools import lru_cache
from dataclasses import dataclass
from urllib.parse import quote as _urlquote


SUPABASE_PROJECT_REF = "frwhxftngijwcujqhltf"
SUPABASE_BUCKET = "axiom-predict-artifacts"


@dataclass(frozen=True)
class Settings:
    # Supabase
    supabase_db_password: str
    supabase_service_key: str
    supabase_url: str
    supabase_db_dsn: str
    supabase_storage_url: str
    supabase_bucket: str

    # AI keys (note APY typo in user's secrets)
    openai_api_key: str | None
    anthropic_api_key: str | None

    # Trading
    kalshi_api_key_id: str | None
    kalshi_private_key: str | None
    kalshi_env: str
    kalshi_api_base_url: str
    kalshi_ws_url: str

    # Operating mode
    paper_mode: bool
    # AXIOM_TRADING_MODE = "paper" (default) | "live" | "halt"
    # When "live", real orders are sent to Kalshi. "halt" lets the
    # orchestrator loop run but never place trades — useful for diagnostics.
    # The trading orchestrator is forbidden from setting this to "live"
    # until the risk-gate task ships.
    trading_mode: str
    # Master switch for the orchestrator scheduler.  When false, no
    # cycle tasks are started at boot.
    orchestrator_enabled: bool
    # Per-vertical cycle cadence in seconds.  Spec §6 calls for ~120s.
    cycle_interval_seconds: int
    # Max fresh swarm debates per cycle, to bound LLM cost.  Cycles
    # may evaluate cached debates without limit.
    max_fresh_debates_per_cycle: int
    # Verticals the orchestrator schedules.  One asyncio task per
    # vertical, each tick runs run_cycle(vertical) which scopes
    # candidate selection to that vertical's leagues.
    orchestrator_verticals: tuple[str, ...]

    # Server
    port: int

    # CORS — comma-separated list, defaults to "*".  Auth is handled per
    # request by Clerk JWT verification (see app/auth.py); there is no
    # shared bearer token / referer shortcut.
    cors_origins: tuple[str, ...]


def _truthy(v: str | None, default: bool = True) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    pwd = os.environ.get("SUPABASE_DB_PASSWORD") or ""
    svc = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SECRET_KEY")
        or ""
    )
    if not pwd:
        raise RuntimeError("SUPABASE_DB_PASSWORD is not set")
    if not svc:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SECRET_KEY) is not set")

    project = SUPABASE_PROJECT_REF
    # URL-encode the password so reserved characters (@:/?#) don't break parsing.
    encoded_pwd = _urlquote(pwd, safe="")
    dsn = (
        f"postgresql://postgres:{encoded_pwd}@db.{project}.supabase.co:5432/postgres"
        "?sslmode=require"
    )

    cors_raw = os.environ.get("AXIOM_CORS_ORIGINS", "*")
    cors_origins = tuple(o.strip() for o in cors_raw.split(",") if o.strip())

    verticals_raw = os.environ.get("AXIOM_ORCHESTRATOR_VERTICALS", "sports")
    verticals = tuple(
        v.strip().lower() for v in verticals_raw.split(",") if v.strip()
    ) or ("sports",)

    kalshi_env = os.environ.get("KALSHI_ENV", "demo").strip().lower()
    if kalshi_env not in ("demo", "prod", "production"):
        raise RuntimeError(f"KALSHI_ENV must be 'demo' or 'prod' (got {kalshi_env!r})")
    if kalshi_env == "production":
        kalshi_env = "prod"
    kalshi_api_base_url = (
        os.environ.get("KALSHI_API_BASE_URL")
        or ("https://api.elections.kalshi.com/trade-api/v2"
            if kalshi_env == "prod"
            else "https://demo-api.kalshi.co/trade-api/v2")
    )
    kalshi_ws_url = (
        os.environ.get("KALSHI_WS_URL")
        or ("wss://api.elections.kalshi.com/trade-api/ws/v2"
            if kalshi_env == "prod"
            else "wss://demo-api.kalshi.co/trade-api/ws/v2")
    )

    trading_mode = (os.environ.get("AXIOM_TRADING_MODE") or "paper").strip().lower()
    if trading_mode not in ("paper", "live", "halt"):
        raise RuntimeError(
            f"AXIOM_TRADING_MODE must be 'paper', 'live', or 'halt' (got {trading_mode!r})"
        )

    return Settings(
        supabase_db_password=pwd,
        supabase_service_key=svc,
        supabase_url=f"https://{project}.supabase.co",
        supabase_db_dsn=dsn,
        supabase_storage_url=f"https://{project}.supabase.co/storage/v1",
        supabase_bucket=SUPABASE_BUCKET,
        openai_api_key=os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_APY_KEY"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        kalshi_api_key_id=os.environ.get("KALSHI_API_KEY_ID"),
        kalshi_private_key=os.environ.get("KALSHI_API_PRIVATE_KEY"),
        kalshi_env=kalshi_env,
        kalshi_api_base_url=kalshi_api_base_url,
        kalshi_ws_url=kalshi_ws_url,
        paper_mode=_truthy(os.environ.get("AXIOM_PAPER_MODE"), default=True),
        trading_mode=trading_mode,
        orchestrator_enabled=_truthy(
            os.environ.get("AXIOM_ORCHESTRATOR_ENABLED"), default=True
        ),
        cycle_interval_seconds=max(
            10, int(os.environ.get("AXIOM_CYCLE_INTERVAL_SECONDS") or "120")
        ),
        max_fresh_debates_per_cycle=max(
            0, int(os.environ.get("AXIOM_MAX_FRESH_DEBATES_PER_CYCLE") or "1")
        ),
        orchestrator_verticals=verticals,
        port=int(os.environ.get("PORT", "8080")),
        cors_origins=cors_origins,
    )
