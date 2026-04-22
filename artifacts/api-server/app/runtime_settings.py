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


async def set_aggression_mode(mode: str, *, actor_user_id: str | None) -> str:
    raw = (mode or "").strip().lower()
    if raw not in VALID_MODES:
        raise ValueError(
            f"invalid aggression_mode {mode!r}; expected one of {list(VALID_MODES)}"
        )
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
    return raw


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


async def get_state() -> dict[str, Any]:
    """Snapshot of the active mode + its derived gate defaults.

    Used by both the admin API and the operator dashboard so they show a
    consistent picture of what the swarm + gates will do on the next
    cycle.
    """
    await ensure_schema()
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
    return {
        "aggression_mode": mode,
        "trading_halted": trading_halted,
        "thresholds": profile_for(mode),
        "available_modes": list(VALID_MODES),
        "updated_at": updated_at,
        "updated_by": updated_by,
    }
