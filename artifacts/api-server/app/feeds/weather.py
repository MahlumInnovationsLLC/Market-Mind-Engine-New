"""Weather pollers — Open-Meteo, NWS, AccuWeather minutecast, HRRR.

Cities polled match the standard Kalshi weather markets (KXHIGH/KXTEMP/
KXLOW).  Open-Meteo + NWS + HRRR are free and require no key;
AccuWeather minutecast requires a key and is skipped cleanly when
absent.

HRRR ingestion is a two-step pull:

1.  We hit NOAA's NOMADS HRRR product at
    ``https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/`` to
    discover the most recent run cycle (``hrrr.tHHz``) by HEAD-ing the
    inventory ``.idx`` files for each candidate forecast hour.  This
    gives us the canonical run timestamp + the GRIB2 byte offsets we
    would need to decode T2M/TMAX/TMIN ourselves.
2.  We then fetch the *decoded* point values for each Kalshi city via
    Open-Meteo's HRRR endpoint, which proxies the same NOAA HRRR cycle
    and returns JSON we can read without bundling a GRIB2 decoder.

The provenance from step 1 (``hrrr_cycle``, ``nomads_url``,
``nomads_inventory_count``) is persisted alongside the values from
step 2 inside the ``hrrr_hourly`` JSONB column so the operator can
audit whether the values we acted on came from the latest published
HRRR run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .. import db
from .registry import Feed, registry
from .scheduler import RateLimited


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        try:
            dt = parsedate_to_datetime(raw)
            return max(1.0, (dt - datetime.now(tz=timezone.utc)).total_seconds())
        except Exception:
            return None


def _check_429(resp: httpx.Response, provider: str) -> None:
    """Raise RateLimited so the scheduler applies a provider-specific backoff."""
    if resp.status_code == 429:
        raise RateLimited(provider, retry_after=_retry_after_seconds(resp) or 60.0)


log = logging.getLogger("axiom.feeds.weather")


@dataclass(frozen=True)
class City:
    code: str          # Kalshi city code
    name: str          # human-readable
    lat: float
    lon: float
    nws_station: str   # METAR station for NWS observations
    accuweather_key: str | None = None  # populated lazily; minutecast needs the location key


# Standard Kalshi weather markets — operator can override the comma-separated list.
DEFAULT_CITIES: tuple[City, ...] = (
    City("NYC", "New York", 40.7128, -74.0060, "KNYC"),
    City("AUS", "Austin",   30.2672, -97.7431, "KAUS"),
    City("CHI", "Chicago",  41.8781, -87.6298, "KORD"),
    City("DEN", "Denver",   39.7392, -104.9903, "KDEN"),
    City("MIA", "Miami",    25.7617, -80.1918, "KMIA"),
    City("LAX", "Los Angeles", 34.0522, -118.2437, "KLAX"),
    City("PHL", "Philadelphia", 39.9526, -75.1652, "KPHL"),
)


def _selected_cities() -> tuple[City, ...]:
    raw = os.environ.get("AXIOM_WEATHER_CITIES")
    if not raw:
        return DEFAULT_CITIES
    wanted = {c.strip().upper() for c in raw.split(",") if c.strip()}
    selected = tuple(c for c in DEFAULT_CITIES if c.code in wanted)
    return selected or DEFAULT_CITIES


# ---------------------------------------------------------------------------
# Open-Meteo (free, no key) — current observations + extended details.
# ---------------------------------------------------------------------------


async def _poll_open_meteo() -> int:
    written = 0
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for city in _selected_cities():
            try:
                r = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": city.lat,
                        "longitude": city.lon,
                        "current": (
                            "temperature_2m,relative_humidity_2m,dew_point_2m,"
                            "wind_speed_10m,wind_gusts_10m,pressure_msl,"
                            "cloud_cover,visibility,precipitation,uv_index"
                        ),
                        "temperature_unit": "fahrenheit",
                        "wind_speed_unit": "mph",
                        "precipitation_unit": "inch",
                    },
                )
            except httpx.HTTPError as e:
                log.warning("open-meteo fetch failed for %s: %s", city.code, e)
                continue
            _check_429(r, "open-meteo")
            if r.status_code >= 400:
                log.warning("open-meteo %s -> HTTP %s", city.code, r.status_code)
                continue
            try:
                payload = r.json().get("current") or {}
            except Exception:
                continue
            if not payload:
                continue

            t = payload.get("temperature_2m")
            try:
                async with db.acquire() as conn, conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO weather_observations
                          (city_code, temperature_f, observed_at, source)
                        VALUES ($1, $2, NOW(), 'open-meteo')
                        """,
                        city.code,
                        float(t) if t is not None else None,
                    )
                    await conn.execute(
                        """
                        INSERT INTO weather_extended
                          (city_code, source, observed_at, wind_mph, humidity_pct,
                           uv_index, dewpoint_f, pressure_in, visibility_mi,
                           cloud_cover_pct, precip_1hr_in, wind_gust_mph,
                           created_at)
                        VALUES ($1, 'open-meteo', NOW(), $2, $3, $4, $5, $6, $7,
                                $8, $9, $10, NOW())
                        """,
                        city.code,
                        _f(payload.get("wind_speed_10m")),
                        _f(payload.get("relative_humidity_2m")),
                        _f(payload.get("uv_index")),
                        _f(payload.get("dew_point_2m")),
                        _hpa_to_inhg(payload.get("pressure_msl")),
                        _m_to_mi(payload.get("visibility")),
                        _f(payload.get("cloud_cover")),
                        _f(payload.get("precipitation")),
                        _f(payload.get("wind_gusts_10m")),
                    )
                    written += 2
            except Exception as e:
                log.warning("open-meteo persist for %s failed: %s", city.code, e)
    return written


# ---------------------------------------------------------------------------
# NWS (free, no key but a User-Agent is required) — metar observations.
# ---------------------------------------------------------------------------


async def _poll_nws() -> int:
    headers = {
        "User-Agent": os.environ.get(
            "AXIOM_NWS_UA",
            "axiom-predict (operator@axiom.example)",
        ),
        "Accept": "application/geo+json",
    }
    written = 0
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for city in _selected_cities():
            try:
                r = await client.get(
                    f"https://api.weather.gov/stations/{city.nws_station}/observations/latest",
                )
            except httpx.HTTPError as e:
                log.warning("nws fetch failed for %s: %s", city.code, e)
                continue
            _check_429(r, "nws")
            if r.status_code >= 400:
                log.warning("nws %s -> HTTP %s", city.code, r.status_code)
                continue
            try:
                props = (r.json() or {}).get("properties") or {}
            except Exception:
                continue
            temp = (props.get("temperature") or {}).get("value")  # celsius
            if temp is None:
                continue
            f_temp = temp * 9 / 5 + 32
            try:
                await db.fetchval(
                    """
                    INSERT INTO weather_observations
                      (city_code, temperature_f, observed_at, source)
                    VALUES ($1, $2, NOW(), 'nws')
                    RETURNING id
                    """,
                    city.code,
                    float(f_temp),
                )
                written += 1
            except Exception as e:
                log.warning("nws persist for %s failed: %s", city.code, e)
    return written


# ---------------------------------------------------------------------------
# HRRR via Open-Meteo (free, no key) — hourly forecast for the next 24h.
# ---------------------------------------------------------------------------


_NOMADS_HRRR_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod"
# HRRR runs every hour (24 cycles/day).  Each cycle's 2D surface file
# lives at ``hrrr.YYYYMMDD/conus/hrrr.tHHz.wrfsfcfFF.grib2`` with a
# companion ``.idx`` text inventory.  We scan a small window of recent
# cycles (newest first) until we find one whose F00 inventory exists.
_NOMADS_RUN_WINDOW_HOURS = 6


async def _discover_latest_hrrr_run(client: httpx.AsyncClient) -> dict[str, Any] | None:
    """Find the most recent HRRR cycle that has F00 published on NOMADS.

    Returns a dict with ``run_time`` (UTC ISO), ``cycle`` ("HH"),
    ``date`` ("YYYYMMDD"), ``inventory_url`` and the parsed inventory
    (a list of {record, byte_start, byte_len, var, level, fhour}).
    Returns ``None`` if no run in the last few hours can be located —
    the caller should still proceed with Open-Meteo for the values, but
    will not record run-cycle provenance.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for back in range(_NOMADS_RUN_WINDOW_HOURS):
        candidate = now - timedelta(hours=back)
        date_dir = candidate.strftime("%Y%m%d")
        cycle = candidate.strftime("%H")
        idx_url = (
            f"{_NOMADS_HRRR_BASE}/hrrr.{date_dir}/conus/"
            f"hrrr.t{cycle}z.wrfsfcf00.grib2.idx"
        )
        try:
            r = await client.get(idx_url)
        except httpx.HTTPError as e:
            log.debug("nomads hrrr inventory fetch failed for %s: %s", idx_url, e)
            continue
        if r.status_code == 404:
            continue
        if r.status_code >= 400:
            log.debug("nomads hrrr inventory %s -> HTTP %s", idx_url, r.status_code)
            continue
        return {
            "run_time": candidate.isoformat(),
            "cycle": cycle,
            "date": date_dir,
            "inventory_url": idx_url,
            "inventory": _parse_hrrr_inventory(r.text),
        }
    return None


def _parse_hrrr_inventory(text: str) -> list[dict[str, Any]]:
    """Parse a NOMADS GRIB2 ``.idx`` file.

    Each line has the form::

        <record>:<byte_offset>:d=<YYYYMMDDHH>:<var>:<level>:<fhour>:

    We only keep the fields the v6 ensemble cares about (TMP at 2 m
    above ground and the daily TMAX/TMIN at 2 m above ground), which
    keeps the persisted provenance blob tiny.
    """
    keep = ("TMP:2 m above ground", "TMAX:2 m above ground", "TMIN:2 m above ground")
    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for raw in text.splitlines():
        parts = raw.split(":")
        if len(parts) < 6:
            continue
        try:
            rec = int(parts[0])
            byte_start = int(parts[1])
        except ValueError:
            continue
        var, level, fhour = parts[3], parts[4], parts[5]
        record = {
            "rec": rec,
            "byte_start": byte_start,
            "var": var,
            "level": level,
            "fhour": fhour,
        }
        # Compute byte length once we see the next record (offsets are
        # ascending; len = next.start - this.start).  For the trailing
        # record we leave byte_len unset.
        if prev is not None and any(prev["full"].startswith(k) for k in keep):
            prev["entry"]["byte_len"] = byte_start - prev["entry"]["byte_start"]
            out.append(prev["entry"])
        prev = {"full": f"{var}:{level}", "entry": record}
    if prev is not None and any(prev["full"].startswith(k) for k in ("TMP:2 m above ground", "TMAX:2 m above ground", "TMIN:2 m above ground")):
        out.append(prev["entry"])
    return out


async def _fetch_open_meteo_hrrr(
    client: httpx.AsyncClient, city: City
) -> dict[str, Any] | None:
    """Decode HRRR T2M/TMAX/TMIN at the city's lat/lon via Open-Meteo.

    Open-Meteo's ``models=gfs_hrrr`` endpoint serves the exact same
    NOAA HRRR cycles, decoded server-side.  Until we ship a native
    GRIB2 decoder this is the cheapest way to convert the bytes NOMADS
    publishes into Fahrenheit values for our point of interest.
    """
    try:
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": city.lat,
                "longitude": city.lon,
                "hourly": "temperature_2m",
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "forecast_days": 1,
                "timezone": "UTC",
                "models": "gfs_hrrr",
            },
        )
    except httpx.HTTPError as e:
        log.warning("hrrr decode (open-meteo) fetch failed for %s: %s", city.code, e)
        return None
    _check_429(r, "open-meteo:hrrr")
    if r.status_code >= 400:
        log.warning("hrrr decode (open-meteo) %s -> HTTP %s", city.code, r.status_code)
        return None
    try:
        return r.json() or {}
    except Exception:
        return None


def _summarise_hrrr(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull current T2M, daily TMAX/TMIN, and an hourly trace from the
    Open-Meteo HRRR JSON envelope.  Returns ``None`` if the response is
    too sparse to act on."""
    hours = payload.get("hourly") or {}
    times = hours.get("time") or []
    temps = hours.get("temperature_2m") or []
    if not times or not temps:
        return None
    daily = payload.get("daily") or {}
    tmax_arr = daily.get("temperature_2m_max") or []
    tmin_arr = daily.get("temperature_2m_min") or []

    # Derive TMAX/TMIN: prefer Open-Meteo's daily aggregates (which
    # match the NOAA HRRR daily MAX/MIN GRIB messages); fall back to
    # min/max over the hourly trace if the daily block is empty.
    valid_temps = [t for t in temps if t is not None]
    if tmax_arr and tmax_arr[0] is not None:
        tmax_f = float(tmax_arr[0])
    elif valid_temps:
        tmax_f = float(max(valid_temps))
    else:
        return None
    if tmin_arr and tmin_arr[0] is not None:
        tmin_f = float(tmin_arr[0])
    elif valid_temps:
        tmin_f = float(min(valid_temps))
    else:
        tmin_f = tmax_f

    t2m_now = next((float(t) for t in temps if t is not None), None)

    return {
        "t2m_f": t2m_now,
        "tmax_f": tmax_f,
        "tmin_f": tmin_f,
        "samples": [{"t": t, "f": v} for t, v in zip(times, temps) if v is not None],
    }


async def _poll_hrrr() -> int:
    from datetime import date
    import json

    written = 0
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Step 1: ask NOMADS for the latest published cycle.  Best-effort
        # — if NOMADS is unreachable we still write Open-Meteo values so
        # the orchestrator has fresh HRRR-derived numbers to act on.
        run_meta = await _discover_latest_hrrr_run(client)

        for city in _selected_cities():
            payload = await _fetch_open_meteo_hrrr(client, city)
            if payload is None:
                continue
            summary = _summarise_hrrr(payload)
            if summary is None:
                continue

            hourly_payload: dict[str, Any] = {
                "model": "hrrr",
                "decoder": "open-meteo:gfs_hrrr",
                "t2m_f": summary["t2m_f"],
                "tmax_f": summary["tmax_f"],
                "tmin_f": summary["tmin_f"],
                "samples": summary["samples"],
            }
            if run_meta is not None:
                hourly_payload["nomads"] = {
                    "cycle": run_meta["cycle"],
                    "date": run_meta["date"],
                    "inventory_url": run_meta["inventory_url"],
                    "inventory_count": len(run_meta["inventory"]),
                }
                hrrr_run_time = run_meta["run_time"]
            else:
                hrrr_run_time = None

            try:
                await db.fetchval(
                    """
                    INSERT INTO weather_hrrr_forecasts
                      (city_code, forecast_date, hrrr_high_f, hrrr_hourly,
                       hrrr_run_time, fetched_at)
                    VALUES ($1, $2, $3, $4::jsonb,
                            COALESCE($5::timestamptz, NOW()), NOW())
                    ON CONFLICT (city_code, forecast_date) DO UPDATE SET
                      hrrr_high_f = EXCLUDED.hrrr_high_f,
                      hrrr_hourly = EXCLUDED.hrrr_hourly,
                      hrrr_run_time = EXCLUDED.hrrr_run_time,
                      fetched_at = EXCLUDED.fetched_at
                    RETURNING id
                    """,
                    city.code,
                    date.today(),
                    float(summary["tmax_f"]),
                    json.dumps(hourly_payload),
                    hrrr_run_time,
                )
                written += 1
            except Exception as e:
                log.warning("hrrr persist for %s failed: %s", city.code, e)
    return written


# ---------------------------------------------------------------------------
# AccuWeather minutecast (key required).
# ---------------------------------------------------------------------------


async def _poll_accuweather_minutecast() -> int:
    key = os.environ.get("ACCUWEATHER_API_KEY")
    if not key:
        raise RuntimeError("ACCUWEATHER_API_KEY not set")
    written = 0
    timeout = httpx.Timeout(10.0, connect=5.0)
    # AccuWeather minutecast accepts geoposition (`q=lat,lon`) directly — no
    # need for a separate location-key lookup, which doubles the quota burn.
    async with httpx.AsyncClient(timeout=timeout) as client:
        for city in _selected_cities():
            try:
                m = await client.get(
                    "https://dataservice.accuweather.com/forecasts/v1/minute/1minute",
                    params={"apikey": key, "q": f"{city.lat},{city.lon}"},
                )
            except httpx.HTTPError as e:
                log.warning("accuweather %s failed: %s", city.code, e)
                continue
            _check_429(m, "accuweather")
            if m.status_code >= 400:
                log.warning("accuweather minute %s -> %s", city.code, m.status_code)
                continue
            try:
                payload = m.json() or {}
            except Exception:
                continue
            summary = payload.get("Summary") or {}
            try:
                await db.fetchval(
                    """
                    INSERT INTO weather_minutecast
                      (city_code, fetched_at, precip_summary, precip_type,
                       precip_intensity, minutes_to_precip)
                    VALUES ($1, NOW(), $2, $3, $4, $5)
                    RETURNING id
                    """,
                    city.code,
                    str(summary.get("LongPhrase") or summary.get("Phrase") or "")[:240] or None,
                    str(summary.get("Type") or "")[:60] or None,
                    str(summary.get("Phrase60") or "")[:60] or None,
                    int(summary.get("BeginMinute")) if summary.get("BeginMinute") is not None else None,
                )
                written += 1
            except Exception as e:
                log.warning("accuweather persist for %s failed: %s", city.code, e)
    return written


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _hpa_to_inhg(x: Any) -> float | None:
    v = _f(x)
    return v * 0.02953 if v is not None else None


def _m_to_mi(x: Any) -> float | None:
    v = _f(x)
    return v * 0.000621371 if v is not None else None


# ---------------------------------------------------------------------------
# Tomorrow.io (free tier, key required) — current observations.
# ---------------------------------------------------------------------------


async def _poll_tomorrow_io() -> int:
    """Poll Tomorrow.io's /weather/realtime endpoint for each tracked
    city and append the reading into ``weather_observations`` +
    ``weather_extended`` so the existing dashboards / predictors pick
    it up alongside the open-meteo + NWS rows.

    Tomorrow.io's free tier is 25 calls/hour/key per the published
    rate limits; we cadence at 30 minutes (configured in
    ``register_weather_feeds``) so the pollers across N cities still
    fit comfortably under quota for N ≤ 12.
    """
    key = os.environ.get("TOMORROW_IO_API_KEY")
    if not key:
        raise RuntimeError("TOMORROW_IO_API_KEY not set")
    written = 0
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for city in _selected_cities():
            try:
                r = await client.get(
                    "https://api.tomorrow.io/v4/weather/realtime",
                    params={
                        "location": f"{city.lat},{city.lon}",
                        "units": "imperial",
                        "apikey": key,
                    },
                )
            except httpx.HTTPError as e:
                log.warning("tomorrow.io fetch failed for %s: %s", city.code, e)
                continue
            _check_429(r, "tomorrow.io")
            if r.status_code >= 400:
                log.warning(
                    "tomorrow.io %s -> HTTP %s: %s",
                    city.code, r.status_code, r.text[:120],
                )
                continue
            try:
                payload = r.json() or {}
            except Exception:
                continue
            values = (
                ((payload.get("data") or {}).get("values")) or {}
            ) if isinstance(payload, dict) else {}
            if not values:
                continue
            t = values.get("temperature")
            try:
                async with db.acquire() as conn, conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO weather_observations
                          (city_code, temperature_f, observed_at, source)
                        VALUES ($1, $2, NOW(), 'tomorrow.io')
                        """,
                        city.code,
                        float(t) if t is not None else None,
                    )
                    await conn.execute(
                        """
                        INSERT INTO weather_extended
                          (city_code, source, observed_at, wind_mph, humidity_pct,
                           uv_index, dewpoint_f, pressure_in, visibility_mi,
                           cloud_cover_pct, precip_1hr_in, wind_gust_mph,
                           created_at)
                        VALUES ($1, 'tomorrow.io', NOW(), $2, $3, $4, $5, $6,
                                $7, $8, $9, $10, NOW())
                        """,
                        city.code,
                        _f(values.get("windSpeed")),
                        _f(values.get("humidity")),
                        _f(values.get("uvIndex")),
                        _f(values.get("dewPoint")),
                        # Tomorrow.io reports pressureSurfaceLevel in inHg in imperial mode
                        _f(values.get("pressureSurfaceLevel")),
                        _f(values.get("visibility")),
                        _f(values.get("cloudCover")),
                        _f(values.get("precipitationIntensity")),
                        _f(values.get("windGust")),
                    )
                    written += 2
            except Exception as e:
                log.warning("tomorrow.io persist for %s failed: %s", city.code, e)
    return written


def register_weather_feeds() -> None:
    if registry.status_of("weather_open_meteo"):
        return  # already registered

    registry.register(Feed(
        name="weather_open_meteo",
        cadence_seconds=300.0,
        poll=_poll_open_meteo,
        enabled=True,
    ))
    registry.register(Feed(
        name="weather_nws",
        cadence_seconds=300.0,
        poll=_poll_nws,
        enabled=True,
    ))
    registry.register(Feed(
        name="weather_hrrr",
        cadence_seconds=6 * 60 * 60.0,
        poll=_poll_hrrr,
        enabled=True,
    ))
    accu_key = os.environ.get("ACCUWEATHER_API_KEY")
    registry.register(Feed(
        name="weather_accuweather_minutecast",
        cadence_seconds=60.0,
        poll=_poll_accuweather_minutecast,
        enabled=bool(accu_key),
        skipped_reason=None if accu_key else "ACCUWEATHER_API_KEY not set",
    ))
    tio_key = os.environ.get("TOMORROW_IO_API_KEY")
    registry.register(Feed(
        name="weather_tomorrow_io",
        cadence_seconds=30 * 60.0,
        poll=_poll_tomorrow_io,
        enabled=bool(tio_key),
        skipped_reason=None if tio_key else "TOMORROW_IO_API_KEY not set",
    ))
