"""30-year climatology baselines for the Kalshi-priced weather cities.

The Weather v6 model needs a long-run "what's normal for this date?"
baseline alongside the live observations from Open-Meteo, NWS, and the
HRRR mesoscale forecast.  This module owns:

* The ``weather_climatology`` table — one row per
  ``(location, day_of_year, metric)`` carrying a Fahrenheit value plus a
  ``source`` tag indicating whether it came from a NOAA NCEI 1991-2020
  Climate Normals download or from the embedded sinusoidal baseline we
  ship for tests / cold-start.
* :func:`backfill_climatology` — idempotent, populates the table from the
  embedded baseline for every supported city × day-of-year × metric.
* :func:`get_climatology` — process-local cache lookup; the cache is
  warmed on first call and refreshed via :func:`refresh_climatology_cache`.

The cache means lookups are O(1) dict reads — well under the 50ms target
for the v6 feature payload builder.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Iterable

from .. import db


log = logging.getLogger("axiom.feeds.weather_climatology")


# Metric vocabulary kept narrow on purpose — the v6 model only baselines
# against daily max / min / average temperature.  Adding precip/wind
# would mean another data source (NCEI station precip normals) and is
# out of scope for this task.
METRIC_TMAX = "tmax_f"
METRIC_TMIN = "tmin_f"
METRIC_TAVG = "tavg_f"
METRICS: tuple[str, ...] = (METRIC_TMAX, METRIC_TMIN, METRIC_TAVG)


# Annual normals for the seven cities the Kalshi weather contracts
# (KXHIGH/KXTEMP/KXLOW) cover.  Values are derived from NOAA NCEI's
# published 1991-2020 Climate Normals at the canonical airport/station
# for each city.  Stored here as (mean, half-range) pairs so a single
# cosine reconstructs the daily baseline; ``peak_doy`` is the warmest
# day of the year for the location.  See README in this dir for the
# upstream URLs used to source these.
_BASELINE: dict[str, dict[str, tuple[float, float, int]]] = {
    # city: { metric: (annual_mean_f, half_range_f, peak_doy) }
    "NYC": {
        METRIC_TMAX: (63.0, 22.0, 203),  # ~Jul 22
        METRIC_TMIN: (47.0, 22.0, 203),
        METRIC_TAVG: (55.0, 22.0, 203),
    },
    "AUS": {
        METRIC_TMAX: (80.0, 16.0, 213),  # ~Aug  1
        METRIC_TMIN: (59.0, 19.0, 213),
        METRIC_TAVG: (69.5, 17.5, 213),
    },
    "CHI": {
        METRIC_TMAX: (60.0, 28.0, 202),  # ~Jul 21
        METRIC_TMIN: (43.0, 28.0, 202),
        METRIC_TAVG: (51.5, 28.0, 202),
    },
    "DEN": {
        METRIC_TMAX: (65.0, 25.0, 202),
        METRIC_TMIN: (37.0, 22.0, 202),
        METRIC_TAVG: (51.0, 23.5, 202),
    },
    "MIA": {
        METRIC_TMAX: (84.0, 8.0, 217),   # ~Aug  5
        METRIC_TMIN: (70.0, 12.0, 217),
        METRIC_TAVG: (77.0, 10.0, 217),
    },
    "LAX": {
        METRIC_TMAX: (75.0, 11.0, 227),  # ~Aug 15
        METRIC_TMIN: (58.0, 9.0, 227),
        METRIC_TAVG: (66.5, 10.0, 227),
    },
    "PHL": {
        METRIC_TMAX: (65.0, 23.0, 203),
        METRIC_TMIN: (47.0, 23.0, 203),
        METRIC_TAVG: (56.0, 23.0, 203),
    },
}


def baseline_value(city_code: str, day_of_year: int, metric: str) -> float | None:
    """Pure-function sinusoidal baseline.

    Returns ``None`` for unknown city/metric so callers can degrade
    cleanly (e.g. a new city was added but climatology hasn't been
    backfilled yet).
    """
    city = _BASELINE.get(city_code.upper())
    if not city:
        return None
    spec = city.get(metric)
    if not spec:
        return None
    mean, half_range, peak_doy = spec
    # Cosine peaking at peak_doy, period = 366 days so leap years line up.
    phase = 2.0 * math.pi * ((day_of_year - peak_doy) / 366.0)
    return round(mean + half_range * math.cos(phase), 2)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def _baseline_rows(cities: Iterable[str] | None = None) -> list[tuple]:
    """Materialise every (city, doy, metric) row from the embedded baseline."""
    cities = tuple(cities) if cities is not None else tuple(_BASELINE.keys())
    rows: list[tuple] = []
    for city in cities:
        spec = _BASELINE.get(city.upper())
        if not spec:
            continue
        for metric in METRICS:
            for doy in range(1, 367):
                v = baseline_value(city, doy, metric)
                if v is None:
                    continue
                rows.append((city.upper(), doy, metric, float(v)))
    return rows


async def backfill_climatology(cities: Iterable[str] | None = None) -> int:
    """Idempotently insert sinusoidal baseline rows.

    Existing rows are preserved (``ON CONFLICT DO NOTHING``) so an
    operator who upserts real NCEI normals later isn't overwritten by
    the baseline.  Returns the number of rows the planner attempted to
    insert (not necessarily the count actually written when conflicts
    suppress some).
    """
    rows = _baseline_rows(cities)
    if not rows:
        return 0
    try:
        async with db.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO weather_climatology
                  (location, day_of_year, metric, value_f, source, updated_at)
                VALUES ($1, $2, $3, $4, 'sinusoidal_baseline', now())
                ON CONFLICT (location, day_of_year, metric) DO NOTHING
                """,
                rows,
            )
    except Exception as e:
        log.warning("weather_climatology backfill failed: %s", e)
        return 0
    log.info("weather_climatology backfill: planned %d rows", len(rows))
    return len(rows)


# ---------------------------------------------------------------------------
# Cache + lookups
# ---------------------------------------------------------------------------


# Map (city_upper, doy, metric) -> value_f.  Loaded on demand from the
# DB; falls through to the in-process baseline if the row is missing or
# the DB read fails.  Keeps live lookups well under the 50ms target.
_CACHE: dict[tuple[str, int, str], float] = {}
_CACHE_READY = False
_CACHE_LOCK = asyncio.Lock()


async def refresh_climatology_cache() -> int:
    """(Re)load every climatology row into the in-process cache.

    Returns the number of rows loaded.  Safe to call repeatedly.
    """
    global _CACHE_READY
    try:
        rows = await db.fetch(
            "SELECT location, day_of_year, metric, value_f FROM weather_climatology"
        )
    except Exception as e:
        log.warning("weather_climatology cache load failed: %s", e)
        # Even on DB failure, populate the cache from the embedded baseline
        # so live lookups don't return None and break the payload builder.
        for (city, doy, metric, v) in _baseline_rows():
            _CACHE[(city, int(doy), metric)] = float(v)
        _CACHE_READY = True
        return len(_CACHE)
    new_cache: dict[tuple[str, int, str], float] = {}
    for r in rows:
        new_cache[(str(r["location"]).upper(), int(r["day_of_year"]), str(r["metric"]))] = (
            float(r["value_f"])
        )
    # Also seed any missing (city, doy, metric) from the baseline so an
    # incomplete DB doesn't surface as ``None`` to consumers.
    for (city, doy, metric, v) in _baseline_rows():
        new_cache.setdefault((city, int(doy), metric), float(v))
    _CACHE.clear()
    _CACHE.update(new_cache)
    _CACHE_READY = True
    return len(_CACHE)


async def _ensure_cache() -> None:
    if _CACHE_READY:
        return
    async with _CACHE_LOCK:
        if _CACHE_READY:
            return
        await refresh_climatology_cache()


def get_climatology_sync(city_code: str, day_of_year: int, metric: str) -> float | None:
    """Synchronous lookup against the in-process cache.

    Falls through to the embedded baseline if the cache hasn't been
    warmed (so callers in sync code paths still get a sensible value).
    """
    key = (city_code.upper(), int(day_of_year), metric)
    if key in _CACHE:
        return _CACHE[key]
    return baseline_value(city_code, day_of_year, metric)


async def get_climatology(city_code: str, day_of_year: int, metric: str) -> float | None:
    """Async lookup that warms the cache on first call.

    Designed to be called from the live feature payload builder; cache
    hits return in microseconds, the first miss triggers a single DB
    read for *all* climatology rows (typically ~7,700 entries).
    """
    await _ensure_cache()
    return get_climatology_sync(city_code, day_of_year, metric)


def supported_cities() -> tuple[str, ...]:
    return tuple(_BASELINE.keys())
