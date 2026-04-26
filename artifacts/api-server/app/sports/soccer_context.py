"""Build the structured ``game_context`` block for soccer swarm debates.

The ``lineup_rotation`` persona used to receive only prose guidance
("think about confirmed-XI deltas, midweek UCL fatigue, ..."); the
Sportradar soccer feeds (task #211) already populate
``sportradar_rosters`` and ``sportradar_injuries`` (the league-wide
"missing players" feed) for every soccer league we trade —
EPL / LALIGA / SERIEA / BUNDES / LIGUE1 / MLS / UCL — but until now
that signal sat idle.

This module wires those cached tables into the persona prompt:

* ``game_context.lineup_status.<side>.missing_players`` — the cached
  missing-players list for each side, status-filtered to the entries
  that actually move probability ("missing", "out", "doubtful",
  "suspended", "injured").
* ``game_context.lineup_status.<side>.roster_size`` — squad depth
  signal pulled from ``sportradar_rosters`` so the persona can tell a
  thin matchday squad from a deep one.
* ``game_context.lineup_status.<side>.lineup_severity_pp`` —
  deterministic per-side severity (probability points the persona
  should shave off the side's win share before LLM reasoning).  Tuned
  from the public lineup-impact studies: a missing GK is the single
  biggest swing (~3pp), missing forwards / wingers ~1.8-2pp, missing
  midfielders / centre-backs ~1.5pp, missing full-backs ~1.2pp.
  Doubtful (vs definitively missing) entries are downweighted 50%.
* ``game_context.lineup_status.net_home_swing_pp`` — the convenience
  delta ``away.severity − home.severity`` so the persona can swing the
  home win share without having to subtract two numbers in prose.

All DB reads are best-effort — a failure for either side collapses to
``[]`` for that team rather than blocking the debate, mirroring the
NFL context fail-soft contract in :mod:`app.sports.nfl_context`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .. import db


log = logging.getLogger("axiom.sports.soccer_context")


# Canonical league set the soccer vertical trades.  Mirrors the ticker
# prefixes registered in `app.verticals.soccer` and the league configs
# in `app.sports.leagues`.
SOCCER_LEAGUES: frozenset[str] = frozenset({
    "EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1", "MLS", "UCL",
})


def is_soccer_league(league: str | None) -> bool:
    """Case-insensitive membership check used by the runner integration
    so callers don't have to upper-case + frozenset-test inline."""
    if not league:
        return False
    return str(league).strip().upper() in SOCCER_LEAGUES


# Statuses surfaced by the sportradar `missing_players` feed.  Anything
# else (e.g. "active", "available") is filtered out so the persona
# prompt only contains genuine availability concerns.
_NOTABLE_STATUSES: frozenset[str] = frozenset({
    "missing", "out", "doubtful", "suspended", "injured",
})

# Definitive-absence statuses contribute the full positional weight;
# softer tags ("doubtful", "questionable") are downweighted.
_DOUBTFUL_STATUSES: frozenset[str] = frozenset({"doubtful", "questionable"})
_DOUBTFUL_DOWNWEIGHT = 0.5


# Probability-point impact per missing player by position bucket.  Keyed
# on the canonical 2-letter position codes the sportradar soccer feed
# emits (`G`, `D`, `M`, `F`) plus the extended codes the missing-players
# payload sometimes carries (`GK`, `CB`, `LB`, `RB`, `CM`, `AM`, `DM`,
# `LW`, `RW`, `CF`, `ST`, `WG`).  Anything we can't classify gets a
# neutral default so an unrecognised position never silently zeroes the
# severity.
_POSITION_WEIGHT_PP: dict[str, float] = {
    # Goalkeepers — single biggest swing (~3pp per public xG studies).
    "G": 0.030, "GK": 0.030,
    # Forwards / wingers / strikers (~1.8-2.0pp).
    "F": 0.020, "FW": 0.020, "ST": 0.020, "CF": 0.020,
    "LW": 0.018, "RW": 0.018, "WG": 0.018,
    # Midfielders (~1.5pp).
    "M": 0.015, "MF": 0.015, "CM": 0.015, "AM": 0.018, "DM": 0.015,
    # Defenders (~1.2-1.5pp).
    "D": 0.015, "DF": 0.015, "CB": 0.015,
    "LB": 0.012, "RB": 0.012,
}
_DEFAULT_PLAYER_WEIGHT_PP = 0.010

# Aggregate cap so a wholesale squad outage doesn't claim a 30pp edge.
# 12pp is roughly "half your XI is gone" — beyond that the persona
# should be reasoning about market suspension, not edge sizing.
_MAX_LINEUP_SEVERITY_PP = 0.12


def _classify_position(raw: str | None) -> str:
    """Coerce a sportradar position string into one of the keys in
    ``_POSITION_WEIGHT_PP`` when possible.  Returns the upper-cased raw
    string (truncated to 2 chars) when no match — callers fall back to
    the default per-player weight for anything unclassifiable."""
    if not raw:
        return ""
    pos = str(raw).strip().upper()
    if pos in _POSITION_WEIGHT_PP:
        return pos
    # Single-character codes (G/D/M/F) are the canonical sportradar form.
    if len(pos) >= 1 and pos[0] in _POSITION_WEIGHT_PP:
        return pos[0]
    # Fall back to first two chars (covers `LWG`, `RWB`, `CDM`, etc.).
    return pos[:2]


def _player_weight_pp(position: str | None, status: str) -> float:
    """Per-player severity contribution in probability points."""
    bucket = _classify_position(position)
    base = _POSITION_WEIGHT_PP.get(bucket, _DEFAULT_PLAYER_WEIGHT_PP)
    if status in _DOUBTFUL_STATUSES:
        base *= _DOUBTFUL_DOWNWEIGHT
    return base


def _compute_lineup_severity_pp(missing: list[dict[str, Any]]) -> float:
    """Deterministic severity score for a list of missing-player dicts.

    Sum of per-player weights, capped at ``_MAX_LINEUP_SEVERITY_PP``.
    Returned in *probability points* (0.020 == 2pp swing).
    """
    if not missing:
        return 0.0
    total = 0.0
    for row in missing:
        if not isinstance(row, dict):
            continue
        status = (row.get("status") or "").strip().lower() or "missing"
        if status not in _NOTABLE_STATUSES:
            continue
        total += _player_weight_pp(row.get("position"), status)
    return min(total, _MAX_LINEUP_SEVERITY_PP)


# ---------------------------------------------------------------------------
# DB readers (sportradar_injuries + sportradar_rosters)
# ---------------------------------------------------------------------------


async def _fetch_team_missing(
    league: str, team_name: str
) -> list[dict[str, Any]]:
    """Read status-filtered missing players for ``team_name`` from the
    soccer-extended ``sportradar_injuries`` cache.  Returns ``[]`` on
    any DB error so the debate isn't blocked by a feed outage."""
    if not team_name:
        return []
    try:
        rows = await db.fetch(
            """
            SELECT player_name, position, status, description
              FROM sportradar_injuries
             WHERE league = $1 AND team_name ILIKE $2
             ORDER BY status, position, player_name
            """,
            league,
            f"%{team_name}%",
        )
    except Exception as e:
        log.debug(
            "soccer missing-players DB read failed for %s/%s: %s",
            league, team_name, e,
        )
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        status = (r["status"] or "").strip().lower() or "missing"
        if status not in _NOTABLE_STATUSES:
            continue
        out.append({
            "player": r["player_name"] or "",
            "position": (r["position"] or "").upper(),
            "status": status,
            "description": (r["description"] or "")[:200],
        })
    return out


async def _fetch_team_roster_size(league: str, team_name: str) -> int:
    """Lightweight squad-depth signal — count of cached ``sportradar_rosters``
    rows for ``team_name``.  Returns 0 on any DB error or empty cache."""
    if not team_name:
        return 0
    try:
        n = await db.fetchval(
            """
            SELECT COUNT(*)
              FROM sportradar_rosters
             WHERE league = $1 AND team_name ILIKE $2
            """,
            league,
            f"%{team_name}%",
        )
    except Exception as e:
        log.debug(
            "soccer roster-size DB read failed for %s/%s: %s",
            league, team_name, e,
        )
        return 0
    try:
        return int(n or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


async def fetch_lineup_status(
    league: str | None,
    home_team: str | None,
    away_team: str | None,
) -> dict[str, Any] | None:
    """Build the ``game_context.lineup_status`` block for a soccer matchup.

    Returns ``None`` when the league isn't a soccer league we trade or
    when both team names are blank — callers treat ``None`` as "no
    lineup_status block" and skip the augmentation.
    """
    if not is_soccer_league(league):
        return None
    if not home_team and not away_team:
        return None
    league_u = str(league).strip().upper()

    # Fetch both sides' missing-players + roster-size counts in parallel —
    # they're independent DB reads and the swarm debate is on the user's
    # critical path.
    home_missing, away_missing, home_roster_n, away_roster_n = await asyncio.gather(
        _fetch_team_missing(league_u, home_team or ""),
        _fetch_team_missing(league_u, away_team or ""),
        _fetch_team_roster_size(league_u, home_team or ""),
        _fetch_team_roster_size(league_u, away_team or ""),
    )

    home_severity = _compute_lineup_severity_pp(home_missing)
    away_severity = _compute_lineup_severity_pp(away_missing)

    return {
        "league": league_u,
        "home": {
            "team": home_team or "",
            "missing_players": home_missing,
            "roster_size": home_roster_n,
            "lineup_severity_pp": round(home_severity, 4),
        },
        "away": {
            "team": away_team or "",
            "missing_players": away_missing,
            "roster_size": away_roster_n,
            "lineup_severity_pp": round(away_severity, 4),
        },
        # Convenience signed-delta in probability points: positive
        # numbers mean the AWAY side is more depleted than HOME (so the
        # home win share should drift up); negative means the reverse.
        "net_home_swing_pp": round(away_severity - home_severity, 4),
    }


async def build_game_context(
    league: str | None,
    home_team: str | None,
    away_team: str | None,
) -> dict[str, Any]:
    """Build the structured ``game_context`` payload for a soccer debate.

    Always returns a dict (never ``None``) so the runner can attach it
    unconditionally.  The ``lineup_status`` sub-field is only present
    when it could actually be built.
    """
    out: dict[str, Any] = {}
    lineup = await fetch_lineup_status(league, home_team, away_team)
    if lineup is not None:
        out["lineup_status"] = lineup
    return out


__all__ = [
    "SOCCER_LEAGUES",
    "is_soccer_league",
    "build_game_context",
    "fetch_lineup_status",
]
