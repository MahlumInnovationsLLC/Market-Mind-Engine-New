"""Helpers that decide which competitor (entrant) of an N-way outright
market a freshly-routed candidate is betting on.

Combat & individual sports (ATP / WTA / F1 / PGA — the leagues whose
``LeagueConfig.n_way_market`` flag is ``True``) trade tournament-winner
markets that ship as one independent YES/NO Kalshi market per
competitor.  The orchestrator only persists a generic YES/NO ``side``
onto the trade row — the per-leg semantics (which competitor this leg
is wagering on) live in the resolved Kalshi ticker / market title.

Settlement (``app.settlement.pipeline.resolve_n_way_competitor``) reads
a ``competitor_id`` field out of the trade's ``metadata`` jsonb so the
per-leg outcome can be derived from the actual race / tournament winner
rather than the binary outcome of any single market.  Without that
field, the settlement path silently falls back to the binary
``outcome_yes`` and a winner outcome would mis-score every losing
competitor's leg as a "model miss" instead of the deterministic NO it
actually is.

This module derives that ``competitor_id`` (and a ``competitor_name``
for human-readable analytics) at trade-insert time from whatever signal
the orchestrator has on hand:

  1. an explicit ``competitor_id`` / ``competitor`` / ``entrant`` /
     ``entrant_id`` already stamped on the candidate's metadata (lets
     feeds pre-tag candidates without a title at all),
  2. the candidate's own ``entrant`` / ``selection`` / ``team`` field
     when it already names a competitor, or the ``home.name`` field
     that the n-way feed normalizer registers for outright markets, or
  3. the resolved Kalshi market title — matched against the candidate's
     ``n_way_field`` competitor names / ids (whichever appears in the
     title).

Returns ``None`` when no competitor can be derived; callers leave the
``metadata`` jsonb empty for those trades and settlement falls back to
its legacy binary path.

This mirrors the soccer-three-way helper in :mod:`.three_way` — the
only structural difference is that the n-way "field" can be 20+
competitors rather than a fixed home/draw/away frame, so we look up the
candidate's ``n_way_field`` to know which names to scan the title for
instead of just the two team names.
"""
from __future__ import annotations

import json
from typing import Any, Mapping


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


def _coerce_field(raw: Any) -> list[dict[str, Any]]:
    """Best-effort coercion of an ``n_way_field`` value into a list of
    competitor metadata dicts.  Accepts:

    * list[dict] — feed-normalized shape (pass through),
    * dict[str, Any] — process-local rating cache shape
      (``{name: rating}``); each key becomes a single-name dict,
    * anything else — empty list.
    """
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for c in raw:
            if isinstance(c, Mapping):
                out.append(dict(c))
        return out
    if isinstance(raw, dict):
        return [{"name": str(k)} for k in raw.keys()]
    return []


def _normalize_str(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


# Generic placeholders the n-way feed normalizer uses for the "FIELD"
# pseudo-competitor on outright markets — never a real entrant.
_FIELD_PLACEHOLDERS: frozenset[str] = frozenset({
    "field", "the field", "any other", "no", "yes",
})


def _is_real_competitor_name(name: str | None) -> bool:
    if not name:
        return False
    return name.strip().lower() not in _FIELD_PLACEHOLDERS


def derive_n_way_competitor(
    *, market_title: str | None, candidate: dict[str, Any] | None,
) -> dict[str, str] | None:
    """Return the competitor (``{"competitor_id": ..., "competitor_name": ...}``)
    the candidate is betting on, or ``None`` when no competitor can be
    derived.

    Caller is expected to gate this on the league's ``n_way_market``
    flag — non-n-way verticals (NBA / NFL / soccer h/d/a / etc.) should
    not invoke this helper because their titles can legitimately
    contain entrant-shaped substrings without implying an outright leg.

    The returned dict always carries at least one of ``competitor_id``
    or ``competitor_name`` when non-None; callers may stamp both onto
    ``trades.metadata`` so downstream analytics can join either way.
    """
    cand = candidate or {}

    # 1) Pre-stamped on the candidate metadata (feeds that already
    # know the per-leg semantics short-circuit every other check).
    md = _coerce_metadata(cand.get("metadata"))
    pre_id = (
        _normalize_str(md.get("competitor_id"))
        or _normalize_str(md.get("entrant_id"))
        or _normalize_str(md.get("competitor"))
        or _normalize_str(md.get("entrant"))
    )
    pre_name = (
        _normalize_str(md.get("competitor_name"))
        or _normalize_str(md.get("entrant_name"))
    )
    if pre_id or pre_name:
        out: dict[str, str] = {}
        if pre_id:
            out["competitor_id"] = pre_id
        if pre_name and _is_real_competitor_name(pre_name):
            out["competitor_name"] = pre_name
        elif pre_id and not pre_name:
            pass
        return out or None

    # 2) Candidate's own entrant / selection / team / home.name fields.
    field = _coerce_field(cand.get("n_way_field"))

    cand_name = (
        _normalize_str(cand.get("entrant"))
        or _normalize_str(cand.get("side_entrant"))
        or _normalize_str(cand.get("selection"))
        or _normalize_str(cand.get("team"))
    )
    home = cand.get("home")
    if cand_name is None and isinstance(home, Mapping):
        cand_name = _normalize_str(home.get("name"))

    cand_id = (
        _normalize_str(cand.get("competitor_id"))
        or _normalize_str(cand.get("entrant_id"))
    )

    if cand_name and not _is_real_competitor_name(cand_name):
        # The normalizer ships ``home.name = "FIELD"`` for the
        # placeholder side of an outright market — never a leg tag.
        cand_name = None

    # When the candidate names a competitor and we have a field, look
    # up the matching ``id`` so we stamp the canonical identifier
    # rather than the (potentially fuzzy) display name.
    if cand_name and not cand_id and field:
        for c in field:
            cn = _normalize_str(c.get("name"))
            if cn and cn.lower() == cand_name.lower():
                cand_id = _normalize_str(c.get("id"))
                # Use the field's canonical name spelling.
                cand_name = cn
                break

    if cand_id or cand_name:
        out = {}
        if cand_id:
            out["competitor_id"] = cand_id
        if cand_name:
            out["competitor_name"] = cand_name
        return out or None

    # 3) Parse the resolved Kalshi market title against the field's
    # competitor names / ids.  Pick the longest match so a title that
    # mentions both "Carlos Alcaraz" and "Alcaraz" resolves to the
    # full-name competitor without ambiguity.
    title = _normalize_str(market_title)
    if not title or not field:
        return None
    title_lc = title.lower()

    best: tuple[int, str | None, str | None] | None = None  # (length, id, name)
    for c in field:
        cn = _normalize_str(c.get("name"))
        cid = _normalize_str(c.get("id"))
        if cn and cn.lower() in title_lc:
            length = len(cn)
            if best is None or length > best[0]:
                best = (length, cid, cn)
        elif cid and cid.lower() in title_lc:
            # Fall back to the id in title — rare but supports feeds
            # that surface ticker-style identifiers ("VER", "HAM").
            length = len(cid)
            if best is None or length > best[0]:
                best = (length, cid, cn)
    if best is None:
        return None
    out = {}
    if best[1]:
        out["competitor_id"] = best[1]
    if best[2]:
        out["competitor_name"] = best[2]
    return out or None
