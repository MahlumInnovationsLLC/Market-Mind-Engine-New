"""Build the structured ``game_context.weather`` block for PGA / golf debates.

Golf weather sits in two very different regimes:

* **Wind / precip** drive the scoring distribution at links and
  exposed-coastal courses (heavy wind => higher scoring => the field's
  cut line widens).  These are the signals that move spread / cut /
  matchup markets.
* **Wave-AM-vs-PM splits** show up at courses with morning fog or
  afternoon thunderstorm risk.

This module is intentionally narrow: it resolves a tournament's host
course (or a free-form ``venue`` field) to a lat/lon and pulls the
nearest V1 weather observation when ``observed_at`` is supplied.  No
live HTTP fetch is wired — that's an explicit non-goal here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..v1_archive import features as v1_features


log = logging.getLogger("axiom.sports.golf_context")


@dataclass(frozen=True)
class Course:
    name: str
    aliases: tuple[str, ...]
    lat: float
    lon: float


# Representative PGA host courses.  This is intentionally a short list
# (the V1 weather city map only covers seven Kalshi cities anyway —
# courses outside ~250mi of one of them won't get a reading).  More
# courses can be appended without changing the lookup contract.
_COURSES: tuple[Course, ...] = (
    Course("Augusta National",      ("masters", "augusta"),                   33.5026, -82.0220),
    Course("Pebble Beach",          ("pebble", "pebble beach"),               36.5694, -121.9489),
    Course("Bay Hill",              ("api", "bay hill", "arnold palmer"),     28.4644, -81.5169),
    Course("TPC Sawgrass",          ("players", "sawgrass"),                  30.1975, -81.3937),
    Course("Riviera",               ("genesis", "riviera"),                   34.0490, -118.5040),
    Course("Torrey Pines",          ("farmers", "torrey pines"),              32.8961, -117.2533),
    Course("Bethpage Black",        ("bethpage", "bethpage black"),           40.7415, -73.4574),
    Course("Winged Foot",           ("winged foot",),                         40.9510, -73.7641),
    Course("Quail Hollow",          ("wells fargo", "quail hollow"),          35.1517, -80.8200),
    Course("Muirfield Village",     ("memorial", "muirfield village"),        40.1503, -83.0867),
    Course("Colonial",              ("colonial", "charles schwab"),           32.7090, -97.3672),
    Course("TPC Scottsdale",        ("waste mgmt", "tpc scottsdale", "phoenix open"),
                                                                              33.6379, -111.9018),
)


_LOOKUP: dict[str, Course] = {}
for _c in _COURSES:
    _LOOKUP[_c.name.lower()] = _c
    for _a in _c.aliases:
        _LOOKUP[_a.lower()] = _c


def resolve_course(venue: str | None) -> Course | None:
    """Resolve a tournament name / course alias to a :class:`Course`.

    Match is on substring presence so "The Masters at Augusta National"
    and "Masters" both hit Augusta National.  Returns ``None`` for any
    venue we don't have coordinates for — callers treat that as "no
    weather block" and the persona augment skips the conditions line.
    """
    if not venue:
        return None
    key = str(venue).strip().lower()
    if not key:
        return None
    # Exact / alias hit first.
    hit = _LOOKUP.get(key)
    if hit is not None:
        return hit
    # Substring match — pick the alias hit with the longest match so
    # "tpc sawgrass players" prefers Sawgrass over a one-letter coincidence.
    best: tuple[int, Course] | None = None
    for alias, course in _LOOKUP.items():
        if alias in key and (best is None or len(alias) > best[0]):
            best = (len(alias), course)
    return best[1] if best else None


async def fetch_weather(
    venue: str | None,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a weather block for the supplied tournament venue.

    Returns ``None`` when the course is unknown.  When ``observed_at``
    is supplied and the V1 archive has a nearby reading, the temperature
    and provenance fields are filled in.  Otherwise we return a
    ``forecast_unavailable: True`` outdoor block so the persona augment
    still gets a venue line to render.
    """
    course = resolve_course(venue)
    if course is None:
        return None
    block: dict[str, Any] = {"venue": course.name, "venue_type": "outdoor"}
    if observed_at is not None:
        v1_block = await v1_features.weather_for_coords(
            course.lat, course.lon, observed_at,
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
    venue: str | None,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the golf ``game_context`` payload (weather only)."""
    out: dict[str, Any] = {}
    weather = await fetch_weather(venue, observed_at=observed_at)
    if weather is not None:
        out["weather"] = weather
    return out


__all__ = [
    "Course",
    "resolve_course",
    "fetch_weather",
    "build_game_context",
]
