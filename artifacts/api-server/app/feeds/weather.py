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
                        # Daily QPF (today's accumulated precipitation in
                        # inches) for the KXRAIN predictor (task #279).
                        # ``timezone=auto`` aligns the daily bucket with
                        # the city's local civil day, which is what
                        # KXRAIN contracts settle on.
                        "daily": "precipitation_sum",
                        "timezone": "auto",
                        "forecast_days": 1,
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
                body = r.json() or {}
            except Exception:
                continue
            payload = body.get("current") or {}
            if not payload:
                continue
            precip_today_in = _first_daily_value(body.get("daily"), "precipitation_sum")

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
                           cloud_cover_pct, precip_1hr_in, precip_today_in,
                           wind_gust_mph, created_at)
                        VALUES ($1, 'open-meteo', NOW(), $2, $3, $4, $5, $6, $7,
                                $8, $9, $10, $11, NOW())
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
                        precip_today_in,
                        _f(payload.get("wind_gusts_10m")),
                    )
                    written += 2
            except Exception as e:
                log.warning("open-meteo persist for %s failed: %s", city.code, e)
    return written


def _first_daily_value(daily: Any, key: str) -> float | None:
    """Open-Meteo returns daily fields as parallel arrays:

        ``daily = {"time": ["2026-04-24"], "precipitation_sum": [0.42]}``

    Pull the first entry for ``key`` defensively — a missing or empty
    list collapses to ``None`` so the caller leaves the column NULL.
    """
    if not isinstance(daily, dict):
        return None
    arr = daily.get(key)
    if not isinstance(arr, list) or not arr:
        return None
    return _f(arr[0])


# ---------------------------------------------------------------------------
# NWS (free, no key but a User-Agent is required) — metar observations.
# ---------------------------------------------------------------------------


# In-process cache of NWS gridpoint URLs keyed by city code.  The
# per-city ``/points/{lat,lon}`` lookup is a constant for the lifetime
# of the process, so we memoise it instead of paying the round-trip on
# every poll.  ``None`` is a sentinel for "tried and failed" — we
# revisit on the next process start.
_NWS_GRIDPOINT_URLS: dict[str, str | None] = {}


async def _resolve_nws_gridpoint_url(
    client: httpx.AsyncClient, city: City,
) -> str | None:
    """Resolve the ``forecastGridData`` URL for ``city`` via NWS's
    ``/points/{lat,lon}`` endpoint, with an in-process cache so the
    discovery only fires once per city per process."""
    cached = _NWS_GRIDPOINT_URLS.get(city.code, ...)  # type: ignore[arg-type]
    if cached is not ...:  # type: ignore[comparison-overlap]
        return cached  # type: ignore[return-value]
    url = f"https://api.weather.gov/points/{city.lat},{city.lon}"
    try:
        r = await client.get(url)
    except httpx.HTTPError as e:
        log.debug("nws points fetch failed for %s: %s", city.code, e)
        _NWS_GRIDPOINT_URLS[city.code] = None
        return None
    if r.status_code >= 400:
        log.debug("nws points %s -> HTTP %s", city.code, r.status_code)
        _NWS_GRIDPOINT_URLS[city.code] = None
        return None
    try:
        props = (r.json() or {}).get("properties") or {}
    except Exception:
        _NWS_GRIDPOINT_URLS[city.code] = None
        return None
    grid_url = props.get("forecastGridData")
    grid_url = str(grid_url) if grid_url else None
    _NWS_GRIDPOINT_URLS[city.code] = grid_url
    return grid_url


def _sum_nws_qpf_today(values: Any, today_utc: Any) -> float | None:
    """Sum NWS gridpoint ``quantitativePrecipitation.values`` entries
    that overlap today's UTC date.

    Each entry has the shape::

        {"validTime": "2026-04-24T00:00:00+00:00/PT6H", "value": 12.7}

    where ``value`` is in millimetres.  The validTime is an ISO-8601
    interval (``<start>/<duration>``).  We attribute the entire
    accumulation to ``today`` whenever the interval starts on the same
    UTC date as ``today_utc`` — this is the convention NWS QPF tables
    use and avoids fractional bucket arithmetic that doesn't actually
    improve accuracy.

    Returns ``None`` if no usable entries are found, otherwise the sum
    converted to inches.
    """
    if not isinstance(values, list) or not values:
        return None
    from datetime import datetime
    total_mm = 0.0
    matched = 0
    for entry in values:
        if not isinstance(entry, dict):
            continue
        valid = entry.get("validTime")
        v = entry.get("value")
        if not isinstance(valid, str) or v is None:
            continue
        start = valid.split("/", 1)[0]
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            continue
        if start_dt.date() != today_utc:
            continue
        try:
            mm = float(v)
        except (TypeError, ValueError):
            continue
        if mm < 0:
            continue
        total_mm += mm
        matched += 1
    if matched == 0:
        return None
    return total_mm / 25.4


async def _fetch_nws_daily_qpf(
    client: httpx.AsyncClient, city: City,
) -> float | None:
    """Pull today's accumulated quantitative precipitation forecast
    (inches) from NWS's gridpoint endpoint.  Returns ``None`` whenever
    the lookup or the daily sum cannot be derived."""
    grid_url = await _resolve_nws_gridpoint_url(client, city)
    if not grid_url:
        return None
    try:
        r = await client.get(grid_url)
    except httpx.HTTPError as e:
        log.debug("nws gridpoint fetch failed for %s: %s", city.code, e)
        return None
    if r.status_code == 429:
        log.warning("nws gridpoint %s -> HTTP 429", city.code)
        return None
    if r.status_code >= 400:
        log.debug("nws gridpoint %s -> HTTP %s", city.code, r.status_code)
        return None
    try:
        props = (r.json() or {}).get("properties") or {}
    except Exception:
        return None
    qpf = props.get("quantitativePrecipitation") or {}
    from datetime import datetime, timezone as _tz
    today_utc = datetime.now(_tz.utc).date()
    return _sum_nws_qpf_today(qpf.get("values"), today_utc)


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

            # Pull today's QPF from the gridpoint endpoint and persist
            # it as a 'nws'-source row in weather_extended so the
            # KXRAIN payload (task #279) can surface it as
            # ``nws_precip_today_in``.  This is best-effort; a NULL
            # result still produces an extended row keyed by today's
            # observation timestamp so freshness windows still apply.
            precip_today_in = await _fetch_nws_daily_qpf(client, city)
            try:
                async with db.acquire() as conn, conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO weather_observations
                          (city_code, temperature_f, observed_at, source)
                        VALUES ($1, $2, NOW(), 'nws')
                        """,
                        city.code,
                        float(f_temp),
                    )
                    if precip_today_in is not None:
                        await conn.execute(
                            """
                            INSERT INTO weather_extended
                              (city_code, source, observed_at,
                               precip_today_in, created_at)
                            VALUES ($1, 'nws', NOW(), $2, NOW())
                            """,
                            city.code,
                            precip_today_in,
                        )
                        written += 1
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

    We keep the fields the v6 ensemble cares about (TMP at 2 m above
    ground, the daily TMAX/TMIN at 2 m above ground, and the surface
    accumulated precipitation APCP for the KXRAIN predictor — task
    #279), which keeps the persisted provenance blob tiny.
    """
    keep = (
        "TMP:2 m above ground",
        "TMAX:2 m above ground",
        "TMIN:2 m above ground",
        "APCP:surface",
    )
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
    if prev is not None and any(prev["full"].startswith(k) for k in keep):
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


def _decode_grib2_point_value(
    grib_bytes: bytes, lat: float, lon: float
) -> float | None:
    """Unit-agnostic GRIB2 point decoder.  Returns the raw value in
    whatever units the encoded message uses (Kelvin for TMP/TMAX/TMIN,
    kg/m^2 — equivalent to mm — for APCP).  Callers do their own unit
    conversion."""
    return _decode_grib2_point_kelvin(grib_bytes, lat, lon)


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
    inventories: dict[int, list[dict[str, Any]]],
    var_prefix: str,
    *,
    level: str = "2 m above ground",
) -> list[tuple[int, dict[str, Any]]]:
    """Return ``[(fhour, record), ...]`` for every record across all
    fetched inventories whose ``var`` matches and whose ``level``
    matches the requested HRRR level (defaults to 2 m above ground for
    temperature; APCP records live at ``surface``)."""
    out: list[tuple[int, dict[str, Any]]] = []
    for fh, records in inventories.items():
        for rec in records:
            if rec["var"] == var_prefix and rec["level"] == level:
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

    # APCP at the surface — accumulated precipitation since the cycle
    # start, in kg/m^2 (≡ mm).  HRRR publishes APCP only at forecast
    # hours ≥ 1, and the value at F=N is the running total over the
    # 0..N hour window.  The KXRAIN predictor (task #279) wants a
    # *daily* total, so we take the maximum across the forecast hours
    # we fetched (which is monotonically the largest accumulation
    # window the cycle exposes) and convert mm → inches.  A missing
    # APCP message degrades cleanly to ``None``: the precipitation
    # predictor's per-source preference list will then fall through to
    # whatever the other providers contributed.
    apcp_mm: float | None = None
    apcp_fhour: int | None = None
    for fh, rec in _select_records(inventories, "APCP", level="surface"):
        url = _hrrr_grib2_url(run_meta["date"], run_meta["cycle"], fh)
        body = await _fetch_grib2_range(
            client, url, rec["byte_start"], rec.get("byte_len")
        )
        mm = _decode_grib2_point_value(body or b"", city.lat, city.lon)
        if mm is None or mm < 0:
            continue
        samples.append({"fhour": fh, "var": "APCP", "mm": mm})
        if apcp_mm is None or fh > (apcp_fhour or -1):
            apcp_mm = mm
            apcp_fhour = fh
    precip_in: float | None = (apcp_mm / 25.4) if apcp_mm is not None else None

    return {
        "t2m_f": t2m_f,
        "tmax_f": tmax_f,
        "tmin_f": tmin_f,
        "precip_in": precip_in,
        "apcp_fhour": apcp_fhour,
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
            # Daily QPF (inches) accumulated by the cycle — surfaces as
            # ``hrrr_precip_in`` in the unified weather payload.  Only
            # written when the GRIB2 decoder produced an APCP value;
            # missing keys are tolerated by the payload reader.
            if decoded.get("precip_in") is not None:
                hourly_payload["precip_in"] = decoded["precip_in"]
                hourly_payload["precip_apcp_fhour"] = decoded.get("apcp_fhour")

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

    A second call to ``/v4/weather/forecast?timesteps=1d`` is issued
    per city so the KXRAIN predictor (task #279) gets a real *daily*
    QPF instead of the 1-hour rate proxy.  The daily call is
    best-effort — a failure leaves ``precip_today_in`` NULL and the
    realtime row still lands.
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

            precip_today_in = await _fetch_tomorrow_io_daily_precip(client, key, city)
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
                           cloud_cover_pct, precip_1hr_in, precip_today_in,
                           wind_gust_mph, created_at)
                        VALUES ($1, 'tomorrow.io', NOW(), $2, $3, $4, $5, $6,
                                $7, $8, $9, $10, $11, NOW())
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
                        precip_today_in,
                        _f(values.get("windGust")),
                    )
                    written += 2
            except Exception as e:
                log.warning("tomorrow.io persist for %s failed: %s", city.code, e)
    return written


async def _fetch_tomorrow_io_daily_precip(
    client: httpx.AsyncClient, key: str, city: City,
) -> float | None:
    """Pull today's accumulated precipitation (inches) from
    Tomorrow.io's daily forecast.  Returns ``None`` whenever the call
    fails or the response doesn't carry a usable accumulation field —
    the caller persists ``NULL`` so downstream consumers know the
    daily forecast is unavailable for this poll.
    """
    try:
        r = await client.get(
            "https://api.tomorrow.io/v4/weather/forecast",
            params={
                "location": f"{city.lat},{city.lon}",
                "timesteps": "1d",
                "units": "imperial",
                "apikey": key,
            },
        )
    except httpx.HTTPError as e:
        log.debug("tomorrow.io daily fetch failed for %s: %s", city.code, e)
        return None
    if r.status_code == 429:
        # Daily call is best-effort — surface the 429 in logs but do
        # not raise (the realtime row should still be persisted).
        log.warning("tomorrow.io daily %s -> HTTP 429", city.code)
        return None
    if r.status_code >= 400:
        log.debug(
            "tomorrow.io daily %s -> HTTP %s: %s",
            city.code, r.status_code, r.text[:120],
        )
        return None
    try:
        body = r.json() or {}
    except Exception:
        return None
    timelines = body.get("timelines") if isinstance(body, dict) else None
    daily = timelines.get("daily") if isinstance(timelines, dict) else None
    if not isinstance(daily, list) or not daily:
        return None
    first = daily[0]
    values = first.get("values") if isinstance(first, dict) else None
    if not isinstance(values, dict):
        return None
    # Tomorrow.io's daily payload exposes accumulation under a few
    # different keys depending on the API tier; prefer rainAccumulationSum
    # (cumulative inches in imperial mode), fall back to a couple of
    # well-known aliases, and finally degrade to averaging the intensity
    # across the day.
    for key_name in (
        "rainAccumulationSum",
        "precipitationAccumulationSum",
        "rainAccumulationAvg",
        "precipitationAccumulationAvg",
    ):
        v = _f(values.get(key_name))
        if v is not None and v >= 0:
            return v
    # Fallback: in/hr average × 24h gives a coarse daily total.
    intensity_avg = _f(values.get("precipitationIntensityAvg"))
    if intensity_avg is not None and intensity_avg >= 0:
        return intensity_avg * 24.0
    return None


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
