"""Persona weight bootstrap from `swarm_analyst_metrics`.

On boot we read all rows, compute weight = max(0.05, 1 - brier_score),
and key by (analyst_id, league, market_type).  At lookup time we fall
back from the most-specific key to less-specific keys, finally to a
neutral 0.50.

The cache is refreshed lazily on first lookup if it has not been
populated, and can be re-warmed by calling `refresh_persona_weights()`.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import db


log = logging.getLogger(__name__)

_NEUTRAL = 0.50

# (analyst_id, league|None, market_type|None) -> weight
_weights: dict[tuple[str, str | None, str | None], float] = {}
_loaded = False


async def refresh_persona_weights() -> int:
    """Reload weights from `swarm_analyst_metrics`.  Returns row count."""
    global _weights, _loaded
    fresh: dict[tuple[str, str | None, str | None], list[float]] = {}
    # Per-task: persona weight is the rolling 90-day Brier in the
    # league of the current ticker.  Fall back to all-time rows when
    # no recent metrics exist for an (analyst, league, market_type)
    # tuple so a cold-start league still has *some* weight to apply.
    try:
        rows: list[Any] = await db.fetch(
            """
            SELECT analyst_id, league, market_type, brier_score, total_predictions,
                   (last_updated IS NOT NULL
                    AND last_updated > now() - interval '90 days') AS recent
              FROM swarm_analyst_metrics
             WHERE brier_score IS NOT NULL
            """
        )
    except Exception as e:
        log.warning("refresh_persona_weights: query failed: %s", e)
        _loaded = True  # don't keep retrying every call
        return 0

    # Two-pass: prefer rows whose `last_updated` falls in the last
    # 90 days.  Only when *no* recent row exists for a key do we fall
    # back to all-time data.
    recent: dict[tuple[str, str | None, str | None], list[float]] = {}
    alltime: dict[tuple[str, str | None, str | None], list[float]] = {}
    for r in rows:
        brier = float(r["brier_score"])
        w = max(0.05, 1.0 - brier)
        is_recent = bool(r["recent"])
        for key in (
            (r["analyst_id"], r["league"], r["market_type"]),
            (r["analyst_id"], r["league"], None),
            (r["analyst_id"], None, None),
        ):
            (recent if is_recent else alltime).setdefault(key, []).append(w)
    for key in set(recent.keys()) | set(alltime.keys()):
        bucket = recent.get(key) or alltime.get(key) or []
        if bucket:
            fresh[key] = bucket

    averaged: dict[tuple[str, str | None, str | None], float] = {
        k: sum(v) / len(v) for k, v in fresh.items()
    }
    _weights = averaged
    _loaded = True
    log.info("persona weights loaded: %d keys from %d rows", len(averaged), len(rows))
    return len(rows)


async def snapshot_persona_weights() -> list[dict[str, Any]]:
    """Return the active per-(persona, league, market_type) weights as a
    list — used by the operator dashboard so the live weights line up
    with whatever the swarm runner actually applied this cycle."""
    if not _loaded:
        await refresh_persona_weights()
    out: list[dict[str, Any]] = []
    for (persona, league, market_type), weight in sorted(
        _weights.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2] or "")
    ):
        # Only surface the most-specific (persona, league, market_type)
        # rows — the dashboard wants the actual lookup, not its
        # fallback aggregates.
        if league is None or market_type is None:
            continue
        out.append({
            "persona": persona,
            "league": league,
            "market_type": market_type,
            "weight": round(float(weight), 4),
        })
    return out


async def get_persona_weight(
    analyst_id: str, league: str | None, market_type: str | None
) -> float:
    if not _loaded:
        await refresh_persona_weights()
    for key in (
        (analyst_id, league, market_type),
        (analyst_id, league, None),
        (analyst_id, None, None),
    ):
        if key in _weights:
            return _weights[key]
    return _NEUTRAL
