"""Runtime configuration loaded from environment.

Critical aliases:
- The user has `OPENAI_APY_KEY` (typo) set in Replit Secrets. We read it
  as-is and expose it under `openai_api_key` rather than asking them to rename.
"""
from __future__ import annotations

import os
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote as _urlquote


SUPABASE_PROJECT_REF = "frwhxftngijwcujqhltf"
SUPABASE_BUCKET = "axiom-predict-artifacts"

# Default model cache lives under the workspace root (`.cache/` is
# gitignored and the workspace mount is persistent across container
# restarts on Replit, unlike `/tmp` which gets wiped). Override with
# AXIOM_MODEL_CACHE_DIR for tests or alternate deploy layouts.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_CACHE_DIR = _WORKSPACE_ROOT / ".cache" / "axiom-models"

# Default budget for the on-disk model cache (reassembled archives +
# extracted trees). The current production payload is ~1.2 GiB; 2 GiB
# leaves headroom for one extra version while still firing a warning
# well before the workspace volume fills. Override with
# AXIOM_MODEL_CACHE_MAX_BYTES.
DEFAULT_MODEL_CACHE_MAX_BYTES = 2 * (1 << 30)


def resolve_model_cache_max_bytes() -> int:
    """Resolve the model cache size budget from env/default.

    Module-level (no Settings dependency) so `models_loader` can read
    it at import time, mirroring `resolve_model_cache_dir`.
    """
    raw = os.environ.get("AXIOM_MODEL_CACHE_MAX_BYTES")
    if raw is None or not raw.strip():
        return DEFAULT_MODEL_CACHE_MAX_BYTES
    try:
        val = int(raw.strip())
    except ValueError as e:
        raise RuntimeError(
            f"AXIOM_MODEL_CACHE_MAX_BYTES must be an integer (got {raw!r})"
        ) from e
    if val <= 0:
        raise RuntimeError(
            f"AXIOM_MODEL_CACHE_MAX_BYTES must be > 0 (got {val})"
        )
    return val


def resolve_model_cache_dir() -> Path:
    """Resolve the model cache directory from env/default.

    Kept module-level (and free of Settings dependencies) so
    `models_loader` can resolve the path at import time without
    requiring Supabase secrets to be present.

    Note: the cache directory is owned exclusively by the loader —
    `models_loader._prune_cache()` will delete any non-canonical
    entries it finds at the root, so always point this at a
    dedicated directory, not a shared one.
    """
    env = os.environ.get("AXIOM_MODEL_CACHE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_MODEL_CACHE_DIR


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
    # Google AI Studio key.  Accepted under either ``GEMINI_API_KEY`` (the
    # name google-genai prefers) or ``GOOGLE_API_KEY`` so operators don't
    # have to remember which variant the SDK wants.  Optional — only the
    # gatekeeper model selector reads it today.
    google_api_key: str | None
    # Ollama local/remote LLM server.  ``ollama_base_url`` defaults to the
    # canonical ``http://localhost:11434`` so a local install Just Works;
    # operators can point at a tunneled/remote box by setting OLLAMA_BASE_URL.
    # ``ollama_api_key`` is only required for tunneled deployments that
    # gate the HTTP API behind a bearer token (vanilla Ollama is anonymous).
    # ``ollama_request_timeout_s`` caps a single /api/chat call so a stalled
    # local model can't wedge a swarm cycle indefinitely.
    ollama_base_url: str
    ollama_api_key: str | None
    ollama_request_timeout_s: float

    # ----- Mac Studio cutover (task #735) -----
    # ``axiom_llm_mode`` selects how the swarm picks providers per
    # persona at startup:
    #   * ``cloud``  (default) → today's behaviour, every persona keeps
    #     its hard-coded OpenAI/Anthropic provider.
    #   * ``local``  → every persona is rewritten to ``provider="ollama"``
    #     using the model triple below; the cross-provider fallback chain
    #     is replaced with a same-provider fallback to
    #     ``ollama_model_backup`` so a primary-model outage on the local
    #     box never silently burns hosted-LLM credit.
    #   * ``hybrid`` → primary set is the ``local`` mapping but the
    #     existing cross-provider fallback chain stays, so a downed
    #     Ollama node still produces a verdict via the cheapest hosted
    #     model.
    # The triple (``ollama_model_fast`` for line-movement / form /
    # public-fade / matchup / moderator personas, ``ollama_model_reasoning``
    # for injury / risk / gatekeeper personas, and
    # ``ollama_model_backup`` for the per-persona fallback model) is
    # consumed by ``app.swarm.personas._apply_llm_mode``.  Defaults
    # match the audit's Mac Studio runbook (qwen2.5:14b /
    # llama3.3:70b / deepseek-v2:16b).  Omitting any of these env vars
    # keeps today's exact ``cloud`` behaviour.
    axiom_llm_mode: str
    ollama_model_fast: str
    ollama_model_reasoning: str
    ollama_model_backup: str

    # Trading
    kalshi_api_key_id: str | None
    kalshi_private_key: str | None
    kalshi_env: str
    kalshi_api_base_url: str
    kalshi_ws_url: str

    # Polymarket (CLOB) — credentials are L2 API keys (key/secret/passphrase)
    # plus the wallet address that owns them. All optional: missing creds
    # put the client in degraded mode, identical to Kalshi.
    polymarket_api_key: str | None
    polymarket_api_secret: str | None
    polymarket_api_passphrase: str | None
    polymarket_wallet_address: str | None
    polymarket_api_base_url: str
    polymarket_ws_url: str

    # Venue routing for the orchestrator's submission step.
    # AXIOM_VENUE_ROUTING = "kalshi" (default) | "polymarket" | "both" | "auto"
    # See app.orchestrator.venue_router for the per-candidate decision rules.
    venue_routing: str

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
    # Settlement pipeline: master switch + cadence (default 60s).
    settlement_enabled: bool
    settlement_interval_seconds: int

    # Pre-submit slippage guard (see app.kalshi.lifecycle): when an order
    # request carries a model_edge_cents value, the lifecycle compares the
    # WS-cached worst-case fill price against the limit price.  If the
    # projected slippage erases more than this percentage of the model
    # edge, the trade is rejected with reason=`slippage_guard`.  Set to
    # 0 to disable.  Default 50%: half the edge gone to slippage = nope.
    slippage_guard_max_edge_pct: float

    # Maximum age (seconds) of a cached Kalshi orderbook snapshot before
    # the lifecycle slippage guard considers it stale. When stale (or
    # missing entirely), the guard either issues a one-shot REST
    # fallback (see ``kalshi_orderbook_rest_fallback_enabled``) or
    # falls through to degraded-allow. Default 5s — WS pushes deltas
    # within ~1s in steady state.  Set to 0 to disable the freshness
    # check (any cached snapshot, no matter how old, counts as fresh).
    kalshi_orderbook_max_age_secs: float
    # When True the slippage guard fires a single ``GET
    # /markets/{ticker}/orderbook`` REST call to repopulate the cache
    # whenever the WS view is stale or missing. Disable to fall back
    # to the previous degraded-allow behaviour (no REST call, guard
    # skipped). Default True.
    kalshi_orderbook_rest_fallback_enabled: bool
    # When True the slippage guard treats an order whose *requested side*
    # has no resting liquidity (the book snapshot exists but that side is
    # empty) as allowed — the order is submitted as a resting limit at the
    # already-edge-capped limit price rather than hard-rejected with
    # "no liquidity on requested side".  This unblocks thin sports / tennis
    # / weather books that have no standing asks at decision time; the limit
    # price still caps overpay, so the worst case is an unfilled resting
    # order, never a bad fill.  Set False to restore the old hard-reject.
    # Default True.
    slippage_guard_allow_empty_side: bool
    # When True the slippage guard does NOT trim a *limit* order on a thin /
    # partial-depth book.  A limit order is self-protecting: every fill is
    # bounded by its own limit price (buy fills <= limit, sell fills >=
    # limit) and the limit already sits inside the edge-erosion budget
    # (worst_acceptable = limit +/- budget), so the order can never fill
    # outside the budget — the unfilled remainder simply rests.  Trimming it
    # to the immediately-fillable slice needlessly shrinks Kelly-sized orders
    # to a sub-dollar position on thin sports / tennis books.  Set False to
    # restore the old behaviour (trim to the fillable slice).  Default True.
    slippage_guard_rest_full_limit: bool

    # Verticals the orchestrator schedules.  One asyncio task per
    # vertical, each tick runs run_cycle(vertical) which scopes
    # candidate selection to that vertical's leagues.
    orchestrator_verticals: tuple[str, ...]

    # Model artifact cache (persistent across container restarts).
    # Populated from AXIOM_MODEL_CACHE_DIR or DEFAULT_MODEL_CACHE_DIR.
    model_cache_dir: Path
    # Soft size budget for the cache. Boot warns when exceeded; surfaced
    # via /api/health.models.disk_usage. Override with
    # AXIOM_MODEL_CACHE_MAX_BYTES (bytes).
    model_cache_max_bytes: int

    # ed25519 public key (base64-encoded raw 32 bytes) used to verify the
    # detached signature on `models_manifest.json`. The matching private
    # key is held offline by the model build pipeline -- a leaked
    # storage-write key alone is therefore not enough to substitute a
    # tampered manifest. When unset the loader still verifies sha256s
    # but logs a warning that signature enforcement is disabled.
    manifest_public_key: str | None

    # When True, a HTTP 400/404 from the storage bucket while fetching
    # `models_manifest.json` is translated to a non-fatal "unconfigured"
    # state (empty predictors, no error) so the READY gate can still
    # open and the orchestrator can run without ML predictions. Intended
    # for the T007 deferral period when no model artifacts have been
    # published yet. Default False (strict): once a manifest IS expected
    # in the bucket, a 400/404 becomes a real error so an operator
    # mistake (wrong bucket / path / permissions) does NOT silently
    # appear as "unconfigured". Toggle via AXIOM_ALLOW_MISSING_MANIFEST.
    models_allow_missing_manifest: bool

    # Telegram concierge bot. When unset the long-poll loop stays asleep
    # (dev / CI default). Per task #111.
    telegram_bot_token: str | None

    # LLM provider fallback-rate spike alert (task #145). The swarm
    # transparently fails over from each persona's primary provider to
    # its configured backup; a sustained outage on one provider would
    # otherwise be invisible because debates still complete on the
    # backup. The monitor periodically reads the `fallback_used` /
    # `primary_provider` flags written into `llm_predictions.metadata`
    # and raises an operator alert when the per-provider fallback rate
    # crosses `_threshold_pct` over the trailing `_window_minutes`.
    # `_min_samples` guards against false positives from a single
    # transient blip; `_interval_seconds` is the check cadence; and
    # `_enabled` lets ops mute the monitor without redeploying.
    llm_fallback_alert_enabled: bool
    llm_fallback_alert_threshold_pct: float
    llm_fallback_alert_window_minutes: int
    llm_fallback_alert_min_samples: int
    llm_fallback_alert_interval_seconds: int

    # Operator identity used to stamp bot-initiated `OrderRequest`s with
    # `context["user_id"]` (task #297). Without this, per-user trade
    # confirmation emails never fire on bot-driven live fills because
    # the lifecycle's notification dispatch keys off the operator's
    # clerk_user_id. Resolution falls back to the first admin in
    # ``app_users`` when this is unset — see
    # ``app.orchestrator.paper._resolve_operator_user_id``.
    operator_user_id: str | None

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
    # Validate against the pluggable vertical registry — unknown names
    # fail loudly at boot so a typo in AXIOM_ORCHESTRATOR_VERTICALS
    # can't silently spawn a no-op scheduler task.  Imported lazily so
    # `app.config` stays importable from registry-side modules without
    # a circular dependency.
    from . import verticals as _vertical_registry

    unknown = [v for v in verticals if not _vertical_registry.has(v)]
    if unknown:
        raise RuntimeError(
            "AXIOM_ORCHESTRATOR_VERTICALS contains unknown vertical(s) "
            f"{unknown!r}; registered: {sorted(_vertical_registry.names())}"
        )

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

    venue_routing = (os.environ.get("AXIOM_VENUE_ROUTING") or "kalshi").strip().lower()
    if venue_routing not in ("kalshi", "polymarket", "both", "auto"):
        raise RuntimeError(
            "AXIOM_VENUE_ROUTING must be 'kalshi', 'polymarket', 'both', or "
            f"'auto' (got {venue_routing!r})"
        )

    axiom_llm_mode = (os.environ.get("AXIOM_LLM_MODE") or "cloud").strip().lower()
    if axiom_llm_mode not in ("cloud", "local", "hybrid"):
        raise RuntimeError(
            "AXIOM_LLM_MODE must be 'cloud', 'local', or 'hybrid' "
            f"(got {axiom_llm_mode!r})"
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
        google_api_key=(
            os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GOOGLE_GENAI_API_KEY")
        ),
        ollama_base_url=(
            (os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434").strip()
        ),
        ollama_api_key=os.environ.get("OLLAMA_API_KEY") or None,
        ollama_request_timeout_s=float(
            os.environ.get("OLLAMA_REQUEST_TIMEOUT_S") or "120"
        ),
        axiom_llm_mode=axiom_llm_mode,
        ollama_model_fast=(
            (os.environ.get("OLLAMA_MODEL_FAST") or "qwen2.5:14b").strip()
        ),
        ollama_model_reasoning=(
            (os.environ.get("OLLAMA_MODEL_REASONING") or "llama3.3:70b").strip()
        ),
        ollama_model_backup=(
            (os.environ.get("OLLAMA_MODEL_BACKUP") or "deepseek-v2:16b").strip()
        ),
        kalshi_api_key_id=os.environ.get("KALSHI_API_KEY_ID"),
        kalshi_private_key=os.environ.get("KALSHI_API_PRIVATE_KEY"),
        kalshi_env=kalshi_env,
        kalshi_api_base_url=kalshi_api_base_url,
        kalshi_ws_url=kalshi_ws_url,
        polymarket_api_key=os.environ.get("POLYMARKET_API_KEY"),
        polymarket_api_secret=os.environ.get("POLYMARKET_API_SECRET"),
        polymarket_api_passphrase=os.environ.get("POLYMARKET_API_PASSPHRASE"),
        polymarket_wallet_address=os.environ.get("POLYMARKET_WALLET_ADDRESS"),
        polymarket_api_base_url=(
            os.environ.get("POLYMARKET_API_BASE_URL") or "https://clob.polymarket.com"
        ).rstrip("/"),
        polymarket_ws_url=(
            os.environ.get("POLYMARKET_WS_URL") or "wss://ws-subscriptions-clob.polymarket.com/ws/"
        ),
        venue_routing=venue_routing,
        paper_mode=_truthy(os.environ.get("AXIOM_PAPER_MODE"), default=True),
        trading_mode=trading_mode,
        orchestrator_enabled=_truthy(
            os.environ.get("AXIOM_ORCHESTRATOR_ENABLED"), default=True
        ),
        cycle_interval_seconds=max(
            10, int(os.environ.get("AXIOM_CYCLE_INTERVAL_SECONDS") or "300")
        ),
        max_fresh_debates_per_cycle=max(
            0, int(os.environ.get("AXIOM_MAX_FRESH_DEBATES_PER_CYCLE") or "1")
        ),
        orchestrator_verticals=verticals,
        settlement_enabled=_truthy(
            os.environ.get("AXIOM_SETTLEMENT_ENABLED"), default=True
        ),
        settlement_interval_seconds=max(
            10, int(os.environ.get("AXIOM_SETTLEMENT_INTERVAL_SECONDS") or "180")
        ),
        slippage_guard_max_edge_pct=max(
            0.0, float(os.environ.get("AXIOM_SLIPPAGE_GUARD_MAX_EDGE_PCT") or "50")
        ),
        kalshi_orderbook_max_age_secs=max(
            0.0, float(os.environ.get("AXIOM_KALSHI_ORDERBOOK_MAX_AGE_SECS") or "5")
        ),
        kalshi_orderbook_rest_fallback_enabled=_truthy(
            os.environ.get("AXIOM_KALSHI_ORDERBOOK_REST_FALLBACK"), default=True
        ),
        slippage_guard_allow_empty_side=_truthy(
            os.environ.get("AXIOM_SLIPPAGE_GUARD_ALLOW_EMPTY_SIDE"), default=True
        ),
        slippage_guard_rest_full_limit=_truthy(
            os.environ.get("AXIOM_SLIPPAGE_GUARD_REST_FULL_LIMIT"), default=True
        ),
        model_cache_dir=resolve_model_cache_dir(),
        model_cache_max_bytes=resolve_model_cache_max_bytes(),
        manifest_public_key=(os.environ.get("AXIOM_MANIFEST_PUBLIC_KEY") or "").strip() or None,
        models_allow_missing_manifest=_truthy(
            os.environ.get("AXIOM_ALLOW_MISSING_MANIFEST"), default=False
        ),
        telegram_bot_token=(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None,
        llm_fallback_alert_enabled=_truthy(
            os.environ.get("AXIOM_LLM_FALLBACK_ALERT_ENABLED"), default=True
        ),
        llm_fallback_alert_threshold_pct=max(
            0.0, float(os.environ.get("AXIOM_LLM_FALLBACK_ALERT_THRESHOLD_PCT") or "25")
        ),
        llm_fallback_alert_window_minutes=max(
            1, int(os.environ.get("AXIOM_LLM_FALLBACK_ALERT_WINDOW_MINUTES") or "15")
        ),
        llm_fallback_alert_min_samples=max(
            1, int(os.environ.get("AXIOM_LLM_FALLBACK_ALERT_MIN_SAMPLES") or "8")
        ),
        llm_fallback_alert_interval_seconds=max(
            10, int(os.environ.get("AXIOM_LLM_FALLBACK_ALERT_INTERVAL_SECONDS") or "300")
        ),
        operator_user_id=(
            (os.environ.get("AXIOM_OPERATOR_USER_ID") or "").strip() or None
        ),
        port=int(os.environ.get("PORT", "8080")),
        cors_origins=cors_origins,
    )
