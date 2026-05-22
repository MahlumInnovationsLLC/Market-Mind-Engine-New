"""Build the structured ``game_context.weather`` block for MLB debates.

MLB is heavily weather-sensitive (wind direction at hitter-friendly
parks shifts totals by a full run; cold + wet days suppress homers
and pitching command).  This module is intentionally narrow: it
resolves an MLB home team to a ballpark + indoor flag and produces a
weather block compatible with :func:`app.sports.persona_context_augment`.

Unlike :mod:`app.sports.nfl_context`, MLB does NOT pull a live HTTP
weather feed today — we rely entirely on the V1 archive when an
``observed_at`` is supplied (historical replays) and on the existing
``weather_observations`` table when we have a current reading for the
nearest tracked Kalshi city.  This keeps the dependency surface
trivial while still consuming V1 data for backtests.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..v1_archive import features as v1_features


log = logging.getLogger("axiom.sports.mlb_context")


@dataclass(frozen=True)
class Ballpark:
    team: str
    market: str
    name: str
    lat: float
    lon: float
    roof: bool  # True for fixed dome / closed-roof venues


# Representative MLB ballparks (2025).  Roof flag covers fixed domes
# AND retractable-roof venues that habitually play closed in adverse
# weather; outdoor games at those parks won't get a V1 weather block,
# which matches the persona augment's "indoor / dome: N/A" treatment.
_BALLPARKS: tuple[Ballpark, ...] = (
    Ballpark("Yankees",    "New York",     "Yankee Stadium",         40.8296, -73.9262, False),
    Ballpark("Mets",       "New York",     "Citi Field",             40.7571, -73.8458, False),
    Ballpark("Phillies",   "Philadelphia", "Citizens Bank Park",     39.9061, -75.1665, False),
    Ballpark("Red Sox",    "Boston",       "Fenway Park",            42.3467, -71.0972, False),
    Ballpark("Cubs",       "Chicago",      "Wrigley Field",          41.9484, -87.6553, False),
    Ballpark("White Sox",  "Chicago",      "Guaranteed Rate Field",  41.8300, -87.6338, False),
    Ballpark("Dodgers",    "Los Angeles",  "Dodger Stadium",         34.0739, -118.2400, False),
    Ballpark("Angels",     "Los Angeles",  "Angel Stadium",          33.8003, -117.8827, False),
    Ballpark("Astros",     "Houston",      "Minute Maid Park",       29.7573, -95.3555, True),
    Ballpark("Rangers",    "Texas",        "Globe Life Field",       32.7473, -97.0817, True),
    Ballpark("Mariners",   "Seattle",      "T-Mobile Park",          47.5914, -122.3325, True),
    Ballpark("Athletics",  "Oakland",      "Oakland Coliseum",       37.7516, -122.2005, False),
    Ballpark("Giants",     "San Francisco","Oracle Park",            37.7786, -122.3893, False),
    Ballpark("Padres",     "San Diego",    "Petco Park",             32.7073, -117.1573, False),
    Ballpark("Rockies",    "Colorado",     "Coors Field",            39.7559, -104.9942, False),
    Ballpark("Diamondbacks","Arizona",     "Chase Field",            33.4453, -112.0667, True),
    Ballpark("Royals",     "Kansas City",  "Kauffman Stadium",       39.0517, -94.4803, False),
    Ballpark("Twins",      "Minnesota",    "Target Field",           44.9817, -93.2776, False),
    Ballpark("Cardinals",  "St. Louis",    "Busch Stadium",          38.6226, -90.1928, False),
    Ballpark("Brewers",    "Milwaukee",    "American Family Field",  43.0280, -87.9712, True),
    Ballpark("Pirates",    "Pittsburgh",   "PNC Park",               40.4469, -80.0058, False),
    Ballpark("Reds",       "Cincinnati",   "Great American Ball Park",39.0975, -84.5066, False),
    Ballpark("Guardians",  "Cleveland",    "Progressive Field",      41.4962, -81.6852, False),
    Ballpark("Tigers",     "Detroit",      "Comerica Park",          42.3390, -83.0485, False),
    Ballpark("Orioles",    "Baltimore",    "Oriole Park at Camden Yards",39.2839, -76.6217, False),
    Ballpark("Nationals",  "Washington",   "Nationals Park",         38.8730, -77.0074, False),
    Ballpark("Braves",     "Atlanta",      "Truist Park",            33.8908, -84.4677, False),
    Ballpark("Marlins",    "Miami",        "loanDepot park",         25.7781, -80.2197, True),
    Ballpark("Rays",       "Tampa Bay",    "Tropicana Field",        27.7682, -82.6534, True),
    Ballpark("Blue Jays",  "Toronto",      "Rogers Centre",          43.6414, -79.3894, True),
)


_LOOKUP: dict[str, Ballpark] = {}
for _p in _BALLPARKS:
    _LOOKUP[_p.team.lower()] = _p
    _LOOKUP[_p.market.lower()] = _p
    _LOOKUP[f"{_p.market.lower()} {_p.team.lower()}"] = _p


def resolve_ballpark(home_team: str | None) -> Ballpark | None:
    if not home_team:
        return None
    return _LOOKUP.get(str(home_team).strip().lower())


async def fetch_weather(
    home_team: str | None,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a weather block for the MLB game's home ballpark.

    Domes / fixed-roof parks short-circuit to ``weather_irrelevant``.
    For outdoor parks we attempt the V1-archive lookup when
    ``observed_at`` is supplied; otherwise we return a minimal
    outdoor block without a forecast (no live HTTP fetch is wired
    yet — that's an explicit non-goal of this module).
    """
    park = resolve_ballpark(home_team)
    if park is None:
        return None
    if park.roof:
        return {
            "venue": park.name,
            "venue_type": "indoor",
            "weather_irrelevant": True,
        }

    block: dict[str, Any] = {"venue": park.name, "venue_type": "outdoor"}
    if observed_at is not None:
        v1_block = await v1_features.weather_for_coords(
            park.lat, park.lon, observed_at,
        )
        if v1_block is not None:
            block["temperature_f"] = v1_block.get("temperature_f")
            block["source"] = "v1_archive"
            block["v1_city_code"] = v1_block.get("city_code")
            block["v1_observed_at"] = v1_block.get("observed_at")
            return block
    block["forecast_unavailable"] = True
    return block


async def build_game_context(
    home_team: str | None,
    away_team: str | None,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the MLB ``game_context`` payload (weather only today)."""
    out: dict[str, Any] = {}
    weather = await fetch_weather(home_team, observed_at=observed_at)
    if weather is not None:
        out["weather"] = weather
    return out


__all__ = [
    "Ballpark",
    "resolve_ballpark",
    "fetch_weather",
    "build_game_context",
]
