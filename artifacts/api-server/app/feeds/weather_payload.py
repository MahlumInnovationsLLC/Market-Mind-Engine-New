"""Build a unified weather feature payload for one Kalshi city.

The Weather v6 ensemble model expects a flat dict keyed by source
prefix so every feature line in the model can reference the originating
provider unambiguously:

* ``om_*``   — Open-Meteo current observation + extended block
* ``nws_*``  — NWS METAR observation
* ``accu_*`` — AccuWeather minutecast precipitation summary
* ``hrrr_*`` — NOAA HRRR forecast (T2M now + daily TMAX/TMIN, plus
  the NOMADS run cycle provenance)
* ``clim_*`` — 30-year climatology baseline for today's day-of-year

This module owns the SELECTs against the tables the pollers populate
(``weather_observations``, ``weather_extended``, ``weather_minutecast``,
``weather_hrrr_forecasts``) and joins them with the cached climatology
lookups.  All reads are best-effort — a missing source becomes an
absent key in the returned dict (callers detect with ``in`` rather
than tolerate spurious zeros).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Mapping

from .. import db
from . import weather_climatology as clim


log = logging.getLogger("axiom.feeds.weather_payload")


# Limit how stale each source can be before we drop it from the
# payload.  These match the cadences the pollers run on, with a small
# safety multiplier so a delayed poll doesn't immediately blank a
# source.  All values are seconds.
_FRESHNESS_S: dict[str, float] = {
    "open-meteo": 30 * 60.0,    # 5-min cadence -> 30 min stale window
    "nws":        30 * 60.0,    # 5-min cadence
    "tomorrow.io": 30 * 60.0,   # 5-min cadence -> 30 min stale window
    "accuweather": 15 * 60.0,   # 1-min cadence -> 15 min stale window
    "hrrr":       12 * 60 * 60.0,  # 6h cadence -> 12 h stale window
}


def _is_fresh(observed_at: Any, source: str, *, now: datetime | None = None) -> bool:
    if observed_at is None:
        return False
    if not isinstance(observed_at, datetime):
        return False
    cutoff = (now or datetime.now(timezone.utc)) - _td(_FRESHNESS_S.get(source, 0))
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return observed_at >= cutoff


def _td(seconds: float):
    from datetime import timedelta
    return timedelta(seconds=float(seconds))


async def build_freshness_summary(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a per-city × per-source freshness snapshot for the
    operator Weather page.

    Unlike :func:`build_weather_payload`, this *does not* drop stale
    sources — it surfaces each source's latest observation timestamp
    plus a ``status`` ("fresh" / "stale" / "missing") so operators can
    see at a glance which feeds are dragging behind.  For HRRR it also
    surfaces the NOMADS run cycle (e.g. ``"12Z"``) and run timestamp so
    operators can confirm we're trading off the most recent published
    HRRR cycle.
    """
    # Local import — avoids a circular at module load (weather.py
    # imports from .scheduler which imports from .registry; the
    # payload module is loaded earlier in the boot order).
    from .weather import _selected_cities

    now = now or datetime.now(timezone.utc)

    cities_out: list[dict[str, Any]] = []
    for city in _selected_cities():
        sources: dict[str, Any] = {}
        for src in ("open-meteo", "nws", "tomorrow.io"):
            sources[src] = await _source_freshness_observation(city.code, src, now)
        sources["accuweather"] = await _source_freshness_minutecast(city.code, now)
        sources["hrrr"] = await _source_freshness_hrrr(city.code, now)
        cities_out.append({
            "code": city.code,
            "name": city.name,
            "sources": sources,
        })

    return {
        "as_of": now.isoformat(timespec="seconds"),
        "freshness_windows_s": {k: int(v) for k, v in _FRESHNESS_S.items()},
        "cities": cities_out,
    }


def _classify(observed_at: Any, source: str, now: datetime) -> str:
    if observed_at is None:
        return "missing"
    return "fresh" if _is_fresh(observed_at, source, now=now) else "stale"


async def _source_freshness_observation(
    city_code: str, source: str, now: datetime,
) -> dict[str, Any]:
    try:
        row = await db.fetchrow(
            """
            SELECT observed_at
            FROM weather_observations
            WHERE city_code = $1 AND source = $2
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            city_code, source,
        )
    except Exception as e:
        log.debug("freshness obs fetch failed (%s/%s): %s", city_code, source, e)
        row = None
    observed_at = row["observed_at"] if row else None
    return {
        "observed_at": _iso(observed_at),
        "status": _classify(observed_at, source, now),
        "max_age_s": int(_FRESHNESS_S.get(source, 0)),
    }


async def _source_freshness_minutecast(
    city_code: str, now: datetime,
) -> dict[str, Any]:
    try:
        row = await db.fetchrow(
            """
            SELECT fetched_at
            FROM weather_minutecast
            WHERE city_code = $1
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            city_code,
        )
    except Exception as e:
        log.debug("freshness accu fetch failed (%s): %s", city_code, e)
        row = None
    observed_at = row["fetched_at"] if row else None
    return {
        "observed_at": _iso(observed_at),
        "status": _classify(observed_at, "accuweather", now),
        "max_age_s": int(_FRESHNESS_S.get("accuweather", 0)),
    }


async def _source_freshness_hrrr(
    city_code: str, now: datetime,
) -> dict[str, Any]:
    today = now.date()
    try:
        row = await db.fetchrow(
            """
            SELECT fetched_at, hrrr_run_time, hrrr_hourly
            FROM weather_hrrr_forecasts
            WHERE city_code = $1 AND forecast_date = $2
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            city_code, today,
        )
    except Exception as e:
        log.debug("freshness hrrr fetch failed (%s): %s", city_code, e)
        row = None
    fetched_at = row["fetched_at"] if row else None
    out: dict[str, Any] = {
        "observed_at": _iso(fetched_at),
        "status": _classify(fetched_at, "hrrr", now),
        "max_age_s": int(_FRESHNESS_S.get("hrrr", 0)),
        "run_cycle": None,
        "run_time": None,
    }
    if row is not None:
        if row["hrrr_run_time"] is not None:
            out["run_time"] = _iso(row["hrrr_run_time"])
        body = _decode_jsonb(row["hrrr_hourly"])
        if isinstance(body, Mapping):
            nomads = body.get("nomads")
            if isinstance(nomads, Mapping):
                cycle = nomads.get("cycle")
                if cycle is not None:
                    # Normalise to the "12Z" badge format operators expect.
                    s = str(cycle).strip().upper()
                    if s.isdigit():
                        s = f"{int(s):02d}Z"
                    elif not s.endswith("Z"):
                        s = f"{s}Z"
                    out["run_cycle"] = s
    return out


async def build_weather_payload(
    city_code: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the Weather v6 payload for ``city_code``.

    Returns a dict with the union of source keys present (callers must
    not assume any particular key exists — sources can degrade
    independently).  ``meta.city_code`` and ``meta.day_of_year`` are
    always set so downstream features can key consistently.
    """
    now = now or datetime.now(timezone.utc)
    city = city_code.upper()
    today = (now.date() if now else date.today())
    doy = int(today.strftime("%j"))

    payload: dict[str, Any] = {
        "meta": {
            "city_code": city,
            "day_of_year": doy,
            "as_of": now.isoformat(),
        },
    }

    # Open-Meteo current observation + extended details ----------------------
    try:
        obs_om = await db.fetchrow(
            """
            SELECT temperature_f, observed_at
            FROM weather_observations
            WHERE city_code = $1 AND source = 'open-meteo'
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            city,
        )
    except Exception as e:
        log.debug("payload open-meteo fetch failed for %s: %s", city, e)
        obs_om = None
    if obs_om is not None and _is_fresh(obs_om["observed_at"], "open-meteo", now=now):
        payload["om_temperature_f"] = _f(obs_om["temperature_f"])
        payload["om_observed_at"] = _iso(obs_om["observed_at"])
        try:
            ext = await db.fetchrow(
                """
                SELECT wind_mph, humidity_pct, uv_index, dewpoint_f,
                       pressure_in, visibility_mi, cloud_cover_pct,
                       precip_1hr_in, precip_today_in, wind_gust_mph,
                       observed_at
                FROM weather_extended
                WHERE city_code = $1 AND source = 'open-meteo'
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                city,
            )
        except Exception as e:
            log.debug("payload open-meteo extended fetch failed for %s: %s", city, e)
            ext = None
        if ext is not None:
            for col in (
                "wind_mph", "humidity_pct", "uv_index", "dewpoint_f",
                "pressure_in", "visibility_mi", "cloud_cover_pct",
                "precip_1hr_in", "precip_today_in", "wind_gust_mph",
            ):
                # ``ext.get`` keeps the SELECT and the test fixtures
                # decoupled — a column that wasn't selected (legacy
                # schemas) or wasn't included in a test mock simply
                # surfaces as None and is dropped from the payload.
                v = _f(_record_get(ext, col))
                if v is not None:
                    payload[f"om_{col}"] = v

    # NWS METAR observation --------------------------------------------------
    try:
        obs_nws = await db.fetchrow(
            """
            SELECT temperature_f, observed_at
            FROM weather_observations
            WHERE city_code = $1 AND source = 'nws'
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            city,
        )
    except Exception as e:
        log.debug("payload nws fetch failed for %s: %s", city, e)
        obs_nws = None
    if obs_nws is not None and _is_fresh(obs_nws["observed_at"], "nws", now=now):
        payload["nws_temperature_f"] = _f(obs_nws["temperature_f"])
        payload["nws_observed_at"] = _iso(obs_nws["observed_at"])

    # NWS daily QPF (gridpoint-derived) --------------------------------------
    # The QPF poller writes ``precip_today_in`` into ``weather_extended``
    # under the ``'nws'`` source tag.  We surface it independently of
    # the METAR observation so a stale METAR doesn't blank the QPF (the
    # QPF freshness window is also wider — gridpoint forecasts are
    # republished a few times a day, not every five minutes).
    try:
        ext_nws = await db.fetchrow(
            """
            SELECT precip_today_in, observed_at
            FROM weather_extended
            WHERE city_code = $1 AND source = 'nws'
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            city,
        )
    except Exception as e:
        log.debug("payload nws extended fetch failed for %s: %s", city, e)
        ext_nws = None
    if ext_nws is not None and _is_fresh(ext_nws["observed_at"], "nws", now=now):
        v = _f(_record_get(ext_nws, "precip_today_in"))
        if v is not None:
            payload["nws_precip_today_in"] = v

    # Tomorrow.io realtime observation --------------------------------------
    # Surfaces the third independent point-forecast required by the GRU+EMOS
    # ensemble (task #246) — the poller writes into weather_observations
    # under the 'tomorrow.io' source tag.
    try:
        obs_tio = await db.fetchrow(
            """
            SELECT temperature_f, observed_at
            FROM weather_observations
            WHERE city_code = $1 AND source = 'tomorrow.io'
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            city,
        )
    except Exception as e:
        log.debug("payload tomorrow.io fetch failed for %s: %s", city, e)
        obs_tio = None
    if obs_tio is not None and _is_fresh(
        obs_tio["observed_at"], "tomorrow.io", now=now
    ):
        payload["tio_temperature_f"] = _f(obs_tio["temperature_f"])
        payload["tio_observed_at"] = _iso(obs_tio["observed_at"])
        try:
            ext_tio = await db.fetchrow(
                """
                SELECT wind_mph, humidity_pct, uv_index, dewpoint_f,
                       pressure_in, visibility_mi, cloud_cover_pct,
                       precip_1hr_in, precip_today_in, wind_gust_mph,
                       observed_at
                FROM weather_extended
                WHERE city_code = $1 AND source = 'tomorrow.io'
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                city,
            )
        except Exception as e:
            log.debug("payload tomorrow.io extended fetch failed for %s: %s", city, e)
            ext_tio = None
        if ext_tio is not None:
            for col in (
                "wind_mph", "humidity_pct", "uv_index", "dewpoint_f",
                "pressure_in", "visibility_mi", "cloud_cover_pct",
                "precip_1hr_in", "precip_today_in", "wind_gust_mph",
            ):
                v = _f(_record_get(ext_tio, col))
                if v is not None:
                    payload[f"tio_{col}"] = v

    # AccuWeather minutecast -------------------------------------------------
    try:
        mc = await db.fetchrow(
            """
            SELECT precip_summary, precip_type, precip_intensity,
                   minutes_to_precip, fetched_at
            FROM weather_minutecast
            WHERE city_code = $1
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            city,
        )
    except Exception as e:
        log.debug("payload accuweather fetch failed for %s: %s", city, e)
        mc = None
    if mc is not None and _is_fresh(mc["fetched_at"], "accuweather", now=now):
        payload["accu_summary"] = mc["precip_summary"]
        payload["accu_precip_type"] = mc["precip_type"]
        payload["accu_precip_intensity"] = mc["precip_intensity"]
        if mc["minutes_to_precip"] is not None:
            payload["accu_minutes_to_precip"] = int(mc["minutes_to_precip"])
        payload["accu_fetched_at"] = _iso(mc["fetched_at"])

    # HRRR forecast ----------------------------------------------------------
    try:
        hrrr = await db.fetchrow(
            """
            SELECT hrrr_high_f, hrrr_hourly, hrrr_run_time, fetched_at
            FROM weather_hrrr_forecasts
            WHERE city_code = $1 AND forecast_date = $2
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            city,
            today,
        )
    except Exception as e:
        log.debug("payload hrrr fetch failed for %s: %s", city, e)
        hrrr = None
    if hrrr is not None and _is_fresh(hrrr["fetched_at"], "hrrr", now=now):
        payload["hrrr_tmax_f"] = _f(hrrr["hrrr_high_f"])
        body = _decode_jsonb(hrrr["hrrr_hourly"])
        if isinstance(body, Mapping):
            t2m = body.get("t2m_f")
            tmin = body.get("tmin_f")
            if t2m is not None:
                payload["hrrr_t2m_f"] = _f(t2m)
            if tmin is not None:
                payload["hrrr_tmin_f"] = _f(tmin)
            # Daily QPF accumulated by the HRRR cycle, in inches.
            # Persisted by the GRIB2 ingester as ``precip_in`` inside
            # the same JSONB blob; the precipitation predictor's
            # ensemble extractor consumes it as ``hrrr_precip_in``.
            precip = body.get("precip_in")
            if precip is not None:
                p = _f(precip)
                if p is not None and p >= 0:
                    payload["hrrr_precip_in"] = p
            # Surface the HRRR hourly trace so the GRU sees a real
            # multi-step temperature sequence (task #246).  The poller
            # writes `samples = [{"t": ts, "f": temp_f}, ...]` into
            # the JSONB column; keep the same shape so the predictor's
            # `_build_sequence` consumer can iterate it directly.
            samples = body.get("samples")
            if isinstance(samples, list):
                hourly: list[dict[str, Any]] = []
                for s in samples:
                    if not isinstance(s, Mapping):
                        continue
                    f_val = _f(s.get("f"))
                    if f_val is None:
                        continue
                    entry: dict[str, Any] = {"f": f_val}
                    if s.get("t") is not None:
                        entry["t"] = s.get("t")
                    hourly.append(entry)
                if hourly:
                    payload["hrrr_hourly_samples"] = hourly
            nomads = body.get("nomads")
            if isinstance(nomads, Mapping):
                payload["hrrr_run_cycle"] = nomads.get("cycle")
                payload["hrrr_inventory_count"] = nomads.get("inventory_count")
        if hrrr["hrrr_run_time"] is not None:
            payload["hrrr_run_time"] = _iso(hrrr["hrrr_run_time"])
        payload["hrrr_fetched_at"] = _iso(hrrr["fetched_at"])

    # Climatology baseline ---------------------------------------------------
    for metric, key in (
        (clim.METRIC_TMAX, "clim_tmax_f"),
        (clim.METRIC_TMIN, "clim_tmin_f"),
        (clim.METRIC_TAVG, "clim_tavg_f"),
    ):
        v = await clim.get_climatology(city, doy, metric)
        if v is not None:
            payload[key] = float(v)

    return payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _record_get(row: Any, key: str) -> Any:
    """Defensive ``row[key]``-style lookup that tolerates both
    ``asyncpg.Record`` and plain ``dict`` rows (the latter is what
    test fixtures inject) and returns ``None`` for absent keys
    instead of raising ``KeyError``.

    This keeps the SELECT column list and any test fixtures decoupled:
    a column added to the schema after a fixture was written, or
    selected here but absent from a deliberately-thin mock, simply
    surfaces as ``None`` and the payload omits the key.
    """
    if row is None:
        return None
    getter = getattr(row, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except (TypeError, KeyError):
            pass
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec="seconds")
    return str(dt)


def _decode_jsonb(blob: Any) -> Any:
    if blob is None:
        return None
    if isinstance(blob, (dict, list)):
        return blob
    if isinstance(blob, str):
        import json
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            return None
    return None
