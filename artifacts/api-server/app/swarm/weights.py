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
    try:
        rows: list[Any] = await db.fetch(
            """
            SELECT analyst_id, league, market_type, brier_score, total_predictions
            FROM swarm_analyst_metrics
            WHERE brier_score IS NOT NULL
            """
        )
    except Exception as e:
        log.warning("refresh_persona_weights: query failed: %s", e)
        _loaded = True  # don't keep retrying every call
        return 0

    for r in rows:
        brier = float(r["brier_score"])
        w = max(0.05, 1.0 - brier)
        # Multiple cuts so lookups can fall back gracefully.
        for key in (
            (r["analyst_id"], r["league"], r["market_type"]),
            (r["analyst_id"], r["league"], None),
            (r["analyst_id"], None, None),
        ):
            fresh.setdefault(key, []).append(w)

    averaged: dict[tuple[str, str | None, str | None], float] = {
        k: sum(v) / len(v) for k, v in fresh.items()
    }
    _weights = averaged
    _loaded = True
    log.info("persona weights loaded: %d keys from %d rows", len(averaged), len(rows))
    return len(rows)


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
