"""Build the structured `game_context` block for NFL swarm debates.

The persona prompt for NFL games used to consist of *prose* exhortations
("consider snap counts, OL/DL injuries, and weather") with no actual
data attached.  This module wires real numbers into the prompt:

* ``game_context.weather`` — current/forecast surface conditions for
  outdoor stadiums, fetched from Open-Meteo (free, keyless).  Indoor
  stadiums (domes + retractable-roof venues that play closed) are
  flagged ``venue_type = "indoor"`` so the persona augment can short-
  circuit the wind/precip discussion.
* ``game_context.player_status`` — OL/DL availability + skill-position
  snap-count percentages.  OL/DL availability is read from the existing
  ``sportradar_injuries`` cache (refreshed every 15 min by the feed
  scheduler).  Snap-count percentages come from the
  ``nfl_snap_counts`` cache populated by ``app.feeds.nfl_snap_counts``;
  rows that haven't been backfilled yet surface as ``[]`` rather than
  blocking the debate.

All HTTP / DB lookups are best-effort — a failure in any one of them
returns ``None`` for the corresponding sub-field so the debate still
runs with whatever signals were available.  The caller is expected to
attach the result to the candidate dict and let
``persona_context_augment`` render it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any

import httpx

from .. import db
from ..v1_archive import features as v1_features
from .nfl_stadiums import Stadium, resolve_stadium


log = logging.getLogger("axiom.sports.nfl_context")


# In-process TTL cache for Open-Meteo lookups.  Stadium weather doesn't
# change minute-to-minute, and a single Sunday slate spawns 13+ NFL
# debates within a few minutes — caching for 10 minutes keeps us well
# under Open-Meteo's free-tier budget while staying fresh enough for
# pre-kickoff windows.
_WEATHER_TTL_S = 10 * 60
_weather_cache: dict[str, tuple[float, dict[str, Any]]] = {}


# Skill-position whitelists used to filter the per-team injury list
# down to the players who actually move spreads.  OL/DL guard the
# trenches and dominate run-game / pass-rush expectations; QB/RB/WR/TE
# move the moneyline + totals on snap-count-driven usage shifts.
_OL_POSITIONS = frozenset({"OL", "T", "G", "C", "OT", "OG", "LT", "LG", "RG", "RT"})
_DL_POSITIONS = frozenset({"DL", "DE", "DT", "NT", "EDGE"})
_SKILL_POSITIONS = frozenset({"QB", "RB", "FB", "WR", "TE"})

# Statuses that materially affect availability.  We surface all of
# these so the persona can weight them — "Questionable" is non-binary
# but historically ~70% play, while "Out" / "IR" / "Doubtful" are
# de-facto scratches.
_NOTABLE_STATUSES = frozenset({
    "out", "ir", "injured reserve", "doubtful", "questionable",
    "suspended", "pup", "physically unable to perform",
})


# ---------------------------------------------------------------------------
# Weather (Open-Meteo, keyless)
# ---------------------------------------------------------------------------


async def _fetch_open_meteo_current(
    lat: float, lon: float, timeout_s: float = 6.0
) -> dict[str, Any] | None:
    """Fetch current surface conditions from Open-Meteo.

    Returns the parsed ``current`` block (Fahrenheit + mph + inches) or
    ``None`` on any non-200 / network failure.  Open-Meteo is keyless
    and rate-limited generously; we still defend with a short timeout
    so a single hung request doesn't stall the debate.
    """
    timeout = httpx.Timeout(timeout_s, connect=3.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": (
                        "temperature_2m,wind_speed_10m,wind_gusts_10m,"
                        "precipitation,weather_code,relative_humidity_2m"
                    ),
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "precipitation_unit": "inch",
                },
            )
    except httpx.HTTPError as e:
        log.debug("open-meteo NFL fetch failed @ (%.3f,%.3f): %s", lat, lon, e)
        return None
    if r.status_code >= 400:
        log.debug("open-meteo NFL HTTP %s @ (%.3f,%.3f)", r.status_code, lat, lon)
        return None
    try:
        return (r.json() or {}).get("current") or None
    except Exception:
        return None


async def fetch_weather(
    home_team: str | None,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Build the ``game_context.weather`` block for ``home_team``.

    Returns a dict with at least ``venue`` + ``venue_type`` keys.  For
    outdoor stadiums we include ``temperature_f``, ``wind_mph``,
    ``wind_gust_mph`` and ``precip_in_per_hr`` when Open-Meteo answered.
    For indoor / dome stadiums we short-circuit with
    ``weather_irrelevant: True`` so the persona augment can drop the
    wind / precip discussion entirely.

    Returns ``None`` only when ``home_team`` is unknown — callers treat
    that as "no weather block".
    """
    stadium: Stadium | None = resolve_stadium(home_team)
    if stadium is None:
        return None

    if stadium.indoor:
        return {
            "venue": stadium.name,
            "venue_type": "indoor",
            "weather_irrelevant": True,
        }

    # V1-era backtest path: when the caller supplies ``observed_at``
    # (the scheduled-start of a historical game), prefer a reading from
    # ``v1_archive.weather_observations`` over a live Open-Meteo call so
    # the persona sees the conditions that actually prevailed at game
    # time.  Live cycles pass ``observed_at=None`` and skip this branch.
    if observed_at is not None:
        v1_block = await v1_features.weather_for_coords(
            stadium.lat, stadium.lon, observed_at,
        )
        if v1_block is not None:
            return {
                "venue": stadium.name,
                "venue_type": "outdoor",
                "temperature_f": v1_block.get("temperature_f"),
                "source": "v1_archive",
                "v1_city_code": v1_block.get("city_code"),
                "v1_observed_at": v1_block.get("observed_at"),
            }

    cache_key = f"{stadium.lat:.3f},{stadium.lon:.3f}"
    now = time.monotonic()
    cached = _weather_cache.get(cache_key)
    if cached is not None and now - cached[0] < _WEATHER_TTL_S:
        return dict(cached[1])

    current = await _fetch_open_meteo_current(stadium.lat, stadium.lon)
    block: dict[str, Any] = {
        "venue": stadium.name,
        "venue_type": "outdoor",
    }
    if current is None:
        block["forecast_unavailable"] = True
        return block

    def _f(key: str) -> float | None:
        v = current.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    block["temperature_f"] = _f("temperature_2m")
    block["wind_mph"] = _f("wind_speed_10m")
    block["wind_gust_mph"] = _f("wind_gusts_10m")
    block["precip_in_per_hr"] = _f("precipitation")
    block["humidity_pct"] = _f("relative_humidity_2m")
    block["weather_code"] = current.get("weather_code")

    _weather_cache[cache_key] = (now, dict(block))
    return block


# ---------------------------------------------------------------------------
# Player status (sportradar_injuries + nfl_snap_counts)
# ---------------------------------------------------------------------------


def _classify_position(raw: str | None) -> str | None:
    """Bucket a sportradar position string into ``OL`` / ``DL`` /
    ``SKILL``, or ``None`` for positions we don't surface (ST, K, P)."""
    if not raw:
        return None
    pos = str(raw).strip().upper()
    if pos in _OL_POSITIONS:
        return "OL"
    if pos in _DL_POSITIONS:
        return "DL"
    if pos in _SKILL_POSITIONS:
        return "SKILL"
    return None


async def _fetch_team_injuries(team_name: str) -> list[dict[str, Any]]:
    """Read OL/DL/SKILL injuries for ``team_name`` from
    ``sportradar_injuries``.  Returns ``[]`` on any DB error so the
    debate isn't blocked by a feed outage."""
    if not team_name:
        return []
    try:
        rows = await db.fetch(
            """
            SELECT player_name, position, status, description
              FROM sportradar_injuries
             WHERE league = 'NFL' AND team_name ILIKE $1
             ORDER BY position, player_name
            """,
            f"%{team_name}%",
        )
    except Exception as e:
        log.debug("nfl injuries DB read failed for %s: %s", team_name, e)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        bucket = _classify_position(r["position"])
        if bucket is None:
            continue
        status = (r["status"] or "").strip().lower()
        if status and status not in _NOTABLE_STATUSES:
            continue
        out.append({
            "player": r["player_name"] or "",
            "position": (r["position"] or "").upper(),
            "bucket": bucket,
            "status": (r["status"] or "").upper() or "UNKNOWN",
            "description": (r["description"] or "")[:200],
        })
    return out


async def _fetch_team_snap_counts(team_name: str) -> list[dict[str, Any]]:
    """Read top skill-position snap percentages for ``team_name`` from
    ``nfl_snap_counts``.  Returns ``[]`` when the cache table doesn't
    exist (fresh checkout) or no rows are present yet — the augment
    falls back to "snap_counts: unavailable" in that case."""
    if not team_name:
        return []
    try:
        rows = await db.fetch(
            """
            SELECT player_name, position, snap_pct, week, season
              FROM nfl_snap_counts
             WHERE team_name ILIKE $1
               AND position = ANY($2::text[])
             ORDER BY season DESC, week DESC, snap_pct DESC NULLS LAST
             LIMIT 8
            """,
            f"%{team_name}%",
            list(_SKILL_POSITIONS),
        )
    except Exception as e:
        # Most likely cause: the table hasn't been created yet (the
        # poller runs out of band).  Logged at debug so a fresh env
        # doesn't spam warnings.
        log.debug("nfl snap counts DB read failed for %s: %s", team_name, e)
        return []
    return [
        {
            "player": r["player_name"] or "",
            "position": (r["position"] or "").upper(),
            "snap_pct": float(r["snap_pct"]) if r["snap_pct"] is not None else None,
            "week": r["week"],
            "season": r["season"],
        }
        for r in rows
    ]


async def fetch_player_status(
    home_team: str | None, away_team: str | None
) -> dict[str, Any] | None:
    """Build the ``game_context.player_status`` block for the matchup.

    Returns ``{"home": {...}, "away": {...}}`` where each side carries
    ``team``, ``injuries`` (OL/DL/SKILL filtered + status-filtered) and
    ``snap_counts`` (top skill-position percentages from the most
    recent week available).  Returns ``None`` when both team names are
    blank — callers treat that as "no player_status block".
    """
    if not home_team and not away_team:
        return None

    # Fetch both teams' injuries + snap counts in parallel — they're
    # independent DB reads and the swarm debate is on the user's
    # critical path.
    home_injuries, away_injuries, home_snaps, away_snaps = await asyncio.gather(
        _fetch_team_injuries(home_team or ""),
        _fetch_team_injuries(away_team or ""),
        _fetch_team_snap_counts(home_team or ""),
        _fetch_team_snap_counts(away_team or ""),
    )

    return {
        "home": {
            "team": home_team or "",
            "injuries": home_injuries,
            "snap_counts": home_snaps,
        },
        "away": {
            "team": away_team or "",
            "injuries": away_injuries,
            "snap_counts": away_snaps,
        },
    }


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


async def build_game_context(
    home_team: str | None,
    away_team: str | None,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the structured ``game_context`` payload for an NFL debate.

    Always returns a dict (never ``None``) so the runner can attach it
    unconditionally.  Sub-fields are present when they could be built
    and absent when they couldn't, which lets the augment branch on
    real availability rather than guessing from the league name.
    """
    out: dict[str, Any] = {}
    weather, player_status = await asyncio.gather(
        fetch_weather(home_team, observed_at=observed_at),
        fetch_player_status(home_team, away_team),
    )
    if weather is not None:
        out["weather"] = weather
    if player_status is not None:
        out["player_status"] = player_status
    return out


__all__ = [
    "build_game_context",
    "fetch_weather",
    "fetch_player_status",
]
