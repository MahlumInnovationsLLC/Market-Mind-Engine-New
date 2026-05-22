"""Tests for ``dashboard.get_soccer_live_predictions`` (Task #879).

The in-game loop persists the soccer shadow-predictor's posterior to
``predictions.model_yes_prob`` and stashes the feature blob + calibration
info under ``predictions.metadata.live_prediction``.  The dashboard
helper rolls those rows up for the operator console so the pre-game
prior vs in-game posterior vs Kalshi mid delta is visible before
``risk.live_loop_enabled_soccer`` is flipped.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest


def _row(
    *,
    pid: int,
    market_id: str,
    league: str = "EPL",
    model_yes_prob: float | None = 0.60,
    kalshi_price: float | None = 0.55,
    pregame_prob: float | None = 0.45,
    home_score: int = 1,
    away_score: int = 0,
    minutes_elapsed: float = 55.0,
    has_live_state: float = 1.0,
    is_live: bool = True,
    game_id: str = "GAME-1",
    skipped_reason: str | None = None,
    extra_features: dict[str, Any] | None = None,
    raw_live_prediction: Any = None,
) -> dict[str, Any]:
    features: dict[str, Any] = {
        "pregame_prob": pregame_prob,
        "kalshi_price": kalshi_price,
        "home_score": home_score,
        "away_score": away_score,
        "minutes_elapsed": minutes_elapsed,
        "minutes_fraction": minutes_elapsed / 90.0 if minutes_elapsed else 0.0,
        "has_live_state": has_live_state,
        "state_age_seconds": 4.0,
    }
    if extra_features:
        features.update(extra_features)
    blob: dict[str, Any] | None = {
        "source": "soccer_live",
        "league": league,
        "features": features,
        "raw_probability": 0.58,
        "calibration": {"applied": True, "method": "isotonic"},
    }
    if skipped_reason:
        blob["skipped_reason"] = skipped_reason
    return {
        "id": pid,
        "kalshi_market_id": market_id,
        "game_id": game_id,
        "league": league,
        "is_live": is_live,
        "model_yes_prob": model_yes_prob,
        "kalshi_price": kalshi_price,
        "game_context": {"matchup": f"Home v Away #{pid}"},
        "game_time": None,
        "live_prediction": (
            raw_live_prediction if raw_live_prediction is not None else blob
        ),
    }


def test_get_soccer_live_predictions_basic(monkeypatch):
    from app import dashboard

    captured: dict[str, Any] = {}

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        captured["query"] = query
        captured["args"] = args
        return [
            _row(pid=1, market_id="MKT-EPL-A"),
            _row(
                pid=2,
                market_id="MKT-LALIGA-B",
                league="LALIGA",
                model_yes_prob=0.40,
                kalshi_price=0.50,
                pregame_prob=0.55,
                minutes_elapsed=10.0,
                home_score=0,
                away_score=0,
            ),
        ]

    monkeypatch.setattr(dashboard.db, "fetch", _fake_fetch)

    out = asyncio.run(dashboard.get_soccer_live_predictions(limit=50))

    # League filter is sourced from soccer_live.SOCCER_LEAGUES, upper-cased.
    leagues_arg = captured["args"][0]
    assert "EPL" in leagues_arg and "LALIGA" in leagues_arg
    assert all(l == l.upper() for l in leagues_arg)
    # Limit clamp is applied to the second positional arg.
    assert captured["args"][1] == 50

    assert out["count"] == 2
    assert set(out["leagues"]) >= {"EPL", "LALIGA"}
    items = {it["prediction_id"]: it for it in out["items"]}

    a = items[1]
    assert a["market_id"] == "MKT-EPL-A"
    assert a["league"] == "EPL"
    assert a["pregame_prob"] == pytest.approx(0.45)
    assert a["live_prob"] == pytest.approx(0.60)
    assert a["kalshi_price"] == pytest.approx(0.55)
    assert a["delta_vs_pregame"] == pytest.approx(0.60 - 0.45)
    assert a["delta_vs_market"] == pytest.approx(0.60 - 0.55)
    assert a["home_score"] == 1 and a["away_score"] == 0
    assert a["minutes_elapsed"] == pytest.approx(55.0)
    assert a["matchup"] == "Home v Away #1"
    assert a["is_live"] is True
    # Forensics blob is passed through intact so the UI can drill in.
    assert isinstance(a["live_prediction"], dict)
    assert a["live_prediction"]["source"] == "soccer_live"
    assert a["calibration"] == {"applied": True, "method": "isotonic"}

    b = items[2]
    assert b["delta_vs_pregame"] == pytest.approx(0.40 - 0.55)
    assert b["delta_vs_market"] == pytest.approx(0.40 - 0.50)


def test_get_soccer_live_predictions_handles_json_string_blob(monkeypatch):
    """Asyncpg may return jsonb columns as Python strings depending on
    codec setup. The helper must transparently decode them."""
    from app import dashboard

    raw_blob = json.dumps(
        {
            "source": "soccer_live",
            "features": {
                "pregame_prob": 0.30,
                "kalshi_price": 0.40,
                "home_score": 0,
                "away_score": 1,
                "minutes_elapsed": 75.0,
                "has_live_state": 1.0,
            },
        }
    )

    async def _fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        return [
            _row(
                pid=3,
                market_id="MKT-SERIEA-C",
                league="SERIEA",
                model_yes_prob=0.38,
                kalshi_price=0.40,
                raw_live_prediction=raw_blob,
            ),
        ]

    monkeypatch.setattr(dashboard.db, "fetch", _fake_fetch)
    out = asyncio.run(dashboard.get_soccer_live_predictions())
    assert out["count"] == 1
    it = out["items"][0]
    assert it["pregame_prob"] == pytest.approx(0.30)
    assert it["live_prob"] == pytest.approx(0.38)
    assert it["delta_vs_pregame"] == pytest.approx(0.38 - 0.30)
    assert it["minutes_elapsed"] == pytest.approx(75.0)
    assert isinstance(it["live_prediction"], dict)


def test_get_soccer_live_predictions_filters_unusable_rows(monkeypatch):
    """A blob with neither prior nor posterior nor market price is
    forensics-only — the operator view drops it so the table isn't
    polluted with rows that have nothing to compare."""
    from app import dashboard

    async def _fake_fetch(_q: str, *_a: Any) -> list[dict[str, Any]]:
        return [
            _row(
                pid=10,
                market_id="MKT-X",
                model_yes_prob=None,
                kalshi_price=None,
                pregame_prob=None,
                skipped_reason="no pre-game prior and no market price",
            ),
            _row(pid=11, market_id="MKT-Y"),
        ]

    monkeypatch.setattr(dashboard.db, "fetch", _fake_fetch)
    out = asyncio.run(dashboard.get_soccer_live_predictions())
    ids = [it["prediction_id"] for it in out["items"]]
    assert ids == [11]
    assert out["count"] == 1


def test_get_soccer_live_predictions_swallows_db_error(monkeypatch):
    from app import dashboard

    async def _boom(_q: str, *_a: Any) -> list[dict[str, Any]]:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(dashboard.db, "fetch", _boom)
    out = asyncio.run(dashboard.get_soccer_live_predictions())
    assert out["items"] == []
    assert out["count"] == 0
    assert "error" in out
    assert "RuntimeError" in out["error"]


def test_get_soccer_live_predictions_limit_clamped(monkeypatch):
    from app import dashboard

    captured: dict[str, Any] = {}

    async def _fake_fetch(_q: str, *args: Any) -> list[dict[str, Any]]:
        captured["limit"] = args[1]
        return []

    monkeypatch.setattr(dashboard.db, "fetch", _fake_fetch)

    asyncio.run(dashboard.get_soccer_live_predictions(limit=0))
    assert captured["limit"] == 1
    asyncio.run(dashboard.get_soccer_live_predictions(limit=10_000))
    assert captured["limit"] == 500
