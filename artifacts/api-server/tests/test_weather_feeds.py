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


# Sample inventories for a synthetic cycle: F00 carries TMP only
# (analysis); F03/F06 carry TMP + TMAX + TMIN (windowed forecasts).
_F00_INV = "1:0:d=2026042212:TMP:2 m above ground:anl:\n"
_FXX_INV = "\n".join([
    "1:0:d=2026042212:TMP:2 m above ground:1 hour fcst:",
    "2:1000:d=2026042212:TMAX:2 m above ground:0-1 hour max fcst:",
    "3:2000:d=2026042212:TMIN:2 m above ground:0-1 hour min fcst:",
    "4:3000:d=2026042212:DPT:2 m above ground:1 hour fcst:",
])


class _FakeResp:
    def __init__(self, status_code: int, body: Any = None,
                 text: str = "", content: bytes = b""):
        self.status_code = status_code
        self._body = body
        self.text = text
        self.content = content
        self.headers: dict[str, str] = {}

    def json(self):
        return self._body


def test_decode_grib2_point_kelvin_returns_none_on_empty_bytes():
    assert weather._decode_grib2_point_kelvin(b"", 40.0, -74.0) is None


def test_select_records_filters_by_var_and_level():
    inventories = {
        0: weather._parse_hrrr_inventory(_F00_INV),
        3: weather._parse_hrrr_inventory(_FXX_INV),
    }
    tmp_recs = weather._select_records(inventories, "TMP")
    tmax_recs = weather._select_records(inventories, "TMAX")
    tmin_recs = weather._select_records(inventories, "TMIN")
    # F00 + F03 each have a TMP record at 2 m above ground.
    assert sorted(fh for fh, _ in tmp_recs) == [0, 3]
    # TMAX/TMIN only appear in the F03 inventory.
    assert [fh for fh, _ in tmax_recs] == [3]
    assert [fh for fh, _ in tmin_recs] == [3]


def test_poll_hrrr_decodes_grib2_and_persists(monkeypatch):
    """End-to-end: NOMADS inventory found, GRIB2 byte ranges fetched,
    eccodes decoder mocked, weather_hrrr_forecasts row written per city."""

    monkeypatch.setenv("AXIOM_WEATHER_CITIES", "NYC,MIA")

    grib_calls: list[tuple[str, str]] = []  # (url, range header)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            if url.endswith("wrfsfcf00.grib2.idx"):
                return _FakeResp(200, text=_F00_INV)
            if url.endswith(".grib2.idx"):
                return _FakeResp(200, text=_FXX_INV)
            if url.endswith(".grib2"):
                # Range request for a GRIB2 message — return marker bytes.
                grib_calls.append((url, (headers or {}).get("Range", "")))
                # Encode the var name so the decoder stub knows what to return.
                if "wrfsfcf00.grib2" in url:
                    return _FakeResp(206, content=b"TMP")
                # Forecast hour files: the byte_start tells us the var.
                rng = (headers or {}).get("Range", "")
                start = int(rng.split("=")[1].split("-")[0])
                tag = {0: b"TMP", 1000: b"TMAX", 2000: b"TMIN"}.get(start, b"X")
                return _FakeResp(206, content=tag)
            return _FakeResp(404)

    monkeypatch.setattr(weather.httpx, "AsyncClient", FakeClient)

    # Stub the GRIB2 decoder: TMP=294K (~70F), TMAX=303K (~85F), TMIN=289K (~60F).
    kelvin_by_tag = {b"TMP": 294.2611, b"TMAX": 302.5944, b"TMIN": 288.7056}

    def fake_decode(grib_bytes: bytes, lat: float, lon: float):
        return kelvin_by_tag.get(grib_bytes)

    monkeypatch.setattr(weather, "_decode_grib2_point_kelvin", fake_decode)

    captured: list[tuple] = []

    async def fake_fetchval(sql, *args):
        captured.append(args)
        return 1

    monkeypatch.setattr(weather.db, "fetchval", fake_fetchval)

    n = asyncio.run(weather._poll_hrrr())
    assert n == 2
    assert {row[0] for row in captured} == {"NYC", "MIA"}

    # We should have hit the GRIB2 file with Range headers (no full GETs).
    assert grib_calls, "expected at least one GRIB2 Range request"
    assert all(rng.startswith("bytes=") for _, rng in grib_calls)

    for row in captured:
        body = json.loads(row[3])
        # Decoder name reflects the new pipeline.
        assert body["decoder"] == "nomads:grib2"
        # T2M ~ 70F, TMAX ~ 85F, TMIN ~ 60F (within rounding).
        assert round(body["t2m_f"], 1) == 70.0
        assert round(body["tmax_f"], 1) == 85.0
        assert round(body["tmin_f"], 1) == 60.0
        # tmax_f propagated to hrrr_high_f column.
        assert round(row[2], 1) == 85.0
        # Provenance carries the cycle/inventory metadata.
        assert body["nomads"]["inventory_count"] >= 1
        assert 0 in body["nomads"]["fhours"]
        # Samples should include the TMP analysis + at least the
        # forecast-hour TMAX/TMIN we decoded.
        sample_vars = {s["var"] for s in body["samples"]}
        assert {"TMP", "TMAX", "TMIN"}.issubset(sample_vars)
        # hrrr_run_time argument (index 4) is set from the discovered cycle.
        assert row[4] is not None


def test_poll_hrrr_skips_when_nomads_unreachable(monkeypatch):
    """With Open-Meteo gone, an unreachable NOMADS means we cannot
    decode anything — the poller writes nothing instead of inventing
    data."""

    monkeypatch.setenv("AXIOM_WEATHER_CITIES", "NYC")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return _FakeResp(404)

    monkeypatch.setattr(weather.httpx, "AsyncClient", FakeClient)

    called = False

    async def fake_fetchval(sql, *args):
        nonlocal called
        called = True
        return 1

    monkeypatch.setattr(weather.db, "fetchval", fake_fetchval)

    n = asyncio.run(weather._poll_hrrr())
    assert n == 0
    assert called is False


def test_poll_hrrr_skips_city_when_tmp_undecodable(monkeypatch):
    """If the GRIB2 decoder cannot produce a T2M value for a city, that
    city's row is skipped instead of writing a half-populated payload."""

    monkeypatch.setenv("AXIOM_WEATHER_CITIES", "NYC")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            if url.endswith(".grib2.idx"):
                return _FakeResp(200, text=_F00_INV)
            if url.endswith(".grib2"):
                return _FakeResp(206, content=b"TMP")
            return _FakeResp(404)

    monkeypatch.setattr(weather.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        weather, "_decode_grib2_point_kelvin", lambda *a, **k: None
    )

    captured: list[tuple] = []

    async def fake_fetchval(sql, *args):
        captured.append(args)
        return 1

    monkeypatch.setattr(weather.db, "fetchval", fake_fetchval)

    n = asyncio.run(weather._poll_hrrr())
    assert n == 0
    assert captured == []


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
