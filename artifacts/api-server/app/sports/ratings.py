"""Per-discipline rating cache for combat & individual sports
(task #197).

Combat / individual leagues that flag ``LeagueConfig.n_way_market``
(ATP / WTA / F1 / PGA) need a per-event ``{entrant: rating}`` mapping
to drive the closed-form Plackett-Luce predictor in
``app.predictor_n_way``.  The Sportradar schedule poll surfaces the
field of competitors as a list of metadata dicts (name / id / seed) on
the normalized row, but the orchestrator's ``_predict_for_candidate``
needs an actual rating-per-entrant dict to call into Plackett-Luce.

This module bridges the two: the feed normalizer calls
``set_field(game_id, competitors)`` to cache the rating mapping derived
from each entrant's seed (when present) or a uniform prior (when not),
and the orchestrator calls ``get_field(game_id)`` to enrich its
candidate dict before predicting.

Hermetic / dependency-free / process-local — operators can layer a
real ATP/WTA/F1/PGA ranking source on top via ``set_field`` from a
separate poller (follow-up #214) without changing this module's
public surface.
"""
from __future__ import annotations

from typing import Iterable, Mapping

# game_id -> {entrant_name: rating} on an Elo-like 1500-anchored scale
# so values plug directly into ``predictor_n_way.plackett_luce`` /
# ``bradley_terry_pairwise``.  Process-local — feed pollers refresh on
# each schedule pull, and tests can clear via ``_FIELD_CACHE.clear()``.
_FIELD_CACHE: dict[str, dict[str, float]] = {}

# Seed -> rating offset relative to a 1500 base.  Lower seed (better
# player / qualifying position) gets a higher rating.  Linear decay of
# 25 Elo per seed slot caps at +/- 250 so a 20-deep field stays within
# a plausible spread (a 250-point Elo gap implies ~80% win prob, which
# matches what real ATP / F1 fields look like in practice).
_SEED_BASE = 1500.0
_SEED_STEP = 25.0
_SEED_CAP = 250.0


def _rating_from_seed(seed: int | float | None) -> float:
    """Map a seed value to an Elo-style rating.  Unseeded entrants
    (``seed is None``) anchor at 1500 so the field collapses to a
    uniform Plackett-Luce prior, which is the correct default."""
    if seed is None:
        return _SEED_BASE
    try:
        s = float(seed)
    except (TypeError, ValueError):
        return _SEED_BASE
    # Seeds are 1-indexed: seed 1 is the favorite.
    offset = max(-_SEED_CAP, min(_SEED_CAP, _SEED_STEP * (10.5 - s)))
    return _SEED_BASE + offset


def derive_field_ratings(
    competitors: Iterable[Mapping] | None,
) -> dict[str, float]:
    """Convert a feed-normalized competitor list to a ratings dict.

    Each competitor is expected to be a mapping with at least a
    ``name`` key; ``seed`` (when present) drives the rating offset.
    Unnamed / non-mapping entries are dropped silently so a partial
    payload still yields a usable field.  Empty input returns ``{}``.
    """
    if not competitors:
        return {}
    out: dict[str, float] = {}
    for c in competitors:
        if not isinstance(c, Mapping):
            continue
        name = c.get("name")
        if not name:
            continue
        out[str(name)] = _rating_from_seed(c.get("seed"))
    return out


def set_field(game_id: str, competitors: Iterable[Mapping] | None) -> None:
    """Cache the rating mapping for an event's field.  No-op when the
    derived mapping has fewer than 2 entries (a 1-entrant 'field'
    can't be priced via Plackett-Luce)."""
    if not game_id:
        return
    ratings = derive_field_ratings(competitors)
    if len(ratings) < 2:
        return
    _FIELD_CACHE[str(game_id)] = ratings


def get_field(game_id: str | None) -> dict[str, float] | None:
    """Return the cached rating mapping for ``game_id`` (a copy so
    callers can mutate freely), or ``None`` when no field is
    cached."""
    if not game_id:
        return None
    cached = _FIELD_CACHE.get(str(game_id))
    return dict(cached) if cached else None


def clear_cache() -> None:
    """Test helper — drop all cached fields."""
    _FIELD_CACHE.clear()


def coerce_to_ratings(raw) -> dict[str, float] | None:
    """Best-effort coercion of a raw ``n_way_field`` value into a
    ``{entrant: rating}`` mapping.

    * ``dict`` input with numeric values is taken as-is.
    * ``list`` input (the feed-normalized competitor metadata shape)
      is routed through ``derive_field_ratings``.
    * Anything else returns ``None`` and the caller falls back to the
      team-sport meta-model.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        try:
            out = {str(k): float(v) for k, v in raw.items()}
            return out if len(out) >= 2 else None
        except (TypeError, ValueError):
            return None
    if isinstance(raw, list):
        out = derive_field_ratings(raw)
        return out if len(out) >= 2 else None
    return None
