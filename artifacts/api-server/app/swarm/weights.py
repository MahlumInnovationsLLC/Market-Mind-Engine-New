"""Persona weight bootstrap from `swarm_analyst_metrics`.

Per task #194 weights are scoped per *vertical* so a persona's Brier
in one domain (e.g. ``sports``) cannot leak into another (e.g.
``crypto``). The cache key grew from ``(analyst, league, market_type)``
to ``(analyst, vertical, league, market_type)``; the lookup falls back
from most-specific to neutral 0.50 in this order:

  (analyst, vertical, league, market_type)
  (analyst, vertical, league, None)
  (analyst, vertical, None,   None)
  (analyst, None,     league, market_type)   # legacy rows missing vertical
  (analyst, None,     league, None)
  (analyst, None,     None,   None)
  -> NEUTRAL (0.50)

The legacy ``(analyst, None, ...)`` tier exists so production rows that
existed before migration 0005's backfill (or fixtures patched without
a vertical column) still feed into the resolver — without it, a brand
new vertical with zero rows would fall straight to neutral even when
the persona has a long sports track record we'd want to anchor on.

Cache is refreshed lazily on first lookup if it has not been populated,
and can be re-warmed by calling `refresh_persona_weights()`.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import db


log = logging.getLogger(__name__)

_NEUTRAL = 0.50

# (analyst_id, vertical|None, league|None, market_type|None) -> weight
_WeightKey = tuple[str, str | None, str | None, str | None]
_weights: dict[_WeightKey, float] = {}
_loaded = False


async def refresh_persona_weights() -> int:
    """Reload weights from `swarm_analyst_metrics`.  Returns row count."""
    global _weights, _loaded
    fresh: dict[_WeightKey, list[float]] = {}
    # Per-task: persona weight is the rolling 90-day Brier in the
    # league of the current ticker.  Fall back to all-time rows when
    # no recent metrics exist for an (analyst, vertical, league,
    # market_type) tuple so a cold-start league still has *some*
    # weight to apply.
    try:
        rows: list[Any] = await db.fetch(
            """
            SELECT analyst_id,
                   vertical,
                   league,
                   market_type,
                   brier_score,
                   total_predictions,
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
    recent: dict[_WeightKey, list[float]] = {}
    alltime: dict[_WeightKey, list[float]] = {}
    for r in rows:
        brier = float(r["brier_score"])
        w = max(0.05, 1.0 - brier)
        is_recent = bool(r["recent"])
        # ``vertical`` may be NULL only for legacy rows pre-0005
        # backfill — we treat those as unscoped (None) so they still
        # surface in the legacy fallback tier.
        v = r["vertical"] or None
        keys: tuple[_WeightKey, ...] = (
            (r["analyst_id"], v, r["league"], r["market_type"]),
            (r["analyst_id"], v, r["league"], None),
            (r["analyst_id"], v, None, None),
            # Legacy unscoped fallbacks so cold-start verticals can
            # anchor on the persona's all-domain history.
            (r["analyst_id"], None, r["league"], r["market_type"]),
            (r["analyst_id"], None, r["league"], None),
            (r["analyst_id"], None, None, None),
        )
        for key in keys:
            (recent if is_recent else alltime).setdefault(key, []).append(w)
    for key in set(recent.keys()) | set(alltime.keys()):
        bucket = recent.get(key) or alltime.get(key) or []
        if bucket:
            fresh[key] = bucket

    averaged: dict[_WeightKey, float] = {
        k: sum(v) / len(v) for k, v in fresh.items()
    }
    _weights = averaged
    _loaded = True
    log.info("persona weights loaded: %d keys from %d rows", len(averaged), len(rows))
    return len(rows)


async def snapshot_persona_weights() -> list[dict[str, Any]]:
    """Return the active per-(persona, vertical, league, market_type)
    weights as a list — used by the operator dashboard so the live
    weights line up with whatever the swarm runner actually applied
    this cycle."""
    if not _loaded:
        await refresh_persona_weights()
    out: list[dict[str, Any]] = []
    for (persona, vertical, league, market_type), weight in sorted(
        _weights.items(),
        key=lambda kv: (
            kv[0][0],
            kv[0][1] or "",
            kv[0][2] or "",
            kv[0][3] or "",
        ),
    ):
        # Only surface the most-specific (persona, vertical, league,
        # market_type) rows — the dashboard wants the actual lookup,
        # not its fallback aggregates.
        if vertical is None or league is None or market_type is None:
            continue
        out.append({
            "persona": persona,
            "vertical": vertical,
            "league": league,
            "market_type": market_type,
            "weight": round(float(weight), 4),
        })
    return out


async def get_persona_weight(
    analyst_id: str,
    league: str | None,
    market_type: str | None,
    *,
    vertical: str | None = None,
) -> float:
    """Resolve the active weight for ``analyst_id`` in this domain.

    ``vertical`` is keyword-only and optional so legacy callers
    (e.g. tests, downstream tools) keep working — when omitted we walk
    the legacy ``(analyst, None, ...)`` tiers and finally neutral.
    """
    if not _loaded:
        await refresh_persona_weights()
    v = (vertical or None)
    fallback_chain: tuple[_WeightKey, ...] = (
        (analyst_id, v, league, market_type),
        (analyst_id, v, league, None),
        (analyst_id, v, None, None),
        (analyst_id, None, league, market_type),
        (analyst_id, None, league, None),
        (analyst_id, None, None, None),
    )
    for key in fallback_chain:
        if key in _weights:
            return _weights[key]
    return _NEUTRAL
