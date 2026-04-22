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
    return {
        "aggression_mode": mode,
        "thresholds": profile_for(mode),
        "available_modes": list(VALID_MODES),
        "updated_at": updated_at,
        "updated_by": updated_by,
    }
