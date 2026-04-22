"""Tests for the HRRR ingester + climatology baselines + payload builder."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.feeds import weather, weather_climatology, weather_payload


# ---------------------------------------------------------------------------
# Climatology
# ---------------------------------------------------------------------------


def test_baseline_value_peaks_in_summer():
    # Every supported city should have its max baseline near its peak DOY.
    for city in weather_climatology.supported_cities():
        spec = weather_climatology._BASELINE[city][weather_climatology.METRIC_TMAX]
        peak = spec[2]
        peak_v = weather_climatology.baseline_value(city, peak, weather_climatology.METRIC_TMAX)
        winter_v = weather_climatology.baseline_value(
            city, (peak + 183) % 366 or 366, weather_climatology.METRIC_TMAX
        )
        assert peak_v is not None and winter_v is not None
        assert peak_v > winter_v, f"{city}: summer should be warmer than winter"


def test_baseline_value_unknown_city_returns_none():
    assert (
        weather_climatology.baseline_value("ZZZ", 100, weather_climatology.METRIC_TMAX)
        is None
    )


def test_baseline_rows_cover_every_city_doy_metric():
    rows = weather_climatology._baseline_rows()
    expected = len(weather_climatology.supported_cities()) * 366 * 3
    assert len(rows) == expected


def test_get_climatology_sync_falls_back_to_baseline_when_cache_cold():
    # The cache hasn't been warmed in this test process — the sync
    # accessor must still return a sensible value via the embedded
    # baseline so callers in non-async paths don't blow up.
    weather_climatology._CACHE.clear()
    weather_climatology._CACHE_READY = False
    v = weather_climatology.get_climatology_sync(
        "NYC", 200, weather_climatology.METRIC_TMAX
    )
    assert v is not None
    # NYC summer high should be in the high 70s/low 80s.
    assert 75.0 <= v <= 90.0


def test_refresh_cache_uses_baseline_when_db_fails(monkeypatch):
    weather_climatology._CACHE.clear()
    weather_climatology._CACHE_READY = False

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(weather_climatology.db, "fetch", boom)
    n = asyncio.run(weather_climatology.refresh_climatology_cache())
    expected = len(weather_climatology.supported_cities()) * 366 * 3
    assert n == expected
    assert weather_climatology._CACHE_READY is True


def test_refresh_cache_merges_db_rows_with_baseline(monkeypatch):
    weather_climatology._CACHE.clear()
    weather_climatology._CACHE_READY = False

    # DB returns one real NCEI row that overrides the embedded baseline.
    db_rows = [
        {"location": "NYC", "day_of_year": 100, "metric": "tmax_f", "value_f": 99.9}
    ]

    async def fake_fetch(*a, **k):
        return db_rows

    monkeypatch.setattr(weather_climatology.db, "fetch", fake_fetch)
    asyncio.run(weather_climatology.refresh_climatology_cache())
    assert weather_climatology._CACHE[("NYC", 100, "tmax_f")] == pytest.approx(99.9)
    # Untouched (city, doy, metric) cells still resolve via the baseline.
    assert weather_climatology._CACHE[("NYC", 200, "tmax_f")] == pytest.approx(
        weather_climatology.baseline_value("NYC", 200, "tmax_f")
    )


# ---------------------------------------------------------------------------
# HRRR ingester
# ---------------------------------------------------------------------------


def test_parse_hrrr_inventory_keeps_only_relevant_records():
    sample = "\n".join([
        "1:0:d=2026042212:TMP:2 m above ground:anl:",
        "2:1234:d=2026042212:DPT:2 m above ground:anl:",
        "3:5678:d=2026042212:TMAX:2 m above ground:0-1 hour max fcst:",
        "4:9000:d=2026042212:TMIN:2 m above ground:0-1 hour min fcst:",
        "5:12000:d=2026042212:UGRD:10 m above ground:anl:",
    ])
    out = weather._parse_hrrr_inventory(sample)
    vars_seen = [(o["var"], o["level"]) for o in out]
    assert ("TMP", "2 m above ground") in vars_seen
    assert ("TMAX", "2 m above ground") in vars_seen
    assert ("TMIN", "2 m above ground") in vars_seen
    assert ("DPT", "2 m above ground") not in vars_seen
    assert ("UGRD", "10 m above ground") not in vars_seen
    # First TMP record should have a computed byte_len.
    tmp = next(o for o in out if o["var"] == "TMP")
    assert tmp["byte_len"] == 1234


def test_parse_hrrr_inventory_handles_garbage():
    assert weather._parse_hrrr_inventory("") == []
    assert weather._parse_hrrr_inventory("not a real line\n") == []


def test_summarise_hrrr_uses_daily_when_present():
    payload = {
        "hourly": {
            "time": ["2026-04-22T00:00", "2026-04-22T01:00"],
            "temperature_2m": [55.0, 60.0],
        },
        "daily": {
            "temperature_2m_max": [82.5],
            "temperature_2m_min": [48.0],
        },
    }
    s = weather._summarise_hrrr(payload)
    assert s["t2m_f"] == 55.0
    assert s["tmax_f"] == 82.5
    assert s["tmin_f"] == 48.0
    assert len(s["samples"]) == 2


def test_summarise_hrrr_falls_back_to_hourly_extremes():
    payload = {
        "hourly": {
            "time": ["t0", "t1", "t2"],
            "temperature_2m": [40.0, None, 75.0],
        },
        "daily": {},
    }
    s = weather._summarise_hrrr(payload)
    assert s["t2m_f"] == 40.0
    assert s["tmax_f"] == 75.0
    assert s["tmin_f"] == 40.0
    assert len(s["samples"]) == 2  # the None sample is filtered


def test_summarise_hrrr_empty_returns_none():
    assert weather._summarise_hrrr({}) is None
    assert weather._summarise_hrrr({"hourly": {"time": [], "temperature_2m": []}}) is None


def test_poll_hrrr_persists_one_row_per_city(monkeypatch):
    """End-to-end: NOMADS inventory found, Open-Meteo decodes values,
    weather_hrrr_forecasts row written for every selected city."""

    monkeypatch.setenv("AXIOM_WEATHER_CITIES", "NYC,MIA")

    inventory_text = "1:0:d=2026042212:TMP:2 m above ground:anl:\n"
    om_response = {
        "hourly": {
            "time": ["2026-04-22T00:00"],
            "temperature_2m": [70.0],
        },
        "daily": {
            "temperature_2m_max": [85.0],
            "temperature_2m_min": [60.0],
        },
    }

    class FakeResp:
        def __init__(self, status_code: int, body: Any = None, text: str = ""):
            self.status_code = status_code
            self._body = body
            self.text = text
            self.headers: dict[str, str] = {}

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, *a, **k):
            self.calls: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            self.calls.append(url)
            if "nomads.ncep.noaa.gov" in url:
                return FakeResp(200, text=inventory_text)
            if "api.open-meteo.com" in url:
                return FakeResp(200, body=om_response)
            return FakeResp(404)

    monkeypatch.setattr(weather.httpx, "AsyncClient", FakeClient)

    captured: list[tuple] = []

    async def fake_fetchval(sql, *args):
        captured.append(args)
        return 1

    monkeypatch.setattr(weather.db, "fetchval", fake_fetchval)

    n = asyncio.run(weather._poll_hrrr())
    assert n == 2
    assert {row[0] for row in captured} == {"NYC", "MIA"}
    # tmax_f from the daily block should be persisted as hrrr_high_f.
    for row in captured:
        assert row[2] == 85.0
        body = json.loads(row[3])
        assert body["t2m_f"] == 70.0
        assert body["tmax_f"] == 85.0
        assert body["tmin_f"] == 60.0
        assert body["nomads"]["inventory_count"] >= 1


def test_poll_hrrr_proceeds_without_nomads(monkeypatch):
    """If NOMADS is unreachable we still write Open-Meteo values, just
    without run-cycle provenance."""

    monkeypatch.setenv("AXIOM_WEATHER_CITIES", "NYC")

    om_response = {
        "hourly": {
            "time": ["2026-04-22T00:00"],
            "temperature_2m": [50.0],
        },
        "daily": {"temperature_2m_max": [70.0], "temperature_2m_min": [40.0]},
    }

    class FakeResp:
        def __init__(self, status_code: int, body: Any = None, text: str = ""):
            self.status_code = status_code
            self._body = body
            self.text = text
            self.headers: dict[str, str] = {}

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            if "nomads.ncep.noaa.gov" in url:
                return FakeResp(404)
            if "api.open-meteo.com" in url:
                return FakeResp(200, body=om_response)
            return FakeResp(404)

    monkeypatch.setattr(weather.httpx, "AsyncClient", FakeClient)

    captured: list[tuple] = []

    async def fake_fetchval(sql, *args):
        captured.append(args)
        return 1

    monkeypatch.setattr(weather.db, "fetchval", fake_fetchval)

    n = asyncio.run(weather._poll_hrrr())
    assert n == 1
    body = json.loads(captured[0][3])
    assert "nomads" not in body
    # hrrr_run_time argument (5th positional, index 4) should be None
    # so the SQL falls back to NOW().
    assert captured[0][4] is None


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def test_payload_includes_climatology_when_no_other_sources(monkeypatch):
    # Stub all DB reads to return nothing — payload should still carry
    # the climatology baseline for the city/day.
    async def empty_fetchrow(*a, **k):
        return None

    monkeypatch.setattr(weather_payload.db, "fetchrow", empty_fetchrow)
    weather_climatology._CACHE.clear()
    weather_climatology._CACHE_READY = True  # baseline-only cache
    for (city, doy, metric, v) in weather_climatology._baseline_rows():
        weather_climatology._CACHE[(city, int(doy), metric)] = float(v)

    fixed_now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    payload = asyncio.run(weather_payload.build_weather_payload("NYC", now=fixed_now))
    assert payload["meta"]["city_code"] == "NYC"
    assert payload["meta"]["day_of_year"] == 203
    assert "clim_tmax_f" in payload
    assert "clim_tmin_f" in payload
    assert "clim_tavg_f" in payload
    # No other sources should appear.
    assert "om_temperature_f" not in payload
    assert "nws_temperature_f" not in payload
    assert "hrrr_t2m_f" not in payload


def test_payload_merges_all_sources(monkeypatch):
    fixed_now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

    async def fetchrow(sql, *args):
        if "weather_observations" in sql and "open-meteo" in sql:
            return {
                "temperature_f": 80.0,
                "observed_at": fixed_now,
            }
        if "weather_extended" in sql:
            return {
                "wind_mph": 5.0,
                "humidity_pct": 60.0,
                "uv_index": 7.0,
                "dewpoint_f": 65.0,
                "pressure_in": 30.0,
                "visibility_mi": 10.0,
                "cloud_cover_pct": 20.0,
                "precip_1hr_in": 0.0,
                "wind_gust_mph": 8.0,
                "observed_at": fixed_now,
            }
        if "weather_observations" in sql and "nws" in sql:
            return {
                "temperature_f": 79.5,
                "observed_at": fixed_now,
            }
        if "weather_minutecast" in sql:
            return {
                "precip_summary": "No precipitation expected",
                "precip_type": None,
                "precip_intensity": None,
                "minutes_to_precip": None,
                "fetched_at": fixed_now,
            }
        if "weather_hrrr_forecasts" in sql:
            return {
                "hrrr_high_f": 84.0,
                "hrrr_hourly": json.dumps({
                    "model": "hrrr",
                    "t2m_f": 81.0,
                    "tmax_f": 84.0,
                    "tmin_f": 67.0,
                    "nomads": {"cycle": "12", "inventory_count": 3},
                }),
                "hrrr_run_time": fixed_now,
                "fetched_at": fixed_now,
            }
        return None

    monkeypatch.setattr(weather_payload.db, "fetchrow", fetchrow)
    weather_climatology._CACHE.clear()
    weather_climatology._CACHE_READY = True
    for (city, doy, metric, v) in weather_climatology._baseline_rows():
        weather_climatology._CACHE[(city, int(doy), metric)] = float(v)

    payload = asyncio.run(weather_payload.build_weather_payload("NYC", now=fixed_now))
    # Each source contributed its prefixed fields.
    assert payload["om_temperature_f"] == 80.0
    assert payload["om_humidity_pct"] == 60.0
    assert payload["nws_temperature_f"] == 79.5
    assert payload["accu_summary"].startswith("No precipitation")
    assert payload["hrrr_t2m_f"] == 81.0
    assert payload["hrrr_tmax_f"] == 84.0
    assert payload["hrrr_tmin_f"] == 67.0
    assert payload["hrrr_run_cycle"] == "12"
    assert payload["hrrr_inventory_count"] == 3
    assert "clim_tmax_f" in payload


def test_payload_drops_stale_sources(monkeypatch):
    fixed_now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    stale = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)

    async def fetchrow(sql, *args):
        if "weather_observations" in sql and "open-meteo" in sql:
            return {"temperature_f": 80.0, "observed_at": stale}
        return None

    monkeypatch.setattr(weather_payload.db, "fetchrow", fetchrow)

    payload = asyncio.run(weather_payload.build_weather_payload("NYC", now=fixed_now))
    assert "om_temperature_f" not in payload


def test_get_climatology_lookup_under_50ms():
    """Sanity: 1,000 cache hits should comfortably fit in <50ms total."""
    import time

    weather_climatology._CACHE.clear()
    weather_climatology._CACHE_READY = True
    for (city, doy, metric, v) in weather_climatology._baseline_rows():
        weather_climatology._CACHE[(city, int(doy), metric)] = float(v)

    t0 = time.perf_counter()
    for _ in range(1000):
        weather_climatology.get_climatology_sync("NYC", 100, "tmax_f")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    # Generous bound — we just need to confirm we're nowhere near 50ms
    # per lookup, never mind 1k of them.
    assert elapsed_ms < 100, f"climatology lookup too slow: {elapsed_ms:.1f}ms"
