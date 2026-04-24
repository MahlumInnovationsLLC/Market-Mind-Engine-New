"""NFL home stadium reference data (lat/lon + indoor flag).

Used by `app.sports.nfl_context.fetch_weather` to decide whether the
matchup is exposed to wind / precip / cold (outdoor) or sealed off
(domes + retractable roofs that play closed by default).

The lookup is keyed by both the team's market name (``"Green Bay"``)
and the team mascot / nickname (``"Packers"``) — the orchestrator and
sportradar feeds pass the names in different casings depending on the
upstream source, so we accept any of them in `resolve_stadium`.

Coordinates are stadium centroids (Wikipedia / Google Earth) — accurate
to ~50 m, which is more than enough for a 2 m surface forecast pulled
from the nearest grid cell of a mesoscale model.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stadium:
    team: str           # canonical short name (mascot)
    market: str         # market / city name
    name: str           # stadium name
    lat: float
    lon: float
    indoor: bool        # True for domes + retractable roofs that play closed


# 32 NFL home stadiums (2025 season).  ``indoor=True`` covers both
# fixed domes (Caesars Superdome) and retractable-roof venues that
# overwhelmingly play closed (NRG Stadium, AT&T Stadium, State Farm
# Stadium, Allegiant Stadium, Lucas Oil, Mercedes-Benz, U.S. Bank,
# Ford Field, Lumen Field is OUTDOOR).
_STADIUMS: tuple[Stadium, ...] = (
    Stadium("Cardinals",   "Arizona",       "State Farm Stadium",        33.5276, -112.2626, True),
    Stadium("Falcons",     "Atlanta",       "Mercedes-Benz Stadium",     33.7553, -84.4006,  True),
    Stadium("Ravens",      "Baltimore",     "M&T Bank Stadium",          39.2780, -76.6227,  False),
    Stadium("Bills",       "Buffalo",       "Highmark Stadium",          42.7738, -78.7870,  False),
    Stadium("Panthers",    "Carolina",      "Bank of America Stadium",   35.2258, -80.8528,  False),
    Stadium("Bears",       "Chicago",       "Soldier Field",             41.8623, -87.6167,  False),
    Stadium("Bengals",     "Cincinnati",    "Paycor Stadium",            39.0954, -84.5160,  False),
    Stadium("Browns",      "Cleveland",     "Cleveland Browns Stadium",  41.5061, -81.6995,  False),
    Stadium("Cowboys",     "Dallas",        "AT&T Stadium",              32.7473, -97.0945,  True),
    Stadium("Broncos",     "Denver",        "Empower Field at Mile High", 39.7439, -105.0201, False),
    Stadium("Lions",       "Detroit",       "Ford Field",                42.3400, -83.0456,  True),
    Stadium("Packers",     "Green Bay",     "Lambeau Field",             44.5013, -88.0622,  False),
    Stadium("Texans",      "Houston",       "NRG Stadium",               29.6847, -95.4107,  True),
    Stadium("Colts",       "Indianapolis",  "Lucas Oil Stadium",         39.7601, -86.1639,  True),
    Stadium("Jaguars",     "Jacksonville",  "EverBank Stadium",          30.3239, -81.6373,  False),
    Stadium("Chiefs",      "Kansas City",   "GEHA Field at Arrowhead",   39.0490, -94.4839,  False),
    Stadium("Raiders",     "Las Vegas",     "Allegiant Stadium",         36.0909, -115.1830, True),
    Stadium("Chargers",    "Los Angeles",   "SoFi Stadium",              33.9535, -118.3392, True),
    Stadium("Rams",        "Los Angeles",   "SoFi Stadium",              33.9535, -118.3392, True),
    Stadium("Dolphins",    "Miami",         "Hard Rock Stadium",         25.9580, -80.2389,  False),
    Stadium("Vikings",     "Minnesota",     "U.S. Bank Stadium",         44.9737, -93.2581,  True),
    Stadium("Patriots",    "New England",   "Gillette Stadium",          42.0909, -71.2643,  False),
    Stadium("Saints",      "New Orleans",   "Caesars Superdome",         29.9511, -90.0812,  True),
    Stadium("Giants",      "New York",      "MetLife Stadium",           40.8135, -74.0745,  False),
    Stadium("Jets",        "New York",      "MetLife Stadium",           40.8135, -74.0745,  False),
    Stadium("Eagles",      "Philadelphia",  "Lincoln Financial Field",   39.9008, -75.1675,  False),
    Stadium("Steelers",    "Pittsburgh",    "Acrisure Stadium",          40.4468, -80.0158,  False),
    Stadium("49ers",       "San Francisco", "Levi's Stadium",            37.4030, -121.9698, False),
    Stadium("Seahawks",    "Seattle",       "Lumen Field",               47.5952, -122.3316, False),
    Stadium("Buccaneers",  "Tampa Bay",     "Raymond James Stadium",     27.9759, -82.5033,  False),
    Stadium("Titans",      "Tennessee",     "Nissan Stadium",            36.1665, -86.7713,  False),
    Stadium("Commanders",  "Washington",    "Northwest Stadium",         38.9077, -76.8645,  False),
)


# Build (lower-cased nickname OR market OR "market mascot") → Stadium.
_LOOKUP: dict[str, Stadium] = {}
for _s in _STADIUMS:
    _LOOKUP[_s.team.lower()] = _s
    _LOOKUP[_s.market.lower()] = _s
    _LOOKUP[f"{_s.market.lower()} {_s.team.lower()}"] = _s


def resolve_stadium(home_team: str | None) -> Stadium | None:
    """Look up a stadium row by team name, market, or "market team".

    Returns ``None`` for unknown / empty inputs so callers can degrade
    gracefully (no weather block in the prompt).
    """
    if not home_team:
        return None
    key = str(home_team).strip().lower()
    if not key:
        return None
    if key in _LOOKUP:
        return _LOOKUP[key]
    # Fall back to a substring match on the mascot — sportradar
    # sometimes ships "Green Bay Packers" while Kalshi ships "Packers".
    for k, v in _LOOKUP.items():
        if key in k or k in key:
            return v
    return None


__all__ = ["Stadium", "resolve_stadium"]
