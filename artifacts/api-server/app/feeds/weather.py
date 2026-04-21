"""Weather pollers — Open-Meteo, NWS, AccuWeather minutecast, HRRR.

Cities polled match the standard Kalshi weather markets (KXHIGH/KXTEMP/
KXLOW).  Open-Meteo + NWS + HRRR (via Open-Meteo's HRRR model) are
free and require no key; AccuWeather minutecast requires a key and is
skipped cleanly when absent.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .. import db
from .registry import Feed, registry


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


async def _poll_hrrr() -> int:
    from datetime import date
    written = 0
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for city in _selected_cities():
            try:
                r = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": city.lat,
                        "longitude": city.lon,
                        "hourly": "temperature_2m",
                        "temperature_unit": "fahrenheit",
                        "forecast_days": 1,
                        "models": "gfs_hrrr",
                    },
                )
            except httpx.HTTPError as e:
                log.warning("hrrr fetch failed for %s: %s", city.code, e)
                continue
            if r.status_code >= 400:
                log.warning("hrrr %s -> HTTP %s", city.code, r.status_code)
                continue
            try:
                payload = r.json() or {}
                hours = payload.get("hourly") or {}
            except Exception:
                continue
            times = hours.get("time") or []
            temps = hours.get("temperature_2m") or []
            if not times or not temps:
                continue
            try:
                hi = max(t for t in temps if t is not None)
            except ValueError:
                continue
            hourly_payload: dict[str, Any] = {
                "model": "hrrr",
                "samples": [
                    {"t": t, "f": v} for t, v in zip(times, temps)
                    if v is not None
                ],
            }
            try:
                import json
                await db.fetchval(
                    """
                    INSERT INTO weather_hrrr_forecasts
                      (city_code, forecast_date, hrrr_high_f, hrrr_hourly,
                       hrrr_run_time, fetched_at)
                    VALUES ($1, $2, $3, $4::jsonb, NOW(), NOW())
                    ON CONFLICT (city_code, forecast_date) DO UPDATE SET
                      hrrr_high_f = EXCLUDED.hrrr_high_f,
                      hrrr_hourly = EXCLUDED.hrrr_hourly,
                      hrrr_run_time = EXCLUDED.hrrr_run_time,
                      fetched_at = EXCLUDED.fetched_at
                    RETURNING id
                    """,
                    city.code,
                    date.today(),
                    float(hi),
                    json.dumps(hourly_payload),
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
    async with httpx.AsyncClient(timeout=timeout) as client:
        for city in _selected_cities():
            # AccuWeather requires a location key per lat/lon — fetch once per call.
            try:
                geo = await client.get(
                    "https://dataservice.accuweather.com/locations/v1/cities/geoposition/search",
                    params={"apikey": key, "q": f"{city.lat},{city.lon}"},
                )
                if geo.status_code >= 400:
                    log.warning("accuweather geo %s -> %s", city.code, geo.status_code)
                    continue
                loc_key = (geo.json() or {}).get("Key")
                if not loc_key:
                    continue
                m = await client.get(
                    f"https://dataservice.accuweather.com/forecasts/v1/minute/1minute",
                    params={"apikey": key, "q": f"{city.lat},{city.lon}"},
                )
                if m.status_code >= 400:
                    log.warning("accuweather minute %s -> %s", city.code, m.status_code)
                    continue
                payload = m.json() or {}
            except httpx.HTTPError as e:
                log.warning("accuweather %s failed: %s", city.code, e)
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
