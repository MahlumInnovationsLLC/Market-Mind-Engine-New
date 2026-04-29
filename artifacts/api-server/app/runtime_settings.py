"""Runtime-tunable operator knobs that aren't persona-/market-scoped.

Currently a single row: the global ``aggression_mode`` (Conservative /
Balanced / Aggressive) which scales the moderator + gatekeeper bias and
the default min-edge / Kelly fraction caps used by the risk gates.

The ``runtime_settings(key, value, updated_at, updated_by)`` table and
the companion ``system_settings`` table are owned by Alembic (revision
``0018_lazy_ddl_relocation``); this module assumes they exist. Per-key
explicit ``system_settings`` overrides (e.g. ``risk.min_edge_pp``) still
win — this module only changes the *defaults* that the gates fall back
to.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import db


log = logging.getLogger("axiom.runtime_settings")


VALID_MODES: tuple[str, ...] = ("conservative", "balanced", "aggressive")
DEFAULT_MODE = "balanced"


# ---------------------------------------------------------------------------
# Gatekeeper model selector (operator-tunable per task).
#
# The LLM gatekeeper is the last veto in front of the order pipe (see
# ``app.swarm.runner._call_persona`` for the GATEKEEPER call site).  By
# default it uses the model baked into ``personas.GATEKEEPER`` —
# ``anthropic/claude-sonnet-4-5-20250929``.  Operators can switch to any
# of the most-capable models from the providers that have a configured
# API key without redeploying:
#
#   * Anthropic   → claude-sonnet-4-5-20250929 (default; provider-pinned
#                   to the persona's baseline)
#   * OpenAI      → gpt-5
#   * Google      → gemini-2.5-pro
#
# The override is stored as two ``runtime_settings`` rows
# (``gatekeeper_provider`` and ``gatekeeper_model_id``).  Both must be
# present together; deleting them reverts to the persona default.
# ---------------------------------------------------------------------------
GATEKEEPER_PROVIDER_KEY = "gatekeeper_provider"
GATEKEEPER_MODEL_KEY = "gatekeeper_model_id"


# Catalogue of operator-selectable gatekeeper models.  Each entry is the
# *most capable* current-generation reasoning model that the relevant
# provider exposes — picked here rather than in a free-form text box so
# the dropdown stays curated and the cost / latency profile is
# predictable.  Updating this list is the only place an operator-facing
# new model needs to be added.
GATEKEEPER_MODEL_CATALOG: tuple[dict[str, str], ...] = (
    {
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-5-20250929",
        "label": "Claude Sonnet 4.5 (Anthropic)",
        "secret_env": "ANTHROPIC_API_KEY",
    },
    {
        "provider": "openai",
        "model_id": "gpt-5",
        "label": "GPT-5 (OpenAI)",
        "secret_env": "OPENAI_API_KEY",
    },
    {
        "provider": "google",
        "model_id": "gemini-2.5-pro",
        "label": "Gemini 2.5 Pro (Google)",
        "secret_env": "GEMINI_API_KEY",
    },
)


def _provider_has_key(provider: str) -> bool:
    """True when the API key for ``provider`` is configured.

    Reads from ``os.environ`` directly (rather than the lru-cached
    ``get_settings()``) so flipping a secret in the Replit UI is picked
    up on the next request without a process restart.  Catalogue's
    ``secret_env`` is the canonical name; we also accept the legacy
    ``GOOGLE_API_KEY`` alias for the Google provider.
    """
    import os

    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY", "").strip())
    if provider == "google":
        return bool(
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )
    return False


def gatekeeper_model_options() -> list[dict[str, Any]]:
    """The catalogue with an `available` flag per entry.

    Surfaces in the admin UI dropdown so operators see *why* a provider
    is greyed out (missing key) rather than getting a 400 on save.
    """
    out: list[dict[str, Any]] = []
    for entry in GATEKEEPER_MODEL_CATALOG:
        out.append(
            {
                "provider": entry["provider"],
                "model_id": entry["model_id"],
                "label": entry["label"],
                "secret_env": entry["secret_env"],
                "available": _provider_has_key(entry["provider"]),
            }
        )
    return out


def _catalog_lookup(provider: str, model_id: str) -> dict[str, str] | None:
    p = (provider or "").strip().lower()
    m = (model_id or "").strip()
    for entry in GATEKEEPER_MODEL_CATALOG:
        if entry["provider"] == p and entry["model_id"] == m:
            return entry
    return None


async def get_gatekeeper_model_override() -> tuple[str, str] | None:
    """Return ``(provider, model_id)`` if an override is set, else ``None``.

    Hot path: called on every gatekeeper invocation.  Best-effort — any
    DB hiccup falls back to the persona default rather than blocking
    trades.
    """
    try:
        rows = await db.fetch(
            """
            SELECT key, value
              FROM runtime_settings
             WHERE key IN ($1, $2)
            """,
            GATEKEEPER_PROVIDER_KEY,
            GATEKEEPER_MODEL_KEY,
        )
    except Exception as e:
        log.debug("get_gatekeeper_model_override read failed: %s", e)
        return None
    kv = {r["key"]: (r["value"] or "").strip() for r in rows}
    provider = kv.get(GATEKEEPER_PROVIDER_KEY) or ""
    model_id = kv.get(GATEKEEPER_MODEL_KEY) or ""
    if not provider or not model_id:
        return None
    if _catalog_lookup(provider, model_id) is None:
        # Stale / unsafe value — log and fall back to persona default.
        log.warning(
            "gatekeeper override %s/%s not in catalogue; using persona default",
            provider, model_id,
        )
        return None
    return provider, model_id


async def get_gatekeeper_model_state() -> dict[str, Any]:
    """Snapshot for the admin panel: current selection + catalogue."""
    override = await get_gatekeeper_model_override()
    updated_at: str | None = None
    updated_by: str | None = None
    if override is not None:
        try:
            row = await db.fetchrow(
                """
                SELECT to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS updated_at,
                       updated_by
                  FROM runtime_settings
                 WHERE key = $1
                """,
                GATEKEEPER_MODEL_KEY,
            )
            if row is not None:
                updated_at = row["updated_at"]
                updated_by = row["updated_by"]
        except Exception as e:
            log.debug("gatekeeper meta read failed: %s", e)
    # Persona default — kept in sync with `personas.GATEKEEPER` so the
    # UI can show "(default)" next to the same option the runner falls
    # back to.
    from .swarm.personas import GATEKEEPER as _GK

    return {
        "selected_provider": override[0] if override else None,
        "selected_model_id": override[1] if override else None,
        "default_provider": _GK.provider,
        "default_model_id": _GK.model_id,
        "options": gatekeeper_model_options(),
        "updated_at": updated_at,
        "updated_by": updated_by,
    }


async def set_gatekeeper_model(
    provider: str | None,
    model_id: str | None,
    *,
    actor_user_id: str | None,
    actor_email: str | None = None,
) -> dict[str, Any]:
    """Upsert (or clear) the gatekeeper override.

    Pass ``provider=None`` (or empty) to revert to the persona default.
    Otherwise both ``provider`` and ``model_id`` must match a catalogue
    entry whose API key is currently configured.  Audited under
    ``set_gatekeeper_model``.
    """
    import json as _json

    before = await get_gatekeeper_model_override()

    clearing = not provider or not model_id
    if clearing:
        async with db.acquire() as c:
            await c.execute(
                "DELETE FROM runtime_settings WHERE key IN ($1, $2)",
                GATEKEEPER_PROVIDER_KEY,
                GATEKEEPER_MODEL_KEY,
            )
        try:
            async with db.acquire() as c:
                await c.execute(
                    """
                    INSERT INTO admin_audit_log (
                        actor_user_id, actor_email,
                        target_user_id, target_email,
                        action, before_value, after_value
                    ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                    """,
                    actor_user_id or "system",
                    actor_email,
                    "system",
                    None,
                    "set_gatekeeper_model",
                    _json.dumps(
                        {
                            "provider": before[0] if before else None,
                            "model_id": before[1] if before else None,
                        }
                    ),
                    _json.dumps({"provider": None, "model_id": None}),
                )
        except Exception as e:
            log.warning("audit log insert (clear gatekeeper model) failed: %s", e)
        return await get_gatekeeper_model_state()

    p = (provider or "").strip().lower()
    m = (model_id or "").strip()
    entry = _catalog_lookup(p, m)
    if entry is None:
        raise ValueError(
            f"gatekeeper model {p}/{m} not in catalogue; "
            f"valid options: {[(e['provider'], e['model_id']) for e in GATEKEEPER_MODEL_CATALOG]}"
        )
    if not _provider_has_key(p):
        raise ValueError(
            f"{entry['secret_env']} is not configured; cannot select {p}/{m}"
        )

    async with db.acquire() as c:
        await c.execute(
            """
            INSERT INTO runtime_settings (key, value, updated_at, updated_by)
            VALUES ($1, $2, now(), $3),
                   ($4, $5, now(), $3)
            ON CONFLICT (key) DO UPDATE SET
                value      = EXCLUDED.value,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by
            """,
            GATEKEEPER_PROVIDER_KEY, p, actor_user_id,
            GATEKEEPER_MODEL_KEY, m,
        )
    try:
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id or "system",
                actor_email,
                "system",
                None,
                "set_gatekeeper_model",
                _json.dumps(
                    {
                        "provider": before[0] if before else None,
                        "model_id": before[1] if before else None,
                    }
                ),
                _json.dumps({"provider": p, "model_id": m}),
            )
    except Exception as e:
        log.warning("audit log insert (set gatekeeper model) failed: %s", e)
    return await get_gatekeeper_model_state()


# Profile values applied as gate defaults when no explicit
# `system_settings` override is present.  Mirrors the moderator /
# gatekeeper textual rules in `app/swarm/personas.py` so the LLM bias
# and the deterministic gates stay aligned.
MODE_PROFILES: dict[str, dict[str, float]] = {
    "conservative": {
        "min_edge_pp": 7.0,
        "kelly_fraction_cap": 0.15,
        "min_agreement_rate": 0.66,
        "max_confidence_band": 0.20,
    },
    "balanced": {
        "min_edge_pp": 4.0,
        "kelly_fraction_cap": 0.25,
        "min_agreement_rate": 0.50,
        "max_confidence_band": 0.30,
    },
    "aggressive": {
        "min_edge_pp": 2.0,
        "kelly_fraction_cap": 0.40,
        "min_agreement_rate": 0.40,
        "max_confidence_band": 0.35,
    },
}


def normalize_mode(mode: str | None) -> str:
    m = (mode or DEFAULT_MODE).strip().lower()
    return m if m in VALID_MODES else DEFAULT_MODE


def profile_for(mode: str | None) -> dict[str, float]:
    return dict(MODE_PROFILES[normalize_mode(mode)])


async def get_aggression_mode() -> str:
    try:
        v = await db.fetchval(
            "SELECT value FROM runtime_settings WHERE key = 'aggression_mode'"
        )
    except Exception as e:
        log.debug("get_aggression_mode read failed (best-effort): %s", e)
        return DEFAULT_MODE
    return normalize_mode(v)


async def get_trading_halted() -> bool:
    """Return the global kill-switch state.

    Stored in the same key/value `runtime_settings` table under the key
    ``trading_halted`` with value ``'true'`` / ``'false'`` (anything else
    is treated as ``False``). Surfaces in the operator dashboard pill
    (DRY-RUN state) and the trading-alerts banner.
    """
    try:
        v = await db.fetchval(
            "SELECT value FROM runtime_settings WHERE key = 'trading_halted'"
        )
    except Exception as e:
        log.debug("get_trading_halted read failed (best-effort): %s", e)
        return False
    return (v or "").strip().lower() == "true"


AGGRESSION_REVERT_KEY = "aggression_mode_revert"


def _normalize_revert_at(revert_at: Any) -> "datetime | None":
    from datetime import datetime, timezone

    if revert_at is None:
        return None
    if isinstance(revert_at, datetime):
        dt = revert_at
    elif isinstance(revert_at, str):
        s = revert_at.strip()
        if not s:
            return None
        # Accept trailing "Z" (Python <3.11 fromisoformat doesn't).
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as e:
            raise ValueError(f"invalid revert_at timestamp: {revert_at!r}") from e
    else:
        raise ValueError(f"invalid revert_at type: {type(revert_at).__name__}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if dt <= now:
        raise ValueError("revert_at must be in the future")
    return dt


async def set_aggression_mode(
    mode: str,
    *,
    actor_user_id: str | None,
    actor_email: str | None = None,
    reason: str | None = None,
    revert_at: Any = None,
    revert_to_mode: str | None = None,
) -> str:
    raw = (mode or "").strip().lower()
    if raw not in VALID_MODES:
        raise ValueError(
            f"invalid aggression_mode {mode!r}; expected one of {list(VALID_MODES)}"
        )
    cleaned_reason = (reason or "").strip()[:500] or None

    # Validate optional schedule before any DB write so a partial failure
    # can't leave us with a new mode + a bad revert row.
    revert_dt = _normalize_revert_at(revert_at)
    revert_target: str | None = None
    if revert_dt is not None:
        revert_target = (revert_to_mode or DEFAULT_MODE).strip().lower()
        if revert_target not in VALID_MODES:
            raise ValueError(
                f"invalid revert_to_mode {revert_to_mode!r}; expected one of {list(VALID_MODES)}"
            )
        if revert_target == raw:
            raise ValueError("revert_to_mode must differ from the new mode")

    before_mode = await get_aggression_mode()
    before_revert = await get_pending_revert()

    async with db.acquire() as c:
        await c.execute(
            """
            INSERT INTO runtime_settings (key, value, updated_at, updated_by)
            VALUES ('aggression_mode', $1, now(), $2)
            ON CONFLICT (key) DO UPDATE SET
                value      = EXCLUDED.value,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by
            """,
            raw,
            actor_user_id,
        )
        # Manual change always supersedes a pending revert: clear it
        # first, then write the new schedule (if any) below.  This keeps
        # things idempotent — flipping the mode by hand cancels the
        # auto-revert the operator no longer wants.
        await c.execute(
            "DELETE FROM runtime_settings WHERE key = $1",
            AGGRESSION_REVERT_KEY,
        )

    new_revert: dict[str, Any] | None = None
    if revert_dt is not None and revert_target is not None:
        import json as _json

        new_revert = {
            "at": revert_dt.isoformat(),
            "target": revert_target,
            "from_mode": raw,
            "by_user_id": actor_user_id,
            "by_email": actor_email,
            "reason": cleaned_reason,
        }
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO runtime_settings (key, value, updated_at, updated_by)
                VALUES ($1, $2, now(), $3)
                ON CONFLICT (key) DO UPDATE SET
                    value      = EXCLUDED.value,
                    updated_at = now(),
                    updated_by = EXCLUDED.updated_by
                """,
                AGGRESSION_REVERT_KEY,
                _json.dumps(new_revert),
                actor_user_id,
            )

    # Audit row — surfaces in the existing admin audit panel and
    # backs the "last 5 mode changes" list on the runtime panel.
    try:
        import json as _json

        before_payload: dict[str, Any] = {"mode": before_mode}
        if before_revert is not None:
            before_payload["pending_revert"] = before_revert
        after_payload: dict[str, Any] = {"mode": raw}
        if cleaned_reason:
            after_payload["reason"] = cleaned_reason
        if new_revert is not None:
            after_payload["pending_revert"] = new_revert
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id or "system",
                actor_email,
                "system",
                None,
                "set_aggression_mode",
                _json.dumps(before_payload),
                _json.dumps(after_payload),
            )
    except Exception as e:
        log.warning("audit log insert (aggression mode) failed: %s", e)
    return raw


async def get_pending_revert() -> dict[str, Any] | None:
    """Return the scheduled auto-revert (if any), or ``None``."""
    try:
        v = await db.fetchval(
            "SELECT value FROM runtime_settings WHERE key = $1",
            AGGRESSION_REVERT_KEY,
        )
    except Exception as e:
        log.debug("get_pending_revert read failed: %s", e)
        return None
    if not v:
        return None
    import json as _json

    try:
        parsed = _json.loads(v)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    target = (parsed.get("target") or "").strip().lower()
    if target not in VALID_MODES:
        return None
    return parsed


async def cancel_pending_revert(
    *, actor_user_id: str | None, actor_email: str | None
) -> dict[str, Any] | None:
    """Cancel an outstanding scheduled revert.  Returns the cancelled
    payload (or ``None`` if there was nothing scheduled)."""
    existing = await get_pending_revert()
    if existing is None:
        return None
    async with db.acquire() as c:
        await c.execute(
            "DELETE FROM runtime_settings WHERE key = $1",
            AGGRESSION_REVERT_KEY,
        )
    try:
        import json as _json

        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id or "system",
                actor_email,
                "system",
                None,
                "cancel_aggression_mode_revert",
                _json.dumps({"pending_revert": existing}),
                _json.dumps({"pending_revert": None}),
            )
    except Exception as e:
        log.warning("audit log insert (cancel revert) failed: %s", e)
    return existing


async def apply_due_revert() -> dict[str, Any] | None:
    """If the pending revert is past due, flip the mode now and clear
    the schedule.  Returns the applied revert payload (or ``None``).

    Called from :func:`get_state` so the dashboard / orchestrator both
    observe the new mode without a separate scheduler process.
    """
    pending = await get_pending_revert()
    if pending is None:
        return None
    from datetime import datetime, timezone

    raw_at = pending.get("at") or ""
    s = str(raw_at)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        due = datetime.fromisoformat(s)
    except Exception:
        # Garbled row — drop it so it doesn't block forever.
        async with db.acquire() as c:
            await c.execute(
                "DELETE FROM runtime_settings WHERE key = $1",
                AGGRESSION_REVERT_KEY,
            )
        return None
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if due > datetime.now(timezone.utc):
        return None

    target = (pending.get("target") or DEFAULT_MODE).strip().lower()
    if target not in VALID_MODES:
        target = DEFAULT_MODE
    before_mode = await get_aggression_mode()

    async with db.acquire() as c:
        await c.execute(
            """
            INSERT INTO runtime_settings (key, value, updated_at, updated_by)
            VALUES ('aggression_mode', $1, now(), $2)
            ON CONFLICT (key) DO UPDATE SET
                value      = EXCLUDED.value,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by
            """,
            target,
            "scheduler",
        )
        await c.execute(
            "DELETE FROM runtime_settings WHERE key = $1",
            AGGRESSION_REVERT_KEY,
        )

    try:
        import json as _json

        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                "scheduler",
                None,
                "system",
                None,
                "set_aggression_mode",
                _json.dumps({"mode": before_mode, "pending_revert": pending}),
                _json.dumps(
                    {
                        "mode": target,
                        "reason": "scheduled auto-revert",
                        "scheduled_by_user_id": pending.get("by_user_id"),
                        "scheduled_by_email": pending.get("by_email"),
                    }
                ),
            )
    except Exception as e:
        log.warning("audit log insert (auto-revert) failed: %s", e)
    return pending


async def get_aggression_mode_history(limit: int = 5) -> list[dict[str, Any]]:
    """Recent ``set_aggression_mode`` audit rows (most recent first).

    Surfaces in the admin runtime panel as "last N mode changes" with
    timestamp, operator, and reason.
    """
    limit = max(1, min(50, int(limit)))
    try:
        rows = await db.fetch(
            """
            SELECT actor_user_id,
                   actor_email,
                   before_value,
                   after_value,
                   to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
              FROM admin_audit_log
             WHERE action = 'set_aggression_mode'
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )
    except Exception as e:
        log.debug("aggression mode history read failed: %s", e)
        return []
    import json as _json

    out: list[dict[str, Any]] = []
    for r in rows:
        before_raw = r["before_value"]
        after_raw = r["after_value"]
        try:
            before = _json.loads(before_raw) if isinstance(before_raw, str) else (before_raw or {})
        except Exception:
            before = {}
        try:
            after = _json.loads(after_raw) if isinstance(after_raw, str) else (after_raw or {})
        except Exception:
            after = {}
        out.append(
            {
                "at": r["created_at"],
                "actor_user_id": r["actor_user_id"],
                "actor_email": r["actor_email"],
                "from_mode": (before or {}).get("mode"),
                "to_mode": (after or {}).get("mode"),
                "reason": (after or {}).get("reason"),
                "scheduled_revert": (after or {}).get("pending_revert"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Risk-knob editor — operator-tunable per-knob overrides persisted in
# ``system_settings`` under ``risk.<knob>``.  The orchestrator picks
# these up via ``_load_operator_overrides`` so changes apply on the
# next cycle (no restart).  Each value is bounded to the range below
# so a fat-fingered slider can't disable the gates entirely.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _KnobSpec:
    key: str  # short name (no risk. prefix)
    label: str
    description: str
    minimum: float
    maximum: float
    step: float
    kind: str  # "float" | "int"
    unit: str  # "pp" | "fraction" | "usd" | "count"


RISK_KNOB_SPECS: tuple["_KnobSpec", ...] = (
    _KnobSpec("min_edge_pp", "Min edge", "Minimum edge in percentage points",
              0.0, 25.0, 0.5, "float", "pp"),
    _KnobSpec("kelly_fraction_cap", "Kelly fraction cap",
              "Fraction of full-Kelly stake the gate will allow",
              0.05, 1.0, 0.05, "float", "fraction"),
    _KnobSpec("min_agreement_rate", "Min persona agreement",
              "Floor on swarm agreement rate before the gate clears",
              0.0, 1.0, 0.05, "float", "fraction"),
    _KnobSpec("max_confidence_band", "Max confidence band",
              "Maximum acceptable consensus confidence-band width",
              0.05, 0.80, 0.05, "float", "fraction"),
    _KnobSpec("bankroll_floor_usd", "Bankroll floor",
              "Refuse to trade when bankroll dips below this",
              0.0, 100_000.0, 50.0, "float", "usd"),
    _KnobSpec("global_exposure_cap_pct", "Global exposure cap",
              "Total open notional + intended as a fraction of bankroll",
              0.05, 1.0, 0.05, "float", "fraction"),
    _KnobSpec("per_vertical_cap_pct", "Per-vertical exposure cap",
              "Per-vertical open notional cap as a fraction of bankroll",
              0.05, 1.0, 0.05, "float", "fraction"),
    _KnobSpec("max_concurrent_positions", "Max concurrent positions",
              "Hard ceiling on simultaneously open positions",
              1.0, 200.0, 1.0, "int", "count"),
    # ---- Round-2 audit gates (task #730) -------------------------------
    # Exposed on the same Live-risk-knobs panel so the operator can tune
    # the new gates without a redeploy.  Bounds were picked so a slider
    # can't push values into a regime the gate's safe-default branch
    # doesn't already handle (cold start, query degraded, etc).
    _KnobSpec("sharpe_min_ratio", "Min trailing Sharpe",
              "Veto when realised Sharpe over the trailing window decays "
              "below this floor",
              -3.0, 5.0, 0.05, "float", "fraction"),
    _KnobSpec("ruin_max_probability", "Max P(ruin)",
              "Veto when bootstrap Monte-Carlo P(ruin over horizon) "
              "exceeds this cap",
              0.0, 1.0, 0.01, "float", "fraction"),
    _KnobSpec("segment_max_losses", "Segment loss throttle",
              "Veto when settled losses for the candidate's "
              "(league, market_type) segment in the trailing window "
              "reach this count",
              1.0, 50.0, 1.0, "int", "count"),
)

RISK_KNOB_KEYS: frozenset[str] = frozenset(s.key for s in RISK_KNOB_SPECS)
_KNOB_BY_KEY: dict[str, _KnobSpec] = {s.key: s for s in RISK_KNOB_SPECS}


def _coerce_knob_value(spec: "_KnobSpec", raw: Any) -> float | int:
    try:
        v = float(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{spec.key}: not numeric ({raw!r})") from e
    if v < spec.minimum or v > spec.maximum:
        raise ValueError(
            f"{spec.key}: {v} outside [{spec.minimum}, {spec.maximum}]"
        )
    if spec.kind == "int":
        return int(round(v))
    return float(v)


async def get_risk_knob_overrides() -> dict[str, float | int]:
    """Read every ``risk.<knob>`` row currently in ``system_settings``,
    filtered to the spec keys + coerced to numeric. Missing rows mean
    the gate falls back to the aggression-mode default."""
    out: dict[str, float | int] = {}
    try:
        rows = await db.fetch(
            "SELECT key, value FROM system_settings WHERE key LIKE 'risk.%'"
        )
    except Exception as e:
        log.debug("risk knob read failed (best-effort): %s", e)
        return out
    for r in rows:
        full = r["key"] or ""
        short = full[len("risk."):]
        if short not in RISK_KNOB_KEYS:
            continue
        spec = _KNOB_BY_KEY[short]
        try:
            out[short] = _coerce_knob_value(spec, r["value"])
        except Exception:
            continue
    return out


async def set_risk_knob_overrides(
    updates: dict[str, Any],
    *,
    actor_user_id: str,
    actor_email: str | None,
) -> dict[str, float | int]:
    """Upsert (or clear with value=None) the supplied risk knobs.

    Unknown keys raise ``ValueError`` so a typo can't silently land in
    ``system_settings``. Each mutation writes an ``admin_audit_log``
    row with action ``set_risk_knob`` so the existing audit panel
    surfaces it next to the user / role changes.
    """
    if not isinstance(updates, dict):
        raise ValueError("updates must be a dict")

    # Validate everything BEFORE writing so partial failures don't
    # leave the configuration half-applied.
    cleaned: dict[str, float | int | None] = {}
    for key, raw in updates.items():
        if key not in RISK_KNOB_KEYS:
            raise ValueError(f"unknown risk knob: {key!r}")
        if raw is None:
            cleaned[key] = None
            continue
        cleaned[key] = _coerce_knob_value(_KNOB_BY_KEY[key], raw)

    before = await get_risk_knob_overrides()

    async with db.acquire() as c:
        for key, val in cleaned.items():
            full = f"risk.{key}"
            if val is None:
                await c.execute(
                    "DELETE FROM system_settings WHERE key = $1", full
                )
            else:
                await c.execute(
                    """
                    INSERT INTO system_settings (key, value, updated_at)
                    VALUES ($1, $2, now())
                    ON CONFLICT (key) DO UPDATE SET
                      value = EXCLUDED.value,
                      updated_at = EXCLUDED.updated_at
                    """,
                    full,
                    str(val),
                )

    after = await get_risk_knob_overrides()
    # Audit row — uses the shared admin_audit_log so the existing
    # admin audit panel renders it. ``target_user_id`` is "system"
    # since the change isn't scoped to a single user.
    try:
        import json

        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id,
                actor_email,
                "system",
                None,
                "set_risk_knob",
                json.dumps(before),
                json.dumps(after),
            )
    except Exception as e:
        log.warning("audit log insert (risk knob) failed: %s", e)
    return after


def risk_knob_specs_payload() -> list[dict[str, Any]]:
    return [
        {
            "key": s.key,
            "label": s.label,
            "description": s.description,
            "minimum": s.minimum,
            "maximum": s.maximum,
            "step": s.step,
            "kind": s.kind,
            "unit": s.unit,
        }
        for s in RISK_KNOB_SPECS
    ]


# Defaults for knobs that don't live in MODE_PROFILES — these mirror
# the hard-coded fallbacks inside ``risk/gates.py`` so the editor can
# display a meaningful "default" column for every knob.
_STATIC_KNOB_DEFAULTS: dict[str, float | int] = {
    "bankroll_floor_usd": 100.0,
    "global_exposure_cap_pct": 0.50,
    "per_vertical_cap_pct": 0.30,
    "max_concurrent_positions": 25,
}


async def get_risk_knob_state() -> dict[str, Any]:
    """Combined view for the editor: spec + current override + the
    aggression-mode default that the override would replace."""
    overrides = await get_risk_knob_overrides()
    mode = await get_aggression_mode()
    profile = profile_for(mode)
    knobs: list[dict[str, Any]] = []
    for s in RISK_KNOB_SPECS:
        default = profile.get(s.key, _STATIC_KNOB_DEFAULTS.get(s.key))
        ov = overrides.get(s.key)
        knobs.append(
            {
                "key": s.key,
                "label": s.label,
                "description": s.description,
                "minimum": s.minimum,
                "maximum": s.maximum,
                "step": s.step,
                "kind": s.kind,
                "unit": s.unit,
                "default_value": default,
                "override_value": ov,
                "effective_value": ov if ov is not None else default,
            }
        )
    return {
        "aggression_mode": mode,
        "knobs": knobs,
    }


async def get_risk_knob_history(
    *,
    per_knob_limit: int = 10,
    scan_limit: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    """Per-knob mutation history derived from ``admin_audit_log``.

    Each ``set_risk_knob`` row stores the *full* override dict before
    and after the change. We diff per knob and return the last
    ``per_knob_limit`` mutations for each known knob (newest first).
    Knobs with no recorded change return an empty list.
    """
    history: dict[str, list[dict[str, Any]]] = {k: [] for k in RISK_KNOB_KEYS}
    try:
        rows = await db.fetch(
            """
            SELECT
              actor_user_id,
              actor_email,
              before_value,
              after_value,
              to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS at
            FROM admin_audit_log
            WHERE action = 'set_risk_knob'
            ORDER BY created_at DESC
            LIMIT $1
            """,
            int(scan_limit),
        )
    except Exception as e:
        log.debug("risk knob history read failed: %s", e)
        return history

    import json as _json

    def _as_dict(v: Any) -> dict[str, Any]:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                d = _json.loads(v)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
        return {}

    for r in rows:
        before = _as_dict(r["before_value"])
        after = _as_dict(r["after_value"])
        keys = (set(before.keys()) | set(after.keys())) & set(RISK_KNOB_KEYS)
        for key in keys:
            b = before.get(key)
            a = after.get(key)
            if b == a:
                continue
            bucket = history[key]
            if len(bucket) >= per_knob_limit:
                continue
            bucket.append(
                {
                    "at": r["at"],
                    "actor_user_id": r["actor_user_id"],
                    "actor_email": r["actor_email"],
                    "before": b,
                    "after": a,
                }
            )
    return history


# ---------------------------------------------------------------------------
# Per-vertical visibility (admin pin / hide) — Task #204
#
# Operators decide which verticals the dashboard surfaces by default and which
# pinned ones lead the strip.  We persist the lists as JSON arrays in
# ``system_settings`` so they share the same audit + rollback path as the risk
# knobs.
# ---------------------------------------------------------------------------


VERTICAL_VISIBILITY_HIDDEN_KEY = "verticals.hidden"
VERTICAL_VISIBILITY_PINNED_KEY = "verticals.pinned"


def _coerce_vertical_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            import json as _json

            raw = _json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        v = item.strip().lower()
        if not v or v in cleaned:
            continue
        cleaned.append(v)
    return cleaned


async def get_vertical_visibility() -> dict[str, list[str]]:
    """Return the operator-set visibility overrides for the dashboard
    vertical strip.

    ``hidden`` — verticals omitted from the default strip (admins still
    see them with a "show hidden" toggle).
    ``pinned`` — leads the strip in the supplied order.
    """
    out: dict[str, list[str]] = {"hidden": [], "pinned": []}
    try:
        rows = await db.fetch(
            "SELECT key, value FROM system_settings WHERE key IN ($1, $2)",
            VERTICAL_VISIBILITY_HIDDEN_KEY,
            VERTICAL_VISIBILITY_PINNED_KEY,
        )
    except Exception as e:
        log.debug("vertical visibility read failed (best-effort): %s", e)
        return out
    for r in rows:
        if r["key"] == VERTICAL_VISIBILITY_HIDDEN_KEY:
            out["hidden"] = _coerce_vertical_list(r["value"])
        elif r["key"] == VERTICAL_VISIBILITY_PINNED_KEY:
            out["pinned"] = _coerce_vertical_list(r["value"])
    return out


async def set_vertical_visibility(
    *,
    hidden: Any | None = None,
    pinned: Any | None = None,
    actor_user_id: str,
    actor_email: str | None,
) -> dict[str, list[str]]:
    """Upsert the hidden / pinned vertical lists.

    Either ``hidden`` or ``pinned`` may be ``None`` to leave that side
    untouched.  Unknown verticals (i.e. not present in the registry)
    raise ``ValueError`` so a typo can't silently land — there is no
    "unregister vertical" workflow today, so an unrecognised name is
    almost certainly a mistake.
    """
    if hidden is None and pinned is None:
        raise ValueError("at least one of hidden/pinned must be provided")

    cleaned_hidden = _coerce_vertical_list(hidden) if hidden is not None else None
    cleaned_pinned = _coerce_vertical_list(pinned) if pinned is not None else None

    # Validate against the registry so a typo can't silently land.
    from . import verticals as _vertical_registry

    known = {s.name for s in _vertical_registry.all_specs()}
    for v in list(cleaned_hidden or []) + list(cleaned_pinned or []):
        if v not in known:
            raise ValueError(f"unknown vertical: {v!r}")

    before = await get_vertical_visibility()

    import json as _json

    async with db.acquire() as c:
        if cleaned_hidden is not None:
            await c.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (key) DO UPDATE SET
                  value = EXCLUDED.value,
                  updated_at = EXCLUDED.updated_at
                """,
                VERTICAL_VISIBILITY_HIDDEN_KEY,
                _json.dumps(cleaned_hidden),
            )
        if cleaned_pinned is not None:
            await c.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (key) DO UPDATE SET
                  value = EXCLUDED.value,
                  updated_at = EXCLUDED.updated_at
                """,
                VERTICAL_VISIBILITY_PINNED_KEY,
                _json.dumps(cleaned_pinned),
            )

    after = await get_vertical_visibility()

    try:
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id,
                actor_email,
                "system",
                None,
                "set_vertical_visibility",
                _json.dumps(before),
                _json.dumps(after),
            )
    except Exception as e:
        log.warning("audit log insert (vertical visibility) failed: %s", e)
    return after


# ---------------------------------------------------------------------------
# Per-vertical halt toggle — Task #271
#
# Halting a single vertical is distinct from the global kill switch: the rest
# of the portfolio keeps trading while the affected vertical sits out. We
# piggyback on ``system_settings`` with keys of the form
# ``risk.vertical_halted.<name>`` so the orchestrator's existing
# ``_load_operator_overrides`` (which strips the ``risk.`` prefix) surfaces
# the flag to ``gate_vertical_halted`` as ``operator_overrides['vertical_halted.<name>']``
# without a second DB round-trip per candidate.
#
# The value is a JSON blob carrying the actor + timestamp so the operator
# console can show who halted what, when. Presence of the row = halted;
# absence = live. Every transition writes an ``admin_audit_log`` row.
# ---------------------------------------------------------------------------


VERTICAL_HALT_KEY_PREFIX = "risk.vertical_halted."


def _vertical_halt_key(vertical: str) -> str:
    return f"{VERTICAL_HALT_KEY_PREFIX}{vertical}"


def _require_known_vertical(vertical: str) -> str:
    v = (vertical or "").strip().lower()
    if not v:
        raise ValueError("vertical must be a non-empty string")
    from . import verticals as _vertical_registry

    known = {s.name for s in _vertical_registry.all_specs()}
    if v not in known:
        raise ValueError(f"unknown vertical: {vertical!r}")
    return v


async def get_vertical_halts() -> dict[str, dict[str, Any]]:
    """Return ``{vertical: {"by_user_id":..., "by_email":..., "at":...}}``.

    Best-effort: a read failure returns an empty dict so the orchestrator
    and dashboard never crash on a sick ``system_settings`` table.
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        rows = await db.fetch(
            "SELECT key, value FROM system_settings WHERE key LIKE $1",
            f"{VERTICAL_HALT_KEY_PREFIX}%",
        )
    except Exception as e:
        log.debug("vertical halts read failed (best-effort): %s", e)
        return out
    import json as _json

    for r in rows:
        key = r["key"] or ""
        if not key.startswith(VERTICAL_HALT_KEY_PREFIX):
            continue
        name = key[len(VERTICAL_HALT_KEY_PREFIX):]
        raw = r["value"]
        meta: dict[str, Any] = {}
        if isinstance(raw, str) and raw:
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                meta = {}
        out[name] = meta
    return out


async def is_vertical_halted(vertical: str) -> bool:
    halts = await get_vertical_halts()
    return (vertical or "").strip().lower() in halts


DEFAULT_NO_REASON = "manual: no reason given"

_VALID_HALT_SOURCES = ("manual_admin", "scheduled_rule", "ttl_expiry")


async def _write_halt_history(
    *,
    vertical: str,
    event: str,
    source: str,
    actor_user_id: str | None,
    actor_email: str | None,
    reason: str | None,
    expires_at: "datetime | None" = None,
    schedule_id: int | None = None,
) -> None:
    """Insert one ``vertical_halt_history`` row.  Best-effort —
    history is advisory; failures must never block the underlying
    halt/resume."""
    try:
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO vertical_halt_history (
                    vertical, event, source,
                    actor_user_id, actor_email,
                    reason, expires_at, schedule_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                vertical, event, source,
                actor_user_id, actor_email,
                reason, expires_at, schedule_id,
            )
    except Exception as e:
        log.warning(
            "vertical_halt_history insert failed (%s/%s/%s): %s",
            vertical, event, source, e,
        )


async def set_vertical_halted(
    vertical: str,
    *,
    actor_user_id: str,
    actor_email: str | None,
    reason: str | None = None,
    ttl_secs: int | None = None,
    source: str = "manual_admin",
    schedule_id: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Mark ``vertical`` halted.

    Returns ``(meta, changed)`` — ``changed`` is ``False`` on a no-op
    double-halt so callers can skip the resting-order cancel sweep and
    the duplicate audit-log row.

    ``ttl_secs`` (optional, must be > 0) records an ``expires_at``
    field in the JSON blob. The background TTL sweeper (started from
    the API lifespan) auto-resumes when the deadline passes. The risk
    gate ignores the field — presence of the row remains the halt
    signal, so a missed sweep tick still fails closed.

    ``source`` is the halt origin written to ``vertical_halt_history``
    — one of ``manual_admin`` / ``scheduled_rule`` / ``ttl_expiry``.
    ``schedule_id`` links the history row back to the firing rule.
    """
    name = _require_known_vertical(vertical)
    if source not in _VALID_HALT_SOURCES:
        raise ValueError(
            f"invalid halt source {source!r}; expected one of {list(_VALID_HALT_SOURCES)}"
        )

    before = await get_vertical_halts()
    if name in before:
        # Idempotent double-halt: return existing meta, report no change.
        return before[name], False

    from datetime import datetime, timedelta, timezone
    import json as _json

    now = datetime.now(timezone.utc)
    meta: dict[str, Any] = {
        "by_user_id": actor_user_id,
        "by_email": actor_email,
        "at": now.isoformat(),
    }
    expires_dt: datetime | None = None
    if ttl_secs is not None:
        ttl_int = int(ttl_secs)
        if ttl_int <= 0:
            raise ValueError("ttl_secs must be a positive integer")
        if ttl_int > 7 * 24 * 3600:
            # 7-day cap — anything longer should be a scheduled rule
            # so it survives a process restart with explicit intent.
            raise ValueError("ttl_secs must be <= 7 days (604800 seconds)")
        expires_dt = now + timedelta(seconds=ttl_int)
        meta["expires_at"] = expires_dt.isoformat()
    if source != "manual_admin":
        meta["source"] = source
    if schedule_id is not None:
        meta["schedule_id"] = int(schedule_id)
    if reason:
        meta["reason"] = str(reason)[:500]

    async with db.acquire() as c:
        await c.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ($1, $2, now())
            ON CONFLICT (key) DO UPDATE SET
              value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at
            """,
            _vertical_halt_key(name),
            _json.dumps(meta),
        )

    after = await get_vertical_halts()
    try:
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id,
                actor_email,
                "system",
                None,
                "halt_vertical",
                _json.dumps({"vertical": name, "halted": name in before}),
                _json.dumps({"vertical": name, "halted": True, "meta": meta}),
            )
    except Exception as e:
        log.warning("audit log insert (halt_vertical) failed: %s", e)
    _ = after  # referenced for side-effect readability

    await _write_halt_history(
        vertical=name,
        event="halted",
        source=source,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        reason=meta.get("reason"),
        expires_at=expires_dt,
        schedule_id=schedule_id,
    )
    return meta, True


async def clear_vertical_halt(
    vertical: str,
    *,
    actor_user_id: str,
    actor_email: str | None,
    reason: str | None = None,
    source: str = "manual_admin",
    event: str = "resumed",
) -> tuple[dict[str, Any] | None, bool]:
    """Resume ``vertical``.

    Returns ``(previous_meta_or_None, changed)`` — ``changed`` is
    ``False`` when the vertical wasn't halted (idempotent resume).
    The optional ``reason`` is recorded in the audit log so operators
    can capture *why* something was resumed (matches the halt flow).

    ``source`` and ``event`` plumb through to the
    ``vertical_halt_history`` row so a TTL sweeper can stamp this
    resume with ``source='ttl_expiry'`` / ``event='ttl_expired'``
    and a manual operator click stays the default ``manual_admin`` /
    ``resumed``.
    """
    name = _require_known_vertical(vertical)
    if source not in _VALID_HALT_SOURCES:
        raise ValueError(
            f"invalid resume source {source!r}; expected one of {list(_VALID_HALT_SOURCES)}"
        )
    if event not in ("resumed", "ttl_expired"):
        raise ValueError(
            f"invalid resume event {event!r}; expected 'resumed' or 'ttl_expired'"
        )

    before = await get_vertical_halts()
    if name not in before:
        return None, False

    prev_meta = before[name]
    cleaned_reason = (reason or "").strip()[:500] or None

    async with db.acquire() as c:
        await c.execute(
            "DELETE FROM system_settings WHERE key = $1",
            _vertical_halt_key(name),
        )

    import json as _json

    after_payload: dict[str, Any] = {"vertical": name, "halted": False}
    if cleaned_reason:
        after_payload["reason"] = cleaned_reason
    if source != "manual_admin":
        after_payload["source"] = source

    try:
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id,
                actor_email,
                "system",
                None,
                "resume_vertical",
                _json.dumps({"vertical": name, "halted": True, "meta": prev_meta}),
                _json.dumps(after_payload),
            )
    except Exception as e:
        log.warning("audit log insert (resume_vertical) failed: %s", e)

    schedule_id_raw = prev_meta.get("schedule_id") if isinstance(prev_meta, dict) else None
    schedule_id_int: int | None = None
    if isinstance(schedule_id_raw, (int, float)):
        try:
            schedule_id_int = int(schedule_id_raw)
        except Exception:
            schedule_id_int = None

    await _write_halt_history(
        vertical=name,
        event=event,
        source=source,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        reason=cleaned_reason,
        schedule_id=schedule_id_int,
    )
    return prev_meta, True


# ---------------------------------------------------------------------------
# TTL sweeper + scheduled-rule eval — task #713
# ---------------------------------------------------------------------------


async def list_expired_vertical_halts(
    now: "datetime | None" = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Return ``[(vertical, meta), ...]`` for halts whose ``expires_at``
    is in the past relative to ``now`` (defaults to UTC wall-clock).

    Halts without an ``expires_at`` field are permanent and never
    surface here. Bad/unparseable ``expires_at`` values are skipped
    with a warning so a single corrupt row never stalls the sweeper.
    """
    from datetime import datetime, timezone

    now_dt = now or datetime.now(timezone.utc)
    out: list[tuple[str, dict[str, Any]]] = []
    halts = await get_vertical_halts()
    for vertical, meta in halts.items():
        expires_raw = meta.get("expires_at") if isinstance(meta, dict) else None
        if not expires_raw:
            continue
        try:
            s = expires_raw.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception as e:
            log.warning(
                "vertical halt %s has invalid expires_at %r: %s",
                vertical, expires_raw, e,
            )
            continue
        if dt <= now_dt:
            out.append((vertical, meta))
    return out


# ---------------------------------------------------------------------------
# Hand-rolled 5-field cron parser (minute hour day-of-month month day-of-week).
# Supports `*`, comma lists, ranges (`1-5`), step values (`*/N`, `1-10/2`),
# and a few common name aliases (`@hourly`, `@daily`, `@weekly`).
# Day-of-week is 0=Sunday..6=Saturday (cron convention); 7 is also Sunday.
# ---------------------------------------------------------------------------


_CRON_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

_CRON_FIELD_BOUNDS = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # dom
    (1, 12),  # month
    (0, 7),   # dow (7 == 0 == Sunday)
)


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    """Return the set of allowed integer values for one cron field."""
    out: set[int] = set()
    for chunk in field.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise ValueError(f"empty cron sub-expression in {field!r}")
        step = 1
        if "/" in chunk:
            base, step_s = chunk.split("/", 1)
            try:
                step = int(step_s)
            except ValueError as e:
                raise ValueError(f"invalid step in {chunk!r}") from e
            if step <= 0:
                raise ValueError(f"step must be positive in {chunk!r}")
        else:
            base = chunk
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError as e:
                raise ValueError(f"invalid range in {chunk!r}") from e
            if start > end:
                raise ValueError(f"range start > end in {chunk!r}")
        else:
            try:
                start = end = int(base)
            except ValueError as e:
                raise ValueError(f"invalid cron value in {chunk!r}") from e
        if start < lo or end > hi:
            raise ValueError(
                f"cron value {chunk!r} out of bounds [{lo}, {hi}]"
            )
        for v in range(start, end + 1, step):
            out.add(v)
    return out


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Parse a 5-field cron expression. Returns the five allowed-value
    sets ``(minute, hour, dom, month, dow)``. Day-of-week 7 is
    normalized to 0 (Sunday). Raises ``ValueError`` on any parse error.
    """
    raw = (expr or "").strip()
    if not raw:
        raise ValueError("cron expression must be non-empty")
    if raw in _CRON_ALIASES:
        raw = _CRON_ALIASES[raw]
    parts = raw.split()
    if len(parts) != 5:
        raise ValueError(
            f"cron expression must have 5 fields, got {len(parts)} ({expr!r})"
        )
    fields = []
    for part, (lo, hi) in zip(parts, _CRON_FIELD_BOUNDS):
        fields.append(_parse_cron_field(part, lo, hi))
    minute, hour, dom, month, dow = fields
    if 7 in dow:
        dow = (dow - {7}) | {0}
    return minute, hour, dom, month, dow


def cron_matches(expr: str, dt: "datetime") -> bool:
    """Return True iff the wall-clock minute ``dt`` (UTC, second/usec
    truncated) matches the cron expression ``expr``.

    Day-of-month and day-of-week behave per Vixie cron: when one is
    `*` and the other is restricted, only the restricted one applies;
    when both are restricted, EITHER match wins (intentionally lenient
    so "Sunday OR the 1st" works).
    """
    minute, hour, dom, month, dow = parse_cron(expr)
    if dt.minute not in minute:
        return False
    if dt.hour not in hour:
        return False
    if dt.month not in month:
        return False
    # Python weekday(): Monday=0..Sunday=6. Cron uses Sunday=0..Saturday=6.
    py_dow = (dt.weekday() + 1) % 7
    parts = (expr.strip().split() if not expr.strip().startswith("@") else
             _CRON_ALIASES[expr.strip()].split())
    dom_unrestricted = parts[2].strip() == "*"
    dow_unrestricted = parts[4].strip() == "*"
    dom_match = dt.day in dom
    dow_match = py_dow in dow
    if dom_unrestricted and not dow_unrestricted:
        return dow_match
    if dow_unrestricted and not dom_unrestricted:
        return dom_match
    return dom_match or dow_match


async def list_vertical_halt_schedules() -> list[dict[str, Any]]:
    """Return every row in ``vertical_halt_schedules``, newest first."""
    try:
        rows = await db.fetch(
            """
            SELECT id, vertical, cron_expr, duration_secs, reason, enabled,
                   created_by_user_id, created_by_email,
                   created_at, updated_at, last_fired_at
              FROM vertical_halt_schedules
             ORDER BY created_at DESC
            """
        )
    except Exception as e:
        log.warning("list_vertical_halt_schedules read failed: %s", e)
        return []
    return [dict(r) for r in rows]


async def create_vertical_halt_schedule(
    *,
    vertical: str,
    cron_expr: str,
    duration_secs: int,
    reason: str,
    actor_user_id: str | None,
    actor_email: str | None,
) -> dict[str, Any]:
    """Insert a new schedule rule. Validates the cron expression and
    duration server-side so a malformed payload can't get persisted.
    Returns the inserted row.
    """
    name = _require_known_vertical(vertical)
    parse_cron(cron_expr)  # raises ValueError on bad expression
    duration_int = int(duration_secs)
    if duration_int <= 0:
        raise ValueError("duration_secs must be a positive integer")
    if duration_int > 7 * 24 * 3600:
        raise ValueError("duration_secs must be <= 7 days (604800 seconds)")
    cleaned_reason = (reason or "").strip()
    if not cleaned_reason:
        raise ValueError("reason is required for scheduled halts")
    cleaned_reason = cleaned_reason[:500]

    row = await db.fetchrow(
        """
        INSERT INTO vertical_halt_schedules (
            vertical, cron_expr, duration_secs, reason,
            enabled, created_by_user_id, created_by_email
        ) VALUES ($1, $2, $3, $4, true, $5, $6)
        RETURNING id, vertical, cron_expr, duration_secs, reason, enabled,
                  created_by_user_id, created_by_email,
                  created_at, updated_at, last_fired_at
        """,
        name, cron_expr.strip(), duration_int, cleaned_reason,
        actor_user_id, actor_email,
    )
    return dict(row) if row else {}


async def update_vertical_halt_schedule_enabled(
    schedule_id: int, enabled: bool
) -> dict[str, Any] | None:
    """Toggle a schedule rule's ``enabled`` flag without deleting it.

    Returns the updated row, or ``None`` if the id was not found.
    Disabling a rule preserves its history + creation metadata so an
    operator can re-enable it later without losing context.
    """
    try:
        sid = int(schedule_id)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid schedule id {schedule_id!r}") from e
    row = await db.fetchrow(
        """
        UPDATE vertical_halt_schedules
           SET enabled = $1, updated_at = now()
         WHERE id = $2
        RETURNING id, vertical, cron_expr, duration_secs, reason, enabled,
                  created_by_user_id, created_by_email,
                  created_at, updated_at, last_fired_at
        """,
        bool(enabled), sid,
    )
    return dict(row) if row else None


async def delete_vertical_halt_schedule(schedule_id: int) -> bool:
    """Delete a schedule rule by id. Returns True iff a row was removed."""
    try:
        sid = int(schedule_id)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid schedule id {schedule_id!r}") from e
    res = await db.fetchval(
        """
        WITH d AS (
          DELETE FROM vertical_halt_schedules WHERE id = $1
          RETURNING 1
        )
        SELECT count(*)::int FROM d
        """,
        sid,
    )
    return bool(res)


async def list_vertical_halt_history(
    *,
    vertical: str | None = None,
    since: "datetime | None" = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return audit history rows, newest first. Optional filters:
    ``vertical`` (exact match, case-insensitive) and ``since`` (UTC
    cutoff). ``limit`` is clamped to [1, 500].
    """
    limit_int = max(1, min(500, int(limit or 100)))
    clauses: list[str] = []
    params: list[Any] = []
    if vertical:
        params.append(vertical.strip().lower())
        clauses.append(f"vertical = ${len(params)}")
    if since is not None:
        params.append(since)
        clauses.append(f"effective_at >= ${len(params)}")
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit_int)
    try:
        rows = await db.fetch(
            f"""
            SELECT id, vertical, event, source,
                   actor_user_id, actor_email,
                   reason, effective_at, expires_at, schedule_id
              FROM vertical_halt_history
              {where_sql}
             ORDER BY effective_at DESC, id DESC
             LIMIT ${len(params)}
            """,
            *params,
        )
    except Exception as e:
        log.warning("list_vertical_halt_history read failed: %s", e)
        return []
    return [dict(r) for r in rows]


def _minute_boundary(dt: "datetime") -> "datetime":
    """Truncate ``dt`` to the start of its UTC minute."""
    return dt.replace(second=0, microsecond=0)


async def list_due_vertical_halt_schedules(
    now: "datetime | None" = None,
) -> list[dict[str, Any]]:
    """Return enabled schedule rows whose cron expression matches the
    current minute and which haven't already fired in the same minute.

    Cron expressions match at minute granularity (the smallest cron
    field is one minute), so the dedupe key is the **minute boundary
    of ``last_fired_at``** vs. the current minute.  This means:

      * a 30s scheduler tick re-evaluates rules twice per minute,
      * a `* * * * *` rule fires exactly once per minute (the second
        tick inside the minute is a no-op because ``last_fired_at``
        already points into the current minute),
      * a one-shot `30 14 * * 5` rule cannot re-fire in the same
        minute even if the loop runs unusually fast.
    """
    from datetime import datetime, timezone

    now_dt = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    minute_start = _minute_boundary(now_dt)
    out: list[dict[str, Any]] = []
    rules = await list_vertical_halt_schedules()
    for rule in rules:
        if not rule.get("enabled"):
            continue
        expr = rule.get("cron_expr") or ""
        try:
            if not cron_matches(expr, now_dt):
                continue
        except ValueError as e:
            log.warning(
                "schedule %s has invalid cron %r: %s",
                rule.get("id"), expr, e,
            )
            continue
        last = rule.get("last_fired_at")
        if isinstance(last, datetime) and last >= minute_start:
            continue
        out.append(rule)
    return out


async def mark_schedule_fired(
    schedule_id: int, *, fired_at: "datetime | None" = None
) -> bool:
    """Atomically claim a schedule fire for the minute of ``fired_at``.

    Returns ``True`` iff this caller successfully advanced
    ``last_fired_at`` into the current minute (i.e. won the race).
    Returns ``False`` if another worker / earlier tick already
    claimed it for the same minute.

    The conditional ``WHERE last_fired_at IS NULL OR last_fired_at <
    minute_start`` is the cheap distributed-lock primitive that
    prevents duplicate halts when more than one api-server replica
    is sweeping the same schedule table.
    """
    from datetime import datetime, timezone

    ts = fired_at or datetime.now(timezone.utc)
    minute_start = _minute_boundary(ts)
    try:
        async with db.acquire() as c:
            row = await c.fetchrow(
                """
                UPDATE vertical_halt_schedules
                   SET last_fired_at = $1, updated_at = now()
                 WHERE id = $2
                   AND (last_fired_at IS NULL OR last_fired_at < $3)
                RETURNING id
                """,
                ts, int(schedule_id), minute_start,
            )
            return row is not None
    except Exception as e:
        log.warning(
            "mark_schedule_fired(%s) failed: %s", schedule_id, e
        )
        return False


# ---------------------------------------------------------------------------
# Aggression-mode presets — Task #356
#
# Operators repeatedly type the same handful of reasons ("NFL primetime",
# "low liquidity overnight", "model drift suspected") when flipping the
# aggression mode. Saving them as labelled one-click presets — paired
# with a target mode and an optional revert-duration in minutes — keeps
# the audit log consistent and shaves a few seconds off every routine
# change.
#
# Persisted as a single JSON-array row in ``runtime_settings`` under the
# key ``aggression_mode_presets`` so the existing key/value table can
# carry it without a dedicated migration. Each entry:
#   {id, label, target_mode, reason, revert_minutes,
#    created_at, created_by_user_id, created_by_email}
# ---------------------------------------------------------------------------


AGGRESSION_PRESETS_KEY = "aggression_mode_presets"
_PRESET_LABEL_MAX = 60
_PRESET_REASON_MAX = 500
_PRESET_REVERT_MINUTES_MAX = 14 * 24 * 60  # 2 weeks
_PRESET_MAX_COUNT = 25


def _coerce_preset_revert_minutes(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid revert_minutes: {raw!r}") from e
    if v <= 0:
        raise ValueError("revert_minutes must be positive")
    if v > _PRESET_REVERT_MINUTES_MAX:
        raise ValueError(
            f"revert_minutes {v} exceeds maximum {_PRESET_REVERT_MINUTES_MAX}"
        )
    return v


def _normalize_preset_record(raw: Any) -> dict[str, Any] | None:
    """Defensive coercion for a single stored preset.

    Returns ``None`` when the row is too malformed to surface (e.g.
    missing label or target_mode) so a single bad entry can't break the
    whole list for the operator.
    """
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip()
    label = str(raw.get("label") or "").strip()
    target = str(raw.get("target_mode") or "").strip().lower()
    if not pid or not label or target not in VALID_MODES:
        return None
    reason_raw = raw.get("reason")
    reason = (
        str(reason_raw).strip()[:_PRESET_REASON_MAX]
        if isinstance(reason_raw, str) and reason_raw.strip()
        else None
    )
    revert_minutes_raw = raw.get("revert_minutes")
    try:
        revert_minutes = _coerce_preset_revert_minutes(revert_minutes_raw)
    except ValueError:
        revert_minutes = None
    return {
        "id": pid,
        "label": label[:_PRESET_LABEL_MAX],
        "target_mode": target,
        "reason": reason,
        "revert_minutes": revert_minutes,
        "created_at": raw.get("created_at"),
        "created_by_user_id": raw.get("created_by_user_id"),
        "created_by_email": raw.get("created_by_email"),
    }


async def list_aggression_presets() -> list[dict[str, Any]]:
    try:
        v = await db.fetchval(
            "SELECT value FROM runtime_settings WHERE key = $1",
            AGGRESSION_PRESETS_KEY,
        )
    except Exception as e:
        log.debug("list_aggression_presets read failed: %s", e)
        return []
    if not v:
        return []
    import json as _json

    try:
        parsed = _json.loads(v)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        norm = _normalize_preset_record(item)
        if norm is not None:
            out.append(norm)
    return out


async def _write_aggression_presets(presets: list[dict[str, Any]], *, actor_user_id: str | None) -> None:
    import json as _json

    async with db.acquire() as c:
        await c.execute(
            """
            INSERT INTO runtime_settings (key, value, updated_at, updated_by)
            VALUES ($1, $2, now(), $3)
            ON CONFLICT (key) DO UPDATE SET
                value      = EXCLUDED.value,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by
            """,
            AGGRESSION_PRESETS_KEY,
            _json.dumps(presets),
            actor_user_id,
        )


async def add_aggression_preset(
    *,
    label: str,
    target_mode: str,
    reason: str | None,
    revert_minutes: Any | None,
    actor_user_id: str,
    actor_email: str | None,
) -> dict[str, Any]:
    """Append a labelled preset.  Raises ``ValueError`` on bad input or
    when the preset cap / duplicate-label rule trips."""
    cleaned_label = (label or "").strip()
    if not cleaned_label:
        raise ValueError("label is required")
    if len(cleaned_label) > _PRESET_LABEL_MAX:
        raise ValueError(f"label too long (max {_PRESET_LABEL_MAX})")
    target = (target_mode or "").strip().lower()
    if target not in VALID_MODES:
        raise ValueError(
            f"invalid target_mode {target_mode!r}; expected one of {list(VALID_MODES)}"
        )
    cleaned_reason = (reason or "").strip()[:_PRESET_REASON_MAX] or None
    cleaned_minutes = _coerce_preset_revert_minutes(revert_minutes)

    existing = await list_aggression_presets()
    if len(existing) >= _PRESET_MAX_COUNT:
        raise ValueError(
            f"preset limit reached ({_PRESET_MAX_COUNT}); delete one first"
        )
    lower_label = cleaned_label.lower()
    if any((p.get("label") or "").strip().lower() == lower_label for p in existing):
        raise ValueError(f"a preset named {cleaned_label!r} already exists")

    from datetime import datetime, timezone
    import uuid as _uuid

    record: dict[str, Any] = {
        "id": _uuid.uuid4().hex,
        "label": cleaned_label,
        "target_mode": target,
        "reason": cleaned_reason,
        "revert_minutes": cleaned_minutes,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_by_user_id": actor_user_id,
        "created_by_email": actor_email,
    }

    next_list = existing + [record]
    await _write_aggression_presets(next_list, actor_user_id=actor_user_id)

    try:
        import json as _json

        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id,
                actor_email,
                "system",
                None,
                "add_aggression_mode_preset",
                _json.dumps({"presets": existing}),
                _json.dumps({"preset": record}),
            )
    except Exception as e:
        log.warning("audit log insert (add preset) failed: %s", e)
    return record


async def delete_aggression_preset(
    preset_id: str,
    *,
    actor_user_id: str,
    actor_email: str | None,
) -> dict[str, Any] | None:
    """Remove a preset by id.  Returns the removed record (or ``None``
    if no preset matched)."""
    pid = (preset_id or "").strip()
    if not pid:
        raise ValueError("preset_id is required")
    existing = await list_aggression_presets()
    removed: dict[str, Any] | None = None
    remaining: list[dict[str, Any]] = []
    for p in existing:
        if removed is None and p.get("id") == pid:
            removed = p
            continue
        remaining.append(p)
    if removed is None:
        return None
    await _write_aggression_presets(remaining, actor_user_id=actor_user_id)
    try:
        import json as _json

        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id,
                actor_email,
                "system",
                None,
                "delete_aggression_mode_preset",
                _json.dumps({"preset": removed}),
                _json.dumps({"preset": None}),
            )
    except Exception as e:
        log.warning("audit log insert (delete preset) failed: %s", e)
    return removed


async def get_state() -> dict[str, Any]:
    """Snapshot of the active mode + its derived gate defaults.

    Used by both the admin API and the operator dashboard so they show a
    consistent picture of what the swarm + gates will do on the next
    cycle.
    """
    # Apply any due scheduled revert before reporting state so the
    # dashboard, orchestrator, and audit log all see the same mode.
    try:
        await apply_due_revert()
    except Exception as e:
        log.debug("apply_due_revert failed (best-effort): %s", e)
    mode = await get_aggression_mode()
    updated_at: str | None = None
    updated_by: str | None = None
    try:
        row = await db.fetchrow(
            """
            SELECT to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS updated_at,
                   updated_by
              FROM runtime_settings
             WHERE key = 'aggression_mode'
            """
        )
        if row is not None:
            updated_at = row["updated_at"]
            updated_by = row["updated_by"]
    except Exception as e:
        log.debug("runtime_settings meta read failed: %s", e)
    trading_halted = await get_trading_halted()
    history = await get_aggression_mode_history(limit=5)
    pending_revert = await get_pending_revert()
    presets = await list_aggression_presets()
    return {
        "aggression_mode": mode,
        "trading_halted": trading_halted,
        "thresholds": profile_for(mode),
        "available_modes": list(VALID_MODES),
        "updated_at": updated_at,
        "updated_by": updated_by,
        "history": history,
        "pending_revert": pending_revert,
        "presets": presets,
    }


# ---------------------------------------------------------------------------
# Per-vertical gatekeeper-knob overrides — Task #714.
#
# The 4 knobs the swarm gatekeeper consults on every cycle —
# ``min_edge_pp`` / ``min_agreement_rate`` / ``max_confidence_band`` /
# ``kelly_fraction_cap`` — already accept a *global* override stored
# as ``system_settings`` rows ``risk.<knob>``.  Task #714 layers a
# *per-vertical* override on top — ``risk.<knob>.<vertical>`` — so a
# single noisy book can be tightened without dragging the rest of the
# portfolio.
#
# Resolution order on the read side (see ``app.risk.gates``):
#
#   1. ``risk.<knob>.<vertical>``  (per-vertical operator override)
#   2. ``risk.<knob>``             (global operator override)
#   3. ``MODE_PROFILES[mode][knob]``  (aggression-mode default)
#
# The same 4 knobs ALREADY validate inputs against ``RISK_KNOB_SPECS``
# (``_coerce_knob_value``) — we reuse that bounds check so a slider in
# the per-vertical UI can't push values outside [min, max] either.
#
# Vertical names are normalised to lower-case to match
# ``app.verticals.registry`` and to keep ``risk.<knob>.<vertical>``
# row keys deterministic.
# ---------------------------------------------------------------------------


GATEKEEPER_TUNABLE_KNOBS: tuple[str, ...] = (
    "min_edge_pp",
    "min_agreement_rate",
    "max_confidence_band",
    "kelly_fraction_cap",
)


# Surfaced read-only in the gatekeeper-tuning UI for reference; the
# operator still tunes it through the existing Live-risk-knobs panel
# (global) or the auto-promote pipeline (per-vertical).  POST/DELETE
# on the gatekeeper-tuning endpoints reject this knob explicitly.
GATEKEEPER_READONLY_KNOBS: tuple[str, ...] = ("kelly_multiplier",)


def _normalise_vertical_name(name: str | None) -> str | None:
    if name is None:
        return None
    s = str(name).strip().lower()
    return s or None


def _gatekeeper_knob_spec(knob: str) -> "_KnobSpec":
    if knob not in GATEKEEPER_TUNABLE_KNOBS:
        raise ValueError(f"unknown gatekeeper knob: {knob!r}")
    return _KNOB_BY_KEY[knob]


def _system_settings_key(knob: str, vertical: str | None) -> str:
    """Compose the ``system_settings`` row key for a knob override.

    Global-scope overrides land under the legacy ``risk.<knob>`` key
    so existing call-sites (and the Live-risk-knobs panel) continue
    to read/write the same rows.  Per-vertical overrides use
    ``risk.<knob>.<vertical>`` — the dot-separator matches the spec
    and is distinct from the legacy underscore convention used by
    ``kelly_multiplier_<vertical>`` (which we leave alone).
    """
    if vertical is None:
        return f"risk.{knob}"
    return f"risk.{knob}.{vertical}"


async def _read_system_setting_raw(key: str) -> str | None:
    try:
        row = await db.fetchrow(
            "SELECT value FROM system_settings WHERE key = $1", key
        )
    except Exception as e:
        log.debug("gatekeeper override read (%s) failed: %s", key, e)
        return None
    if row is None:
        return None
    raw = row["value"]
    return None if raw is None else str(raw)


def _coerce_optional_knob(spec: "_KnobSpec", raw: str | None) -> float | int | None:
    if raw is None:
        return None
    try:
        return _coerce_knob_value(spec, raw)
    except Exception:
        return None


async def get_gatekeeper_effective_state(
    vertical: str | None = None,
) -> dict[str, Any]:
    """Return the full effective gatekeeper-knob view for the operator
    console.  When ``vertical`` is provided the response includes the
    per-vertical override (if any) and the resolved effective value
    for that vertical; when ``vertical`` is None the per-vertical
    column is omitted and the effective value is the global override
    (or the mode default).

    Includes the four mutable knobs plus a read-only entry for
    ``kelly_multiplier`` so the UI can show the per-vertical Kelly
    multiplier next to the gate thresholds without the operator
    being able to mutate it from this surface.
    """
    vert = _normalise_vertical_name(vertical)
    mode = await get_aggression_mode()
    profile = profile_for(mode)

    out_knobs: list[dict[str, Any]] = []
    for knob in GATEKEEPER_TUNABLE_KNOBS:
        spec = _KNOB_BY_KEY[knob]
        default = float(profile.get(knob, 0.0))
        global_raw = await _read_system_setting_raw(_system_settings_key(knob, None))
        global_override = _coerce_optional_knob(spec, global_raw)
        per_vertical_override: float | int | None = None
        if vert is not None:
            per_raw = await _read_system_setting_raw(
                _system_settings_key(knob, vert)
            )
            per_vertical_override = _coerce_optional_knob(spec, per_raw)
        if per_vertical_override is not None:
            effective = float(per_vertical_override)
            source = "per_vertical"
        elif global_override is not None:
            effective = float(global_override)
            source = "global"
        else:
            effective = default
            source = "mode_default"
        entry = {
            "key": knob,
            "label": spec.label,
            "description": spec.description,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "step": spec.step,
            "unit": spec.unit,
            "kind": spec.kind,
            "mode_default": default,
            "global_override": global_override,
            "effective": effective,
            "source": source,
            "read_only": False,
        }
        if vert is not None:
            entry["per_vertical_override"] = per_vertical_override
        out_knobs.append(entry)

    # Read-only kelly_multiplier surface — pulled from the registry
    # spec and the existing override path; ``effective`` follows the
    # same precedence the gate itself uses.
    try:
        from . import verticals as _verticals
        spec_obj = _verticals.get_spec(vert) if vert else None
        spec_default = float(spec_obj.kelly_multiplier) if spec_obj else 1.0
    except Exception:
        spec_default = 1.0
    km_global_raw = await _read_system_setting_raw("risk.kelly_multiplier")
    try:
        km_global = float(km_global_raw) if km_global_raw is not None else None
    except (TypeError, ValueError):
        km_global = None
    km_per: float | None = None
    if vert is not None:
        km_per_key = f"risk.kelly_multiplier_{vert.replace('-', '_')}"
        km_per_raw = await _read_system_setting_raw(km_per_key)
        try:
            km_per = float(km_per_raw) if km_per_raw is not None else None
        except (TypeError, ValueError):
            km_per = None
    if km_per is not None:
        km_eff, km_src = km_per, "per_vertical"
    elif km_global is not None:
        km_eff, km_src = km_global, "global"
    else:
        km_eff, km_src = spec_default, "vertical_spec_default"
    km_entry: dict[str, Any] = {
        "key": "kelly_multiplier",
        "label": "Kelly multiplier",
        "description": (
            "Per-vertical Kelly stake multiplier. Tuned via the "
            "auto-promote pipeline; surfaced here for reference only."
        ),
        "minimum": 0.0,
        "maximum": 5.0,
        "step": 0.05,
        "unit": "fraction",
        "kind": "float",
        "mode_default": spec_default,
        "global_override": km_global,
        "effective": km_eff,
        "source": km_src,
        "read_only": True,
    }
    if vert is not None:
        km_entry["per_vertical_override"] = km_per
    out_knobs.append(km_entry)

    return {
        "vertical": vert,
        "aggression_mode": mode,
        "knobs": out_knobs,
    }


async def list_known_verticals_for_tuning() -> list[str]:
    """Return the registered vertical names sorted alphabetically.
    Used by the GET /effective endpoint to populate the per-vertical
    selector without coupling the front-end to the registry shape.
    """
    try:
        from . import verticals as _verticals
        names = list(_verticals.registry.names())
    except Exception as e:
        log.debug("list_known_verticals_for_tuning: registry unavailable (%s)", e)
        return []
    return sorted({_normalise_vertical_name(n) or "" for n in names if n})


async def _write_gatekeeper_history_row(
    *,
    knob: str,
    vertical: str | None,
    old_value: float | int | None,
    new_value: float | int | None,
    actor_user_id: str,
    actor_email: str | None,
    reason: str | None,
) -> None:
    try:
        await db.execute(
            """
            INSERT INTO gatekeeper_setting_history
                (knob, vertical, old_value, new_value,
                 changed_by_user_id, changed_by_email, reason)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            knob,
            vertical,
            None if old_value is None else str(old_value),
            None if new_value is None else str(new_value),
            actor_user_id,
            actor_email,
            (reason.strip()[:500] if isinstance(reason, str) and reason.strip() else None),
        )
    except Exception as e:
        # History write failures must NOT mask the actual setting
        # change — log loudly and move on.  The change itself has
        # already landed in ``system_settings``.
        log.warning("gatekeeper_setting_history insert failed: %s", e)


async def set_gatekeeper_override(
    *,
    knob: str,
    vertical: str | None,
    value: Any,
    actor_user_id: str,
    actor_email: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """Upsert a gatekeeper knob override.

    * ``knob`` must be one of ``GATEKEEPER_TUNABLE_KNOBS``.
    * ``vertical=None`` writes the global ``risk.<knob>`` row.
    * ``vertical='<name>'`` writes ``risk.<knob>.<name>`` after
      validating the name against the live registry.
    * ``value`` is coerced + bounds-checked through
      ``_coerce_knob_value`` so out-of-range inputs raise ``ValueError``.

    No-op semantics: if the new value equals the current row value
    exactly, nothing is written and the response carries
    ``no_op=True`` so the API layer can skip the audit alert.
    """
    spec = _gatekeeper_knob_spec(knob)
    vert = _normalise_vertical_name(vertical)
    if vert is not None:
        try:
            from . import verticals as _verticals
            if not _verticals.registry.has(vert):
                raise ValueError(f"unknown vertical: {vert!r}")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"vertical registry unavailable: {e}") from e
    new_val = _coerce_knob_value(spec, value)

    key = _system_settings_key(knob, vert)
    old_raw = await _read_system_setting_raw(key)
    old_val = _coerce_optional_knob(spec, old_raw)

    if old_val is not None and float(old_val) == float(new_val):
        return {
            "knob": knob,
            "vertical": vert,
            "key": key,
            "old_value": old_val,
            "new_value": new_val,
            "changed": False,
            "no_op": True,
        }

    str_val = str(new_val)
    try:
        await db.execute(
            """
            INSERT INTO system_settings (key, value, updated_at, updated_by)
            VALUES ($1, $2, now(), $3)
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value,
                  updated_at = now(),
                  updated_by = EXCLUDED.updated_by
            """,
            key, str_val, actor_email or actor_user_id,
        )
    except Exception as e:
        raise RuntimeError(f"failed to write {key}: {e}") from e

    await _write_gatekeeper_history_row(
        knob=knob,
        vertical=vert,
        old_value=old_val,
        new_value=new_val,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        reason=reason,
    )
    return {
        "knob": knob,
        "vertical": vert,
        "key": key,
        "old_value": old_val,
        "new_value": new_val,
        "changed": True,
        "no_op": False,
    }


async def clear_gatekeeper_override(
    *,
    knob: str,
    vertical: str | None,
    actor_user_id: str,
    actor_email: str | None,
    reason: str | None,
) -> dict[str, Any]:
    """Delete a gatekeeper knob override, falling the gate back to
    the next layer in the resolution chain.  Returns ``no_op=True``
    if the key was already absent (no audit row written).
    """
    spec = _gatekeeper_knob_spec(knob)
    vert = _normalise_vertical_name(vertical)
    if vert is not None:
        try:
            from . import verticals as _verticals
            if not _verticals.registry.has(vert):
                raise ValueError(f"unknown vertical: {vert!r}")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"vertical registry unavailable: {e}") from e

    key = _system_settings_key(knob, vert)
    old_raw = await _read_system_setting_raw(key)
    old_val = _coerce_optional_knob(spec, old_raw)
    if old_raw is None:
        return {
            "knob": knob,
            "vertical": vert,
            "key": key,
            "old_value": None,
            "new_value": None,
            "changed": False,
            "no_op": True,
        }

    try:
        await db.execute("DELETE FROM system_settings WHERE key = $1", key)
    except Exception as e:
        raise RuntimeError(f"failed to delete {key}: {e}") from e

    await _write_gatekeeper_history_row(
        knob=knob,
        vertical=vert,
        old_value=old_val,
        new_value=None,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        reason=reason,
    )
    return {
        "knob": knob,
        "vertical": vert,
        "key": key,
        "old_value": old_val,
        "new_value": None,
        "changed": True,
        "no_op": False,
    }


async def list_gatekeeper_setting_history(
    *,
    knob: str | None = None,
    vertical: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return audit history rows newest-first.  Optional filters:
    ``knob`` (exact match) and ``vertical`` (exact match, lower-case;
    pass the literal string ``'__global__'`` to filter to
    global-scope rows i.e. ``vertical IS NULL``).  ``limit`` is
    clamped to [1, 500].
    """
    limit_int = max(1, min(500, int(limit or 100)))
    clauses: list[str] = []
    params: list[Any] = []
    if knob:
        params.append(knob)
        clauses.append(f"knob = ${len(params)}")
    if vertical is not None:
        if vertical == "__global__":
            clauses.append("vertical IS NULL")
        else:
            params.append(_normalise_vertical_name(vertical))
            clauses.append(f"vertical = ${len(params)}")
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit_int)
    try:
        rows = await db.fetch(
            f"""
            SELECT id, knob, vertical, old_value, new_value,
                   changed_by_user_id, changed_by_email,
                   reason, changed_at
              FROM gatekeeper_setting_history
              {where_sql}
             ORDER BY changed_at DESC, id DESC
             LIMIT ${len(params)}
            """,
            *params,
        )
    except Exception as e:
        log.warning("list_gatekeeper_setting_history read failed: %s", e)
        return []
    return [dict(r) for r in rows]
