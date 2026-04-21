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

    # Operating mode
    paper_mode: bool

    # Server
    port: int

    # Auth — single shared bearer/api-key for read-only telemetry endpoints.
    # When unset the API runs without auth (dev convenience); set in prod.
    api_token: str | None
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
        kalshi_env=os.environ.get("KALSHI_ENV", "demo"),
        paper_mode=_truthy(os.environ.get("AXIOM_PAPER_MODE"), default=True),
        port=int(os.environ.get("PORT", "8080")),
        api_token=os.environ.get("AXIOM_API_TOKEN") or None,
        cors_origins=cors_origins,
    )
