"""Per-vertical persona enable/disable storage.

Backed by ``swarm_persona_toggles`` (migration 0005).  One row per
(vertical, persona_id) pair carrying the boolean ``enabled`` flag plus
the audit columns (``updated_at``, ``updated_by``).  An *absent* row
means the persona is enabled inside that vertical's profile — only
explicit "disable" decisions are persisted, mirroring how
``runtime_settings`` only stores explicit overrides.

The store maintains a per-process cache so the swarm runner doesn't
hit the database on every debate; the cache is invalidated on every
write through this module and warmed lazily on first read.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import db


log = logging.getLogger("axiom.swarm.toggles")


# {vertical: {persona_id: enabled_bool}}
_cache: dict[str, dict[str, bool]] = {}
_loaded = False


async def _refresh_cache() -> None:
    global _cache, _loaded
    fresh: dict[str, dict[str, bool]] = {}
    try:
        rows = await db.fetch(
            "SELECT vertical, persona, enabled FROM swarm_persona_toggles"
        )
    except Exception as e:
        log.debug("toggle cache refresh failed: %s", e)
        _loaded = True
        return
    for r in rows:
        v = (r["vertical"] or "").strip().lower()
        p = (r["persona"] or "").strip()
        if not v or not p:
            continue
        fresh.setdefault(v, {})[p] = bool(r["enabled"])
    _cache = fresh
    _loaded = True


def reset_cache() -> None:
    """Test hook + post-write invalidation."""
    global _cache, _loaded
    _cache = {}
    _loaded = False


async def disabled_persona_ids(vertical: str | None) -> tuple[str, ...]:
    """Return persona IDs that have been explicitly disabled for the
    given vertical.  Empty tuple when there are no overrides (=> the
    profile runs in full)."""
    if not vertical:
        return ()
    if not _loaded:
        await _refresh_cache()
    bucket = _cache.get(vertical.strip().lower())
    if not bucket:
        return ()
    return tuple(sorted(pid for pid, on in bucket.items() if not on))


async def get_toggles_for_vertical(vertical: str) -> dict[str, bool]:
    """Return {persona_id: enabled} for every row stored for ``vertical``."""
    if not _loaded:
        await _refresh_cache()
    return dict(_cache.get((vertical or "").strip().lower(), {}))


async def set_toggle(
    vertical: str,
    persona: str,
    *,
    enabled: bool,
    actor_user_id: str | None,
    actor_email: str | None,
) -> dict[str, bool]:
    """UPSERT the (vertical, persona) toggle.

    Writes an ``admin_audit_log`` row with action ``set_persona_toggle``
    so the existing audit panel surfaces the change. Returns the new
    {persona_id: enabled} map for the vertical.
    """
    v = (vertical or "").strip().lower()
    p = (persona or "").strip()
    if not v:
        raise ValueError("vertical required")
    if not p:
        raise ValueError("persona required")
    before = await get_toggles_for_vertical(v)
    async with db.acquire() as c:
        await c.execute(
            """
            INSERT INTO swarm_persona_toggles (
                vertical, persona, enabled, updated_at, updated_by
            ) VALUES ($1, $2, $3, now(), $4)
            ON CONFLICT (vertical, persona) DO UPDATE SET
                enabled    = EXCLUDED.enabled,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by
            """,
            v, p, bool(enabled), actor_user_id,
        )
    reset_cache()
    after = await get_toggles_for_vertical(v)
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
                "set_persona_toggle",
                json.dumps({"vertical": v, "persona": p, "before": before}),
                json.dumps({"vertical": v, "persona": p, "after": after}),
            )
    except Exception as e:
        log.warning("audit log insert (persona toggle) failed: %s", e)
    return after


async def list_all_overrides() -> list[dict[str, Any]]:
    """Read every stored toggle row (ordered by vertical, persona)."""
    try:
        rows = await db.fetch(
            """
            SELECT vertical, persona, enabled,
                   to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS updated_at,
                   updated_by
              FROM swarm_persona_toggles
             ORDER BY vertical, persona
            """
        )
    except Exception as e:
        log.debug("list_all_overrides failed: %s", e)
        return []
    return [
        {
            "vertical": r["vertical"],
            "persona": r["persona"],
            "enabled": bool(r["enabled"]),
            "updated_at": r["updated_at"],
            "updated_by": r["updated_by"],
        }
        for r in rows
    ]
