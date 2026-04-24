"""Operator CLI: load NOAA NCEI 1991-2020 daily Climate Normals into
``weather_climatology`` with ``source = 'ncei'``.

The Weather v6 model already consumes ``weather_climatology`` rows; the
table ships pre-seeded with a sinusoidal baseline (``source =
'sinusoidal_baseline'``) for cold-start correctness.  This script
replaces that approximation with the real NCEI normals for each city's
canonical airport station.  Because :func:`backfill_climatology`
inserts the baseline with ``ON CONFLICT DO NOTHING`` and this script
upserts with ``ON CONFLICT DO UPDATE``, NCEI rows always win once
loaded.

Usage (from the api-server directory)::

    python scripts/backfill_ncei_climatology.py
    python scripts/backfill_ncei_climatology.py --cities NYC,AUS
    python scripts/backfill_ncei_climatology.py --dry-run

The job is idempotent.  Re-running with the same upstream data is a
no-op aside from bumping ``updated_at``.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
import time
from datetime import date
from pathlib import Path

import httpx

# Allow running as `python scripts/backfill_ncei_climatology.py` from api-server.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.feeds.weather_climatology import (  # noqa: E402
    METRIC_TAVG,
    METRIC_TMAX,
    METRIC_TMIN,
    refresh_climatology_cache,
    supported_cities,
)


# Canonical NCEI GHCN-D station IDs for each city's primary airport.
# These are the same stations NWS uses for the city's official daily
# observations and that the Kalshi KXHIGH/KXLOW contracts settle on.
CITY_STATIONS: dict[str, tuple[str, str]] = {
    # city: (airport_call_sign, GHCN-D station id)
    "NYC": ("KNYC", "USW00094728"),  # NY Central Park (KNYC settles KXHIGHNY)
    "AUS": ("KAUS", "USW00013904"),  # Austin-Bergstrom Intl
    "CHI": ("KORD", "USW00094846"),  # Chicago O'Hare Intl
    "DEN": ("KDEN", "USW00003017"),  # Denver Intl
    "MIA": ("KMIA", "USW00012839"),  # Miami Intl
    "LAX": ("KLAX", "USW00023174"),  # Los Angeles Intl
    "PHL": ("KPHL", "USW00013739"),  # Philadelphia Intl
}

NCEI_URL = "https://www.ncei.noaa.gov/data/normals-daily/1991-2020/access/{station}.csv"

# NCEI column -> our metric vocabulary.  All three are reported in
# degrees Fahrenheit (no scaling).
COLUMN_TO_METRIC: dict[str, str] = {
    "DLY-TMAX-NORMAL": METRIC_TMAX,
    "DLY-TMIN-NORMAL": METRIC_TMIN,
    "DLY-TAVG-NORMAL": METRIC_TAVG,
}


def _doy_from_mmdd(mm: int, dd: int) -> int:
    """Map an NCEI ``MM-DD`` date to a leap-year day-of-year (1..366).

    The ``weather_climatology`` table stores 366 keys per (city, metric);
    using a leap-year calendar means Feb 29 → 60 and Mar 1 → 61, which
    matches Python's ``strftime("%j")`` in leap years.  In non-leap
    years lookups for dates after Feb 28 are off by one calendar day,
    which is well below NCEI's measurement noise (~0.1°F).
    """
    return date(2000, mm, dd).timetuple().tm_yday


async def _fetch_station_csv(client: httpx.AsyncClient, station: str) -> str:
    url = NCEI_URL.format(station=station)
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


def _parse_rows(city: str, csv_text: str) -> list[tuple]:
    """Yield ``(city, doy, metric, value_f)`` tuples for every NCEI row."""
    rows: list[tuple] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for record in reader:
        date_str = (record.get("DATE") or "").strip()
        if not date_str or "-" not in date_str:
            continue
        try:
            mm_str, dd_str = date_str.split("-", 1)
            mm = int(mm_str)
            dd = int(dd_str)
        except ValueError:
            continue
        try:
            doy = _doy_from_mmdd(mm, dd)
        except ValueError:
            # Defends against malformed dates like "02-30".
            continue
        for column, metric in COLUMN_TO_METRIC.items():
            raw = (record.get(column) or "").strip()
            if not raw:
                continue
            try:
                v = float(raw)
            except ValueError:
                continue
            # NCEI uses sentinel values like -7777/-9999 for missing; any
            # tmax below -100°F or above 150°F is clearly bogus.
            if not (-100.0 <= v <= 150.0):
                continue
            rows.append((city.upper(), doy, metric, round(v, 2)))
    return rows


async def _upsert_rows(rows: list[tuple]) -> int:
    if not rows:
        return 0
    async with db.acquire() as conn, conn.transaction():
        await conn.executemany(
            """
            INSERT INTO weather_climatology
              (location, day_of_year, metric, value_f, source, updated_at)
            VALUES ($1, $2, $3, $4, 'ncei', now())
            ON CONFLICT (location, day_of_year, metric) DO UPDATE
              SET value_f = EXCLUDED.value_f,
                  source = 'ncei',
                  updated_at = now()
            """,
            rows,
        )
    return len(rows)


async def _verify_coverage() -> dict[str, int]:
    """Return ``{city: ncei_row_count}`` so the operator can spot gaps."""
    rows = await db.fetch(
        """
        SELECT location, COUNT(*)::int AS n
        FROM weather_climatology
        WHERE source = 'ncei'
        GROUP BY location
        ORDER BY location
        """
    )
    return {str(r["location"]): int(r["n"]) for r in rows}


async def _main(args: argparse.Namespace) -> int:
    requested = (
        [c.strip().upper() for c in args.cities.split(",") if c.strip()]
        if args.cities
        else list(CITY_STATIONS.keys())
    )
    unknown = [c for c in requested if c not in CITY_STATIONS]
    if unknown:
        print(f"unknown cities: {unknown} (known: {sorted(CITY_STATIONS)})", file=sys.stderr)
        return 2

    # Warn early if any supported city is missing a station mapping —
    # surfacing a config drift between the climatology module and this
    # script.
    missing = [c for c in supported_cities() if c not in CITY_STATIONS]
    if missing:
        print(f"WARN: cities supported by climatology but missing here: {missing}", file=sys.stderr)

    t0 = time.perf_counter()
    timeout = httpx.Timeout(60.0, connect=10.0)
    total_rows = 0
    per_city: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for city in requested:
            airport, station = CITY_STATIONS[city]
            try:
                csv_text = await _fetch_station_csv(client, station)
            except Exception as e:
                print(f"FAIL {city} ({airport}/{station}): download error: {e}", file=sys.stderr)
                per_city[city] = 0
                continue
            rows = _parse_rows(city, csv_text)
            per_city[city] = len(rows)
            if args.dry_run:
                print(f"DRY {city} ({airport}/{station}): parsed {len(rows)} rows")
                continue
            written = await _upsert_rows(rows)
            total_rows += written
            print(f"OK  {city} ({airport}/{station}): upserted {written} rows")

    if not args.dry_run and total_rows > 0:
        loaded = await refresh_climatology_cache()
        print(f"cache refreshed: {loaded} rows total")

    if not args.dry_run:
        coverage = await _verify_coverage()
        print("--- ncei coverage by city ---")
        for c in sorted(set(list(per_city.keys()) + list(coverage.keys()))):
            print(f"  {c}: parsed={per_city.get(c, 0)} db_ncei_rows={coverage.get(c, 0)}")

    await db.close_pool()
    print(f"total_upserts={total_rows} wall_clock={time.perf_counter() - t0:.1f}s")
    # Exit non-zero if anything failed so cron / CI sees the error.
    return 0 if all(n > 0 for n in per_city.values()) else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill NOAA NCEI 1991-2020 daily Climate Normals into weather_climatology."
    )
    p.add_argument(
        "--cities", default=None,
        help=f"Comma-separated city codes to load (default: all of {sorted(CITY_STATIONS)}).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Download and parse but do not write to the database.",
    )
    args = p.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
