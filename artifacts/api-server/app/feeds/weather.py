"""Weather pollers — Open-Meteo, NWS, AccuWeather minutecast, HRRR.

Cities polled match the standard Kalshi weather markets (KXHIGH/KXTEMP/
KXLOW).  Open-Meteo + NWS + HRRR are free and require no key;
AccuWeather minutecast requires a key and is skipped cleanly when
absent.

HRRR ingestion talks directly to NOAA's NOMADS HRRR product at
``https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/`` and
decodes the GRIB2 messages locally via eccodes — we no longer route
through Open-Meteo's HRRR mirror, so a third-party rate limit or
outage cannot blind us to the cycle NOAA just published.

Per cycle we:

1.  HEAD-then-GET the F00 ``.idx`` inventory files (newest cycle
    first) to discover the latest published run.  The inventory gives
    us the canonical run timestamp + the byte offsets for every GRIB2
    record in the file.
2.  Pull the inventories for a handful of forecast hours in the same
    cycle (F00, F03, F06, ..., F18) so we can locate the per-hour
    TMAX/TMIN GRIB messages too.
3.  For each city we issue HTTP Range GETs against the GRIB2 file for
    only the bytes covering TMP / TMAX / TMIN at 2 m above ground,
    decode them with eccodes (``codes_grib_find_nearest``) at the
    city's lat/lon, and aggregate to ``t2m_f`` (analysis), ``tmax_f``
    (max across hours) and ``tmin_f`` (min across hours).

Run-cycle provenance (``hrrr_cycle``, ``nomads_url``,
``nomads_inventory_count``) is persisted alongside the decoded values
inside the ``hrrr_hourly`` JSONB column so the operator can audit
which run produced the numbers the orchestrator acted on.
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
# Forecast hours we pull inventories for to find TMAX/TMIN messages.
# F00 is analysis (TMP only); the windowed TMAX/TMIN messages live in
# F01..F18.  Sampling every 3 hours keeps the per-cycle request count
# bounded (~6 inventory GETs per cycle, plus per-city Range GETs) while
# still covering the full daytime/nighttime extremes window.
_HRRR_FORECAST_HOURS: tuple[int, ...] = (0, 3, 6, 9, 12, 15, 18)


def _hrrr_grib2_url(date_dir: str, cycle: str, fhour: int) -> str:
    return (
        f"{_NOMADS_HRRR_BASE}/hrrr.{date_dir}/conus/"
        f"hrrr.t{cycle}z.wrfsfcf{fhour:02d}.grib2"
    )


def _hrrr_idx_url(date_dir: str, cycle: str, fhour: int) -> str:
    return _hrrr_grib2_url(date_dir, cycle, fhour) + ".idx"


async def _discover_latest_hrrr_run(client: httpx.AsyncClient) -> dict[str, Any] | None:
    """Find the most recent HRRR cycle that has F00 published on NOMADS.

    Returns a dict with ``run_time`` (UTC ISO), ``cycle`` ("HH"),
    ``date`` ("YYYYMMDD"), ``inventory_url`` and the parsed F00
    inventory (a list of {record, byte_start, byte_len, var, level,
    fhour}).  Returns ``None`` if no run in the last few hours can be
    located — the caller will then skip the cycle entirely (we have no
    fallback decoder once Open-Meteo is gone).
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for back in range(_NOMADS_RUN_WINDOW_HOURS):
        candidate = now - timedelta(hours=back)
        date_dir = candidate.strftime("%Y%m%d")
        cycle = candidate.strftime("%H")
        idx_url = _hrrr_idx_url(date_dir, cycle, 0)
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


async def _fetch_run_inventories(
    client: httpx.AsyncClient, run_meta: dict[str, Any]
) -> dict[int, list[dict[str, Any]]]:
    """Pull the .idx inventories for every forecast hour we need from
    the discovered cycle.  Returns ``{fhour: [records]}``; missing
    hours are simply absent from the dict."""
    inventories: dict[int, list[dict[str, Any]]] = {0: run_meta["inventory"]}
    for fh in _HRRR_FORECAST_HOURS:
        if fh == 0:
            continue
        url = _hrrr_idx_url(run_meta["date"], run_meta["cycle"], fh)
        try:
            r = await client.get(url)
        except httpx.HTTPError as e:
            log.debug("hrrr inventory F%02d fetch failed: %s", fh, e)
            continue
        _check_429(r, "nomads")
        if r.status_code >= 400:
            log.debug("hrrr inventory F%02d -> HTTP %s", fh, r.status_code)
            continue
        inventories[fh] = _parse_hrrr_inventory(r.text)
    return inventories


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


async def _fetch_grib2_range(
    client: httpx.AsyncClient,
    url: str,
    byte_start: int,
    byte_len: int | None,
) -> bytes | None:
    """HTTP Range GET against a NOMADS GRIB2 file.

    ``byte_len`` may be ``None`` for the trailing record in the file,
    in which case we request from ``byte_start`` to end-of-file.
    """
    if byte_len is not None and byte_len <= 0:
        return None
    end = f"{byte_start + byte_len - 1}" if byte_len else ""
    headers = {"Range": f"bytes={byte_start}-{end}"}
    try:
        r = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        log.debug("grib2 range fetch failed (%s @ %s): %s", url, byte_start, e)
        return None
    _check_429(r, "nomads")
    if r.status_code not in (200, 206):
        log.debug("grib2 range %s @ %s -> HTTP %s", url, byte_start, r.status_code)
        return None
    return r.content


def _decode_grib2_point_kelvin(
    grib_bytes: bytes, lat: float, lon: float
) -> float | None:
    """Decode a single GRIB2 message and return the value at the grid
    point nearest ``(lat, lon)``.  HRRR's TMP/TMAX/TMIN at 2 m above
    ground are encoded in Kelvin; the caller is responsible for the
    K -> F conversion.

    ``codes_grib_find_nearest`` accepts either ``[-180, 180]`` or
    ``[0, 360]`` longitude; we still normalise negative inputs to the
    ``[0, 360]`` form HRRR uses internally to avoid surprises with
    older eccodes builds.
    """
    if not grib_bytes:
        return None
    # Imported lazily so the module is importable in environments where
    # eccodes wasn't installed yet (e.g. fresh checkouts before pip).
    try:
        import eccodes  # type: ignore[import-not-found]
    except Exception as e:  # pragma: no cover - import-time failure
        log.warning("eccodes not available, cannot decode HRRR GRIB2: %s", e)
        return None

    norm_lon = lon if lon >= 0 else lon + 360.0
    gid = None
    try:
        gid = eccodes.codes_new_from_message(grib_bytes)
        nearest = eccodes.codes_grib_find_nearest(gid, lat, norm_lon)
    except Exception as e:
        log.debug("grib2 decode failed: %s", e)
        return None
    finally:
        if gid is not None:
            try:
                eccodes.codes_release(gid)
            except Exception:
                pass
    if not nearest:
        return None
    value = nearest[0].value
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _select_records(
    inventories: dict[int, list[dict[str, Any]]], var_prefix: str
) -> list[tuple[int, dict[str, Any]]]:
    """Return ``[(fhour, record), ...]`` for every record across all
    fetched inventories whose ``var`` matches and whose level is the
    2 m above ground level we care about."""
    out: list[tuple[int, dict[str, Any]]] = []
    for fh, records in inventories.items():
        for rec in records:
            if rec["var"] == var_prefix and rec["level"] == "2 m above ground":
                out.append((fh, rec))
    return out


async def _decode_hrrr_for_city(
    client: httpx.AsyncClient,
    city: City,
    run_meta: dict[str, Any],
    inventories: dict[int, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Issue Range GETs against the run's GRIB2 files and decode TMP /
    TMAX / TMIN at the city's grid point.  Returns ``None`` if we can
    not even produce a current T2M reading (a hard requirement)."""

    samples: list[dict[str, Any]] = []

    def _persist(fh: int, var: str, kelvin: float | None) -> float | None:
        if kelvin is None:
            return None
        f_value = (kelvin - 273.15) * 9 / 5 + 32
        samples.append({"fhour": fh, "var": var, "f": f_value})
        return f_value

    # T2M (analysis) — TMP at 2 m in F00.  Without this we cannot
    # write a row, so abort early.
    t2m_f: float | None = None
    for fh, rec in _select_records(inventories, "TMP"):
        url = _hrrr_grib2_url(run_meta["date"], run_meta["cycle"], fh)
        body = await _fetch_grib2_range(
            client, url, rec["byte_start"], rec.get("byte_len")
        )
        kelvin = _decode_grib2_point_kelvin(body or b"", city.lat, city.lon)
        f_value = _persist(fh, "TMP", kelvin)
        if fh == 0 and f_value is not None and t2m_f is None:
            t2m_f = f_value
    if t2m_f is None:
        log.warning("hrrr: no TMP value decoded for %s, skipping", city.code)
        return None

    tmax_values: list[float] = []
    for fh, rec in _select_records(inventories, "TMAX"):
        url = _hrrr_grib2_url(run_meta["date"], run_meta["cycle"], fh)
        body = await _fetch_grib2_range(
            client, url, rec["byte_start"], rec.get("byte_len")
        )
        f_value = _persist(fh, "TMAX", _decode_grib2_point_kelvin(
            body or b"", city.lat, city.lon
        ))
        if f_value is not None:
            tmax_values.append(f_value)

    tmin_values: list[float] = []
    for fh, rec in _select_records(inventories, "TMIN"):
        url = _hrrr_grib2_url(run_meta["date"], run_meta["cycle"], fh)
        body = await _fetch_grib2_range(
            client, url, rec["byte_start"], rec.get("byte_len")
        )
        f_value = _persist(fh, "TMIN", _decode_grib2_point_kelvin(
            body or b"", city.lat, city.lon
        ))
        if f_value is not None:
            tmin_values.append(f_value)

    # If TMAX/TMIN messages are missing (e.g. the cycle only published
    # F00 so far), degrade gracefully to the analysis temperature so
    # the orchestrator still gets a sensible row instead of a gap.
    tmax_f = max([t2m_f, *tmax_values])
    tmin_f = min([t2m_f, *tmin_values])

    return {
        "t2m_f": t2m_f,
        "tmax_f": tmax_f,
        "tmin_f": tmin_f,
        "samples": samples,
    }


async def _poll_hrrr() -> int:
    from datetime import date
    import json

    written = 0
    timeout = httpx.Timeout(30.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Step 1: discover the latest published cycle.  Without a run
        # we have no inventory and cannot decode anything — exit clean.
        run_meta = await _discover_latest_hrrr_run(client)
        if run_meta is None:
            log.info("hrrr: no recent NOMADS cycle found, skipping poll")
            return 0

        # Step 2: fetch the inventories for every forecast hour we
        # care about so we know the byte ranges for each city.
        inventories = await _fetch_run_inventories(client, run_meta)

        # Step 3: per city, range-fetch + decode the GRIB messages.
        for city in _selected_cities():
            decoded = await _decode_hrrr_for_city(client, city, run_meta, inventories)
            if decoded is None:
                continue

            hourly_payload: dict[str, Any] = {
                "model": "hrrr",
                "decoder": "nomads:grib2",
                "t2m_f": decoded["t2m_f"],
                "tmax_f": decoded["tmax_f"],
                "tmin_f": decoded["tmin_f"],
                "samples": decoded["samples"],
                "nomads": {
                    "cycle": run_meta["cycle"],
                    "date": run_meta["date"],
                    "inventory_url": run_meta["inventory_url"],
                    "inventory_count": len(run_meta["inventory"]),
                    "fhours": sorted(inventories.keys()),
                },
            }

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
                    float(decoded["tmax_f"]),
                    json.dumps(hourly_payload),
                    run_meta["run_time"],
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
