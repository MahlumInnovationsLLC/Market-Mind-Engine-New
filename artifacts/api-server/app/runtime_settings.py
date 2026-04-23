"""Runtime-tunable operator knobs that aren't persona-/market-scoped.

Currently a single row: the global ``aggression_mode`` (Conservative /
Balanced / Aggressive) which scales the moderator + gatekeeper bias and
the default min-edge / Kelly fraction caps used by the risk gates.

The table is `runtime_settings(key, value, updated_at, updated_by)` and
is bootstrapped lazily on first read/write so a brand new database
doesn't need a separate migration step. Per-key explicit
`system_settings` overrides (e.g. `risk.min_edge_pp`) still win — this
module only changes the *defaults* that the gates fall back to.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import db


log = logging.getLogger("axiom.runtime_settings")


VALID_MODES: tuple[str, ...] = ("conservative", "balanced", "aggressive")
DEFAULT_MODE = "balanced"


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


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runtime_settings (
    key         text PRIMARY KEY,
    value       text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text
);
"""

_ready = False


async def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    try:
        async with db.acquire() as c:
            await c.execute(_SCHEMA_SQL)
        _ready = True
    except Exception as e:  # pragma: no cover - exercised on prod boot
        log.warning("runtime_settings ensure_schema failed: %s", e)


def normalize_mode(mode: str | None) -> str:
    m = (mode or DEFAULT_MODE).strip().lower()
    return m if m in VALID_MODES else DEFAULT_MODE


def profile_for(mode: str | None) -> dict[str, float]:
    return dict(MODE_PROFILES[normalize_mode(mode)])


async def get_aggression_mode() -> str:
    await ensure_schema()
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
    await ensure_schema()
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

    await ensure_schema()
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
    await ensure_schema()
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
)

RISK_KNOB_KEYS: frozenset[str] = frozenset(s.key for s in RISK_KNOB_SPECS)
_KNOB_BY_KEY: dict[str, _KnobSpec] = {s.key: s for s in RISK_KNOB_SPECS}


_SYSTEM_SETTINGS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_settings (
    key         text PRIMARY KEY,
    value       text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""

_system_settings_ready = False


async def _ensure_system_settings_schema() -> None:
    global _system_settings_ready
    if _system_settings_ready:
        return
    try:
        async with db.acquire() as c:
            await c.execute(_SYSTEM_SETTINGS_SCHEMA_SQL)
        _system_settings_ready = True
    except Exception as e:
        log.warning("system_settings ensure_schema failed: %s", e)


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
    await _ensure_system_settings_schema()
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
    await _ensure_system_settings_schema()

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
    await _ensure_system_settings_schema()
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
    await _ensure_system_settings_schema()

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
    await _ensure_system_settings_schema()
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


async def set_vertical_halted(
    vertical: str,
    *,
    actor_user_id: str,
    actor_email: str | None,
    reason: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Mark ``vertical`` halted.

    Returns ``(meta, changed)`` — ``changed`` is ``False`` on a no-op
    double-halt so callers can skip the resting-order cancel sweep and
    the duplicate audit-log row.
    """
    name = _require_known_vertical(vertical)
    await _ensure_system_settings_schema()

    before = await get_vertical_halts()
    if name in before:
        # Idempotent double-halt: return existing meta, report no change.
        return before[name], False

    from datetime import datetime, timezone
    import json as _json

    meta: dict[str, Any] = {
        "by_user_id": actor_user_id,
        "by_email": actor_email,
        "at": datetime.now(timezone.utc).isoformat(),
    }
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
    return meta, True


async def clear_vertical_halt(
    vertical: str,
    *,
    actor_user_id: str,
    actor_email: str | None,
) -> tuple[dict[str, Any] | None, bool]:
    """Resume ``vertical``.

    Returns ``(previous_meta_or_None, changed)`` — ``changed`` is
    ``False`` when the vertical wasn't halted (idempotent resume).
    """
    name = _require_known_vertical(vertical)
    await _ensure_system_settings_schema()

    before = await get_vertical_halts()
    if name not in before:
        return None, False

    prev_meta = before[name]

    async with db.acquire() as c:
        await c.execute(
            "DELETE FROM system_settings WHERE key = $1",
            _vertical_halt_key(name),
        )

    import json as _json

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
                _json.dumps({"vertical": name, "halted": False}),
            )
    except Exception as e:
        log.warning("audit log insert (resume_vertical) failed: %s", e)
    return prev_meta, True


async def get_state() -> dict[str, Any]:
    """Snapshot of the active mode + its derived gate defaults.

    Used by both the admin API and the operator dashboard so they show a
    consistent picture of what the swarm + gates will do on the next
    cycle.
    """
    await ensure_schema()
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
    return {
        "aggression_mode": mode,
        "trading_halted": trading_halted,
        "thresholds": profile_for(mode),
        "available_modes": list(VALID_MODES),
        "updated_at": updated_at,
        "updated_by": updated_by,
        "history": history,
        "pending_revert": pending_revert,
    }
