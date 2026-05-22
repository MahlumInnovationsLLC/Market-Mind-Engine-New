"""Read-only feature-source helpers backed by the V1 archive schema.

Phase 2 of task #731 loaded three V1 tables under ``v1_archive``:

* ``weather_observations`` — historical Kalshi-city weather readings.
* ``line_snapshots``       — 4.7M sportsbook line snapshots.
* ``llm_predictions``      — historical LLM-debate calls (see
  :mod:`app.v1_archive.llm_backtest` for the replay harness).

This module exposes minimal, fail-soft accessors for the first two so
the live feature builders can fall back to V1 data when the live tables
don't cover the lookup window (typical for backtests / V1-era games).

Every accessor:

* Honours the ``AXIOM_USE_V1_ARCHIVE_FOR_FEATURES`` kill switch
  (default ON), mirroring ``AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION``.
* Preflights ``to_regclass(...) IS NOT NULL`` so missing migrations
  return ``None`` / ``[]`` instead of raising ``UndefinedTable``.
* Returns the empty / ``None`` sentinel on any DB exception — these
  are advisory enrichment paths, never on the live trade hot path.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from .. import db


log = logging.getLogger("axiom.v1_archive.features")


_FLAG_ENV = "AXIOM_USE_V1_ARCHIVE_FOR_FEATURES"


def use_v1_features() -> bool:
    """Return True when the V1-archive feature fallbacks are enabled.

    Mirrors the calibrator kill switch so an operator who disables the
    archive globally takes both code paths offline at once.
    """
    return os.getenv(_FLAG_ENV, "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


# ---------------------------------------------------------------------------
# Table-presence preflights (cached per-process; cheap to recheck).
# ---------------------------------------------------------------------------


async def _table_present(qualified: str) -> bool:
    try:
        return bool(
            await db.fetchval(
                "SELECT to_regclass($1) IS NOT NULL", qualified,
            )
        )
    except Exception as e:
        log.debug("v1_archive preflight %s failed: %s", qualified, e)
        return False


# ---------------------------------------------------------------------------
# Weather — map a stadium lat/lon to the nearest Kalshi city_code, then
# read the closest-in-time observation from v1_archive.weather_observations.
# ---------------------------------------------------------------------------


def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_mi = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r_mi * math.asin(math.sqrt(a))


def nearest_city_code(lat: float, lon: float, *, max_mi: float = 250.0) -> str | None:
    """Pick the closest Kalshi city_code for ``(lat, lon)``.

    Returns ``None`` if no city sits within ``max_mi`` — far-away
    stadiums (e.g. Green Bay relative to the 7 default cities) get
    skipped rather than attributed to a city whose weather has no
    correlation with the actual venue.
    """
    # Imported lazily so this module doesn't pull the live HTTP feed
    # at import time (matters for the calibrator unit tests).
    from ..feeds.weather import DEFAULT_CITIES

    best: tuple[float, str] | None = None
    for c in DEFAULT_CITIES:
        d = _haversine_mi(lat, lon, c.lat, c.lon)
        if best is None or d < best[0]:
            best = (d, c.code)
    if best is None or best[0] > max_mi:
        return None
    return best[1]


async def weather_for_coords(
    lat: float,
    lon: float,
    observed_at: datetime,
    *,
    window: timedelta = timedelta(hours=3),
    max_mi: float = 250.0,
) -> dict[str, Any] | None:
    """Pull the V1 weather reading closest to ``observed_at`` for the
    nearest tracked city.

    Returns ``None`` (not a dict) when the archive is disabled, the
    table is missing, no city is within ``max_mi``, no reading falls
    in the ``± window`` band, or anything else goes wrong.  Successful
    returns carry ``temperature_f`` plus provenance fields
    (``source = 'v1_archive'``, ``city_code``, ``observed_at``,
    ``distance_mi``) so callers can audit which row they consumed.
    """
    if not use_v1_features():
        return None
    if not await _table_present("v1_archive.weather_observations"):
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)

    city = nearest_city_code(lat, lon, max_mi=max_mi)
    if city is None:
        return None

    lo = observed_at - window
    hi = observed_at + window
    try:
        row = await db.fetchrow(
            """
            SELECT temperature_f, observed_at, source
              FROM v1_archive.weather_observations
             WHERE city_code = $1
               AND temperature_f IS NOT NULL
               AND observed_at BETWEEN $2 AND $3
             ORDER BY abs(extract(epoch FROM (observed_at - $4)))
             LIMIT 1
            """,
            city, lo, hi, observed_at,
        )
    except Exception as e:
        log.debug("v1_archive weather lookup failed for %s: %s", city, e)
        return None
    if row is None or row["temperature_f"] is None:
        return None

    distance_mi = _haversine_mi(
        lat, lon,
        *_city_coords(city),
    ) if _city_coords(city) else None
    return {
        "temperature_f": float(row["temperature_f"]),
        "source": "v1_archive",
        "v1_source": row["source"],
        "city_code": city,
        "observed_at": row["observed_at"],
        "distance_mi": distance_mi,
    }


def _city_coords(code: str) -> tuple[float, float] | None:
    from ..feeds.weather import DEFAULT_CITIES
    for c in DEFAULT_CITIES:
        if c.code == code:
            return (c.lat, c.lon)
    return None


# ---------------------------------------------------------------------------
# Line snapshots — latest line per (game_id, market_type) before a given
# timestamp.  Used as a fallback when ``public.line_snapshots`` has no
# coverage for a V1-era game.
# ---------------------------------------------------------------------------


async def latest_line_for_game(
    game_id: str,
    market_keys: Iterable[str],
    *,
    before: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the most recent V1 line snapshot for ``game_id`` whose
    ``market_type`` matches any of ``market_keys``.

    ``before`` (when provided) caps the search to snapshots written
    strictly before that timestamp — important for backtest replay so
    we never leak a line that wasn't visible at decision time.
    """
    if not use_v1_features():
        return None
    if not game_id:
        return None
    keys = [str(k) for k in market_keys if k]
    if not keys:
        return None
    if not await _table_present("v1_archive.line_snapshots"):
        return None

    try:
        if before is None:
            row = await db.fetchrow(
                """
                SELECT line_value, odds_value, book, market_type, "timestamp"
                  FROM v1_archive.line_snapshots
                 WHERE game_id = $1
                   AND market_type = ANY($2::text[])
                   AND line_value IS NOT NULL
                 ORDER BY "timestamp" DESC
                 LIMIT 1
                """,
                str(game_id), keys,
            )
        else:
            if before.tzinfo is None:
                before = before.replace(tzinfo=timezone.utc)
            row = await db.fetchrow(
                """
                SELECT line_value, odds_value, book, market_type, "timestamp"
                  FROM v1_archive.line_snapshots
                 WHERE game_id = $1
                   AND market_type = ANY($2::text[])
                   AND line_value IS NOT NULL
                   AND "timestamp" < $3
                 ORDER BY "timestamp" DESC
                 LIMIT 1
                """,
                str(game_id), keys, before,
            )
    except Exception as e:
        log.debug("v1_archive line lookup failed for %s: %s", game_id, e)
        return None
    if row is None or row["line_value"] is None:
        return None
    return {
        "line_value": float(row["line_value"]),
        "odds_value": float(row["odds_value"]) if row["odds_value"] is not None else None,
        "book": row["book"],
        "market_type": row["market_type"],
        "timestamp": row["timestamp"],
        "source": "v1_archive",
    }


async def closing_lines_for_games(
    game_ids: Iterable[str],
    market_keys: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Bulk variant of :func:`latest_line_for_game` for training jobs.

    Returns a ``{game_id: row}`` mapping, omitting games without a
    matching V1 snapshot.  Returns ``{}`` when the archive is disabled
    or the table is missing.
    """
    if not use_v1_features():
        return {}
    ids = [str(g) for g in game_ids if g]
    keys = [str(k) for k in market_keys if k]
    if not ids or not keys:
        return {}
    if not await _table_present("v1_archive.line_snapshots"):
        return {}
    try:
        rows = await db.fetch(
            """
            SELECT DISTINCT ON (game_id)
                   game_id, line_value, "timestamp"
              FROM v1_archive.line_snapshots
             WHERE game_id = ANY($1::text[])
               AND market_type = ANY($2::text[])
               AND line_value IS NOT NULL
             ORDER BY game_id, "timestamp" DESC
            """,
            ids, keys,
        )
    except Exception as e:
        log.debug("v1_archive closing-line bulk lookup failed: %s", e)
        return {}
    return {
        str(r["game_id"]): {
            "line_value": float(r["line_value"]),
            "timestamp": r["timestamp"],
            "source": "v1_archive",
        }
        for r in rows
    }


__all__ = [
    "use_v1_features",
    "nearest_city_code",
    "weather_for_coords",
    "latest_line_for_game",
    "closing_lines_for_games",
]
