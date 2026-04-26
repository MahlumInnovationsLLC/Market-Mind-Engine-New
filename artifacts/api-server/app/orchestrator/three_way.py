"""Helpers that decide which leg (home/draw/away) of a 3-way soccer
market a freshly-routed candidate is betting on.

Each soccer match ships as three independent YES/NO Kalshi markets —
one for the home win, one for the draw, one for the away win.  The
orchestrator only persists a generic YES/NO `side` onto the trade row
because the per-leg semantics live in the (separate) Kalshi ticker.

Settlement (``app.settlement.pipeline.resolve_three_way_leg``) reads a
``three_way_leg`` field out of the trade's ``metadata`` jsonb to apply
the per-leg outcome from a 3-way match result.  Without that field,
the settlement path silently falls back to the binary ``outcome_yes``
and a draw outcome would mis-score the home/away legs.

This module derives that ``three_way_leg`` value at trade-insert time
from whatever signal the orchestrator has on hand:

  1. an explicit ``three_way_leg`` already stamped on the candidate's
     metadata (lets feeds pre-tag candidates without a title at all),
  2. the candidate's own ``side`` field when it already names a leg
     (``home`` / ``draw`` / ``away``), or
  3. the resolved Kalshi market title — searched for ``draw``/``tie``
     keywords first, then matched against the candidate's home/away
     team names.

Returns ``None`` when no leg can be derived; callers leave the
``metadata`` jsonb empty for those trades and settlement falls back to
its legacy binary path.
"""
from __future__ import annotations

import json
from typing import Any

from ..settlement.pipeline import normalize_three_way_result


_DRAW_KEYWORDS: tuple[str, ...] = ("draw", "tie", "stalemate", "level")


def _coerce_metadata(md: Any) -> dict[str, Any]:
    if isinstance(md, dict):
        return md
    if isinstance(md, str):
        try:
            decoded = json.loads(md)
        except Exception:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _team_name(cand: dict[str, Any], ctx: dict[str, Any], key: str) -> str | None:
    """Pull a team name from the candidate's top-level field, its
    ``game_context`` blob, or the legacy ``home``/``away`` sub-dict
    that the n-way feed registers."""
    v = cand.get(key)
    if isinstance(v, str) and v.strip():
        return v
    v = ctx.get(key)
    if isinstance(v, str) and v.strip():
        return v
    short = "home" if key == "home_team" else "away"
    sub = cand.get(short)
    if isinstance(sub, dict):
        nv = sub.get("name")
        if isinstance(nv, str) and nv.strip():
            return nv
    sub = ctx.get(short)
    if isinstance(sub, dict):
        nv = sub.get("name")
        if isinstance(nv, str) and nv.strip():
            return nv
    return None


def derive_three_way_leg(
    *, market_title: str | None, candidate: dict[str, Any] | None
) -> str | None:
    """Return the 3-way leg (``home``/``draw``/``away``) the candidate
    is betting on, or ``None`` when no leg can be derived.

    Caller is expected to gate this on the league's
    ``three_way_market`` flag — non-3-way verticals (NBA/NFL/etc.)
    should not invoke this helper because their titles can legitimately
    contain words like "draw" without implying a soccer draw leg.
    """
    cand = candidate or {}

    # 1) Pre-stamped on the candidate metadata (feeds that already
    # know the per-leg semantics can short-circuit every other check).
    md = _coerce_metadata(cand.get("metadata"))
    for key in ("three_way_leg", "outcome_3way", "leg"):
        v = md.get(key)
        if isinstance(v, str):
            norm = normalize_three_way_result(v)
            if norm is not None:
                return norm

    # 2) Candidate's own `side` already encoding a leg name (the
    # legacy fallback path the settlement helper also honors).
    side = cand.get("side")
    if isinstance(side, str):
        norm = normalize_three_way_result(side)
        if norm is not None:
            return norm

    # 3) Parse the resolved Kalshi market title against draw keywords
    # and the candidate's home/away team names.  Order matters: the
    # draw keyword check fires first so a title like "Will the Liverpool
    # vs Manchester City match end in a draw?" resolves to the draw
    # leg even though both team names also appear in the title.
    if not isinstance(market_title, str) or not market_title.strip():
        return None
    title_lc = market_title.lower()
    for kw in _DRAW_KEYWORDS:
        # Word-boundary-ish check: avoid matching "Andrew" or "tier".
        if kw in title_lc:
            return "draw"

    ctx = cand.get("game_context")
    if isinstance(ctx, str):
        try:
            ctx_decoded = json.loads(ctx)
            ctx = ctx_decoded if isinstance(ctx_decoded, dict) else {}
        except Exception:
            ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}

    home_name = _team_name(cand, ctx, "home_team")
    away_name = _team_name(cand, ctx, "away_team")

    home_in = isinstance(home_name, str) and home_name.lower() in title_lc
    away_in = isinstance(away_name, str) and away_name.lower() in title_lc
    if home_in and not away_in:
        return "home"
    if away_in and not home_in:
        return "away"
    if home_in and away_in:
        # Both names appear — pick whichever shows up first as the
        # subject of the title (Kalshi titles are typically phrased
        # "Will <X> beat <Y>?" so the YES side is the first team).
        try:
            hi = title_lc.index(home_name.lower())
            ai = title_lc.index(away_name.lower())
            if hi != ai:
                return "home" if hi < ai else "away"
        except ValueError:  # pragma: no cover — defensive
            return None
    return None
