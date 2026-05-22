"""Coverage for the V1-archive feature accessors (task #818).

Hermetic — no DB.  Stubs the small db.fetch* surface used by the
accessors so each branch (flag off, table missing, no row, hit) is
covered.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

from app import db as _db
from app.v1_archive import features as v1f
from app.v1_archive import llm_backtest


# ---------------------------------------------------------------------------
# use_v1_features flag
# ---------------------------------------------------------------------------


def test_flag_defaults_on(monkeypatch):
    monkeypatch.delenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", raising=False)
    assert v1f.use_v1_features() is True


@pytest.mark.parametrize("val", ["0", "false", "No", "OFF", ""])
def test_flag_off_values(monkeypatch, val):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", val)
    assert v1f.use_v1_features() is False


# ---------------------------------------------------------------------------
# nearest_city_code
# ---------------------------------------------------------------------------


def test_nearest_city_picks_closest_default_city():
    # JFK is right next to NYC.
    assert v1f.nearest_city_code(40.6413, -73.7781) == "NYC"
    # Downtown Chicago.
    assert v1f.nearest_city_code(41.85, -87.65) == "CHI"


def test_nearest_city_returns_none_when_far():
    # Honolulu — no default city within 250 mi.
    assert v1f.nearest_city_code(21.30, -157.85) is None


# ---------------------------------------------------------------------------
# weather_for_coords
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


def test_weather_for_coords_flag_off_short_circuits(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "0")

    async def boom(*a, **kw):  # noqa: ANN001
        raise AssertionError("must not touch DB when flag is off")

    monkeypatch.setattr(_db, "fetchval", boom)
    monkeypatch.setattr(_db, "fetchrow", boom)
    out = asyncio.run(v1f.weather_for_coords(
        40.81, -74.07, datetime(2024, 1, 1, tzinfo=timezone.utc),
    ))
    assert out is None


def test_weather_for_coords_missing_table(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")

    async def fake_fetchval(q, *a):  # noqa: ANN001
        return None  # to_regclass returns NULL

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    out = asyncio.run(v1f.weather_for_coords(
        40.81, -74.07, datetime(2024, 1, 1, tzinfo=timezone.utc),
    ))
    assert out is None


def test_weather_for_coords_returns_row(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")
    captured: dict[str, Any] = {}

    async def fake_fetchval(q, *a):  # noqa: ANN001
        return True

    async def fake_fetchrow(q, *a):  # noqa: ANN001
        captured["city"] = a[0]
        captured["lo"] = a[1]
        captured["hi"] = a[2]
        return {
            "temperature_f": 41.5,
            "observed_at": datetime(2024, 1, 1, 18, tzinfo=timezone.utc),
            "source": "open-meteo",
        }

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetchrow", fake_fetchrow)

    # MetLife Stadium — closest default city is NYC.
    out = asyncio.run(v1f.weather_for_coords(
        40.8135, -74.0745, datetime(2024, 1, 1, 19, tzinfo=timezone.utc),
    ))
    assert out is not None
    assert out["temperature_f"] == 41.5
    assert out["city_code"] == "NYC"
    assert out["source"] == "v1_archive"
    assert out["v1_source"] == "open-meteo"
    assert captured["city"] == "NYC"
    # ±3-hour band by default.
    assert captured["hi"] - captured["lo"] == timedelta(hours=6)


def test_weather_for_coords_naive_datetime_assumed_utc(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")

    async def fake_fetchval(q, *a):  # noqa: ANN001
        return True

    async def fake_fetchrow(q, *a):  # noqa: ANN001
        # Naive input should be coerced; the bound timestamp must be tz-aware.
        assert a[1].tzinfo is not None
        return None

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetchrow", fake_fetchrow)
    out = asyncio.run(v1f.weather_for_coords(
        40.81, -74.07, datetime(2024, 1, 1, 19),
    ))
    assert out is None


# ---------------------------------------------------------------------------
# latest_line_for_game
# ---------------------------------------------------------------------------


def test_latest_line_for_game_returns_row(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")

    async def fake_fetchval(q, *a):  # noqa: ANN001
        return True

    captured: dict[str, Any] = {}

    async def fake_fetchrow(q, *a):  # noqa: ANN001
        captured["query"] = q
        captured["args"] = a
        return {
            "line_value": -3.5,
            "odds_value": -110,
            "book": "draftkings",
            "market_type": "spreads",
            "timestamp": datetime(2023, 9, 1, tzinfo=timezone.utc),
        }

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetchrow", fake_fetchrow)
    out = asyncio.run(v1f.latest_line_for_game(
        "g1", ("spreads", "spread"),
    ))
    assert out is not None
    assert out["line_value"] == -3.5
    assert out["odds_value"] == -110.0
    assert out["source"] == "v1_archive"
    # No "before" filter when not supplied.
    assert "timestamp\" <" not in captured["query"].replace(" ", "").lower()


def test_latest_line_for_game_with_before_caps_window(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")

    async def fake_fetchval(q, *a):  # noqa: ANN001
        return True

    captured: dict[str, Any] = {}

    async def fake_fetchrow(q, *a):  # noqa: ANN001
        captured["q"] = q
        captured["args"] = a
        return None

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetchrow", fake_fetchrow)
    asyncio.run(v1f.latest_line_for_game(
        "g1", ("spreads",),
        before=datetime(2023, 9, 1, tzinfo=timezone.utc),
    ))
    assert "<" in captured["q"]  # cap clause present
    assert captured["args"][-1].tzinfo is not None


def test_latest_line_for_game_empty_inputs_short_circuit(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")

    async def boom(*a, **kw):  # noqa: ANN001
        raise AssertionError("must not touch DB when inputs are empty")

    monkeypatch.setattr(_db, "fetchval", boom)
    monkeypatch.setattr(_db, "fetchrow", boom)
    assert asyncio.run(v1f.latest_line_for_game("", ("spreads",))) is None
    assert asyncio.run(v1f.latest_line_for_game("g1", ())) is None


def test_closing_lines_bulk(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")

    async def fake_fetchval(q, *a):  # noqa: ANN001
        return True

    async def fake_fetch(q, *a):  # noqa: ANN001
        return [
            {"game_id": "g1", "line_value": 6.5,
             "timestamp": datetime(2023, 9, 1, tzinfo=timezone.utc)},
            {"game_id": "g2", "line_value": -2.5,
             "timestamp": datetime(2023, 9, 2, tzinfo=timezone.utc)},
        ]

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetch", fake_fetch)
    out = asyncio.run(v1f.closing_lines_for_games(
        ("g1", "g2", "g3"), ("totals",),
    ))
    assert set(out.keys()) == {"g1", "g2"}
    assert out["g1"]["line_value"] == 6.5
    assert out["g2"]["source"] == "v1_archive"


# ---------------------------------------------------------------------------
# llm_backtest.replay
# ---------------------------------------------------------------------------


def test_llm_replay_empty_when_disabled(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "0")

    async def boom(*a, **kw):  # noqa: ANN001
        raise AssertionError("no DB calls when archive disabled")

    monkeypatch.setattr(_db, "fetchval", boom)
    monkeypatch.setattr(_db, "fetch", boom)
    out = asyncio.run(llm_backtest.replay())
    assert out.total_rows == 0
    assert out.buckets == ()


def test_llm_replay_empty_when_table_missing(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")

    async def fake_fetchval(q, *a):  # noqa: ANN001
        return None

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    out = asyncio.run(llm_backtest.replay())
    assert out.total_rows == 0


def test_llm_replay_aggregates_buckets(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")
    captured: dict[str, Any] = {}

    async def fake_fetchval(q, *a):  # noqa: ANN001
        return True

    async def fake_fetch(q, *a):  # noqa: ANN001
        captured["q"] = q
        captured["args"] = a
        return [
            {"provider": "openai", "model_id": "gpt-4", "league": "NFL",
             "category": "sports", "n": 100, "n_correct": 60,
             "win_rate": 0.6, "brier": 0.22, "avg_confidence": 0.7},
            {"provider": "openai", "model_id": "gpt-4", "league": "NBA",
             "category": "sports", "n": 50, "n_correct": 20,
             "win_rate": 0.4, "brier": 0.30, "avg_confidence": 0.6},
        ]

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetch", fake_fetch)
    out = asyncio.run(llm_backtest.replay(
        providers=("openai",), leagues=("nfl", "nba"),
    ))
    assert out.total_rows == 150
    assert len(out.buckets) == 2
    # Weighted win-rate over 100+50: (60+20)/150
    assert out.overall_win_rate == pytest.approx(80 / 150)
    # Weighted Brier: (0.22*100 + 0.30*50) / 150
    assert out.overall_brier == pytest.approx((0.22 * 100 + 0.30 * 50) / 150)
    # Filters echoed for audit.
    assert out.filters["providers"] == ["openai"]
    assert out.filters["leagues"] == ["nfl", "nba"]
    # Query upper-cased the league filter before binding.
    leagues_param = next(p for p in captured["args"] if p == ["NFL", "NBA"])
    assert leagues_param == ["NFL", "NBA"]


def test_llm_replay_handles_missing_brier(monkeypatch):
    monkeypatch.setenv("AXIOM_USE_V1_ARCHIVE_FOR_FEATURES", "1")

    async def fake_fetchval(q, *a):  # noqa: ANN001
        return True

    async def fake_fetch(q, *a):  # noqa: ANN001
        return [
            {"provider": "anthropic", "model_id": "sonnet", "league": "MLB",
             "category": "sports", "n": 10, "n_correct": 4,
             "win_rate": 0.4, "brier": None, "avg_confidence": None},
        ]

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)
    monkeypatch.setattr(_db, "fetch", fake_fetch)
    out = asyncio.run(llm_backtest.replay())
    assert out.total_rows == 10
    assert out.overall_brier is None
    assert out.overall_avg_confidence is None
    assert out.buckets[0].brier is None


# ----------------------------------------------------------------------
# MLB + golf weather builders (task #818 phase 2 wiring).
# ----------------------------------------------------------------------
from datetime import datetime, timezone

import pytest

from app.sports import mlb_context, golf_context


def test_mlb_resolve_ballpark_aliases():
    p = mlb_context.resolve_ballpark("Yankees")
    assert p is not None and p.name == "Yankee Stadium"
    assert mlb_context.resolve_ballpark("new york yankees") is p
    assert mlb_context.resolve_ballpark("Cubs").roof is False
    assert mlb_context.resolve_ballpark("Rays").roof is True
    assert mlb_context.resolve_ballpark("Unknown FC") is None
    assert mlb_context.resolve_ballpark(None) is None


def test_mlb_fetch_weather_dome_short_circuits(monkeypatch):
    async def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("v1 weather should not be queried for domes")
    monkeypatch.setattr(mlb_context.v1_features, "weather_for_coords", boom)
    out = asyncio.run(mlb_context.fetch_weather(
        "Rays", observed_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
    ))
    assert out == {
        "venue": "Tropicana Field",
        "venue_type": "indoor",
        "weather_irrelevant": True,
    }


def test_mlb_fetch_weather_outdoor_uses_v1(monkeypatch):
    async def fake(lat, lon, ts):
        assert abs(lat - 40.8296) < 1e-3
        return {"temperature_f": 71.0, "city_code": "NYC",
                "observed_at": ts.isoformat()}
    monkeypatch.setattr(mlb_context.v1_features, "weather_for_coords", fake)
    out = asyncio.run(mlb_context.fetch_weather(
        "Yankees", observed_at=datetime(2024, 7, 4, 19, tzinfo=timezone.utc),
    ))
    assert out["source"] == "v1_archive"
    assert out["temperature_f"] == 71.0
    assert out["v1_city_code"] == "NYC"


def test_mlb_fetch_weather_outdoor_no_observed_at(monkeypatch):
    monkeypatch.setattr(mlb_context.v1_features, "weather_for_coords",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("should not be called")))
    out = asyncio.run(mlb_context.fetch_weather("Cubs"))
    assert out == {"venue": "Wrigley Field", "venue_type": "outdoor",
                   "forecast_unavailable": True}


def test_golf_resolve_course_aliases_and_substring():
    assert golf_context.resolve_course("Masters").name == "Augusta National"
    assert golf_context.resolve_course(
        "The Masters at Augusta National Golf Club"
    ).name == "Augusta National"
    assert golf_context.resolve_course("waste mgmt phoenix open").name == \
        "TPC Scottsdale"
    assert golf_context.resolve_course("Some Other Course") is None
    assert golf_context.resolve_course(None) is None
    assert golf_context.resolve_course("") is None


def test_golf_fetch_weather_uses_v1(monkeypatch):
    async def fake(lat, lon, ts):
        return {"temperature_f": 64.0, "city_code": "ATL",
                "observed_at": ts.isoformat()}
    monkeypatch.setattr(golf_context.v1_features, "weather_for_coords", fake)
    out = asyncio.run(golf_context.fetch_weather(
        "Masters", observed_at=datetime(2024, 4, 12, tzinfo=timezone.utc),
    ))
    assert out["venue"] == "Augusta National"
    assert out["source"] == "v1_archive"
    assert out["temperature_f"] == 64.0


def test_golf_fetch_weather_unknown_returns_none():
    out = asyncio.run(golf_context.fetch_weather("Made Up Open"))
    assert out is None


def test_golf_build_game_context_no_observed_at():
    out = asyncio.run(golf_context.build_game_context("Pebble Beach"))
    assert out["weather"]["venue"] == "Pebble Beach"
    assert out["weather"]["forecast_unavailable"] is True
