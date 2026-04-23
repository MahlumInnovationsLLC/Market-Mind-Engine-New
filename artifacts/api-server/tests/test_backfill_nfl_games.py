"""Tests for the one-time NFL season backfill (task #207).

Covers:
  * Schedule payload normalization (weeks -> games -> rows)
  * Inline scoring + boxscore fallback paths
  * CSV parsing path
  * `_build_upsert_rows` produces the tuple shape `games` upsert expects
  * Margin regressor produces a non-trivial NFL fit when the backfilled
    rows are surfaced through `load_training_games`
  * Per-league isotonic calibrator improves Brier vs. the uncalibrated
    baseline on a held-out slice of NFL-shaped probabilities/outcomes
"""
from __future__ import annotations

import asyncio
import csv
import random
from pathlib import Path
from typing import Any

import pytest

from app import calibrators, margin_regressor
from scripts import backfill_nfl_games as backfill


# ---------------------------------------------------------------------------
# Schedule / boxscore parsing
# ---------------------------------------------------------------------------


def test_extract_games_from_schedule_walks_weeks():
    payload = {
        "year": 2024, "type": "REG",
        "weeks": [
            {
                "sequence": 1,
                "games": [
                    {
                        "id": "sr:nfl:2024:1:KC-BAL",
                        "scheduled": "2024-09-05T20:20:00Z",
                        "status": "closed",
                        "home": {"name": "Kansas City Chiefs"},
                        "away": {"name": "Baltimore Ravens"},
                        "scoring": {"home_points": 27, "away_points": 20},
                    },
                ],
            },
            {
                "sequence": 2,
                "games": [
                    {
                        "id": "sr:nfl:2024:2:GB-IND",
                        "scheduled": "2024-09-15T17:00:00Z",
                        "status": "scheduled",
                        "home": {"name": "Indianapolis Colts"},
                        "away": {"name": "Green Bay Packers"},
                    },
                ],
            },
        ],
    }
    games = backfill.extract_games_from_schedule(payload)
    assert len(games) == 2
    g1, g2 = games
    assert g1["id"] == "sr:nfl:2024:1:KC-BAL"
    assert g1["home_team"] == "Kansas City Chiefs"
    assert g1["away_team"] == "Baltimore Ravens"
    assert g1["home_score"] == 27
    assert g1["away_score"] == 20
    assert g1["status"] == "closed"
    # Future game keeps None scores (boxscore fallback won't fire for
    # 'scheduled' status anyway).
    assert g2["home_score"] is None
    assert g2["away_score"] is None


def test_extract_games_handles_top_level_points_shape():
    """Some Sportradar variants put the points at the top of the game
    object rather than nested under `scoring`."""
    payload = {
        "weeks": [{
            "sequence": 1,
            "games": [{
                "id": "g1",
                "scheduled": "2024-09-05T20:20:00Z",
                "status": "closed",
                "home": {"name": "A"}, "away": {"name": "B"},
                "home_points": 14, "away_points": 10,
            }],
        }],
    }
    games = backfill.extract_games_from_schedule(payload)
    assert games[0]["home_score"] == 14
    assert games[0]["away_score"] == 10


def test_extract_score_from_boxscore_handles_both_envelopes():
    wrapped = {
        "game": {
            "status": "closed",
            "home": {"points": 31}, "away": {"points": 24},
        },
    }
    h, a, st = backfill.extract_score_from_boxscore(wrapped)
    assert (h, a, st) == (31, 24, "closed")

    flat = {
        "status": "closed",
        "scoring": {"home_points": 17, "away_points": 14},
        "home": {}, "away": {},
    }
    h, a, st = backfill.extract_score_from_boxscore(flat)
    assert (h, a, st) == (17, 14, "closed")


# ---------------------------------------------------------------------------
# CSV path
# ---------------------------------------------------------------------------


def test_load_csv_parses_required_columns(tmp_path: Path):
    csv_path = tmp_path / "nfl.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "game_id", "home_team", "away_team",
            "home_score", "away_score", "scheduled_start", "status",
        ])
        writer.writeheader()
        writer.writerow({
            "game_id": "g1",
            "home_team": "Chiefs", "away_team": "Ravens",
            "home_score": "27", "away_score": "20",
            "scheduled_start": "2024-09-05T20:20:00Z",
            "status": "closed",
        })
    rows = backfill.load_csv(csv_path)
    assert len(rows) == 1
    assert rows[0]["id"] == "g1"
    assert rows[0]["home_score"] == 27
    assert rows[0]["status"] == "closed"


def test_load_csv_rejects_when_missing_required_column(tmp_path: Path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("game_id,home_team\n1,X\n")
    with pytest.raises(SystemExit):
        backfill.load_csv(csv_path)


# ---------------------------------------------------------------------------
# Upsert row projection
# ---------------------------------------------------------------------------


def test_build_upsert_rows_skips_incomplete_games():
    games = [
        # Good row.
        {
            "id": "g1", "home_team": "Chiefs", "away_team": "Ravens",
            "scheduled": "2024-09-05T20:20:00Z", "status": "closed",
            "home_score": 27, "away_score": 20,
        },
        # Missing scheduled — should be dropped.
        {
            "id": "g2", "home_team": "A", "away_team": "B",
            "scheduled": "", "status": "closed",
            "home_score": 1, "away_score": 0,
        },
        # Missing id — dropped.
        {
            "id": "", "home_team": "A", "away_team": "B",
            "scheduled": "2024-09-05T20:20:00Z", "status": "closed",
        },
    ]
    rows = backfill._build_upsert_rows(games)
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "g1"
    assert r[1] == "nfl"
    assert r[2] == "NFL"
    assert r[3] == "Chiefs"
    assert r[4] == "Ravens"
    assert r[5] == "2024-09-05T20:20:00Z"
    assert r[6] == "closed"
    assert r[7] == 27
    assert r[8] == 20


def test_merge_and_dedupe_keeps_first_occurrence():
    a = [{"id": "g1", "home_team": "X"}, {"id": "g2", "home_team": "Y"}]
    b = [{"id": "g1", "home_team": "OTHER"}, {"id": "g3", "home_team": "Z"}]
    merged = backfill.merge_and_dedupe(a, b)
    by_id = {g["id"]: g for g in merged}
    assert set(by_id.keys()) == {"g1", "g2", "g3"}
    assert by_id["g1"]["home_team"] == "X"  # first source wins


# ---------------------------------------------------------------------------
# Sportradar fetch wiring (httpx stubbed)
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class _StubClient:
    """Async-context-manager stand-in for `httpx.AsyncClient`."""

    def __init__(self, responses: dict[str, _StubResponse]) -> None:
        self._responses = responses
        self.requests: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, params: dict | None = None):
        self.requests.append(url)
        if url in self._responses:
            return self._responses[url]
        # Default 404 so unmocked calls are obvious.
        return _StubResponse({}, status=404)


def test_fetch_from_sportradar_falls_back_to_boxscore_for_missing_scores(
    monkeypatch,
):
    schedule_payload = {
        "weeks": [{
            "sequence": 1,
            "games": [
                {
                    "id": "g_with_score",
                    "scheduled": "2024-09-05T20:20:00Z",
                    "status": "closed",
                    "home": {"name": "A"}, "away": {"name": "B"},
                    "scoring": {"home_points": 27, "away_points": 20},
                },
                {
                    "id": "g_missing_score",
                    "scheduled": "2024-09-08T17:00:00Z",
                    "status": "closed",
                    "home": {"name": "C"}, "away": {"name": "D"},
                },
            ],
        }],
    }
    boxscore_payload = {
        "game": {
            "status": "closed",
            "home": {"points": 14}, "away": {"points": 10},
        },
    }
    schedule_url = backfill.SPORTRADAR_SCHEDULE_URL.format(year=2024, stype="REG")
    boxscore_url = backfill.SPORTRADAR_BOXSCORE_URL.format(
        game_id="g_missing_score",
    )
    stub = _StubClient({
        schedule_url: _StubResponse(schedule_payload),
        boxscore_url: _StubResponse(boxscore_payload),
    })

    def _fake_async_client(*a, **kw):
        return stub

    monkeypatch.setenv("SPORTRADAR_NFL_KEY", "sk_test")
    monkeypatch.setattr(backfill.httpx, "AsyncClient", _fake_async_client)

    games = asyncio.run(backfill.fetch_from_sportradar(2024, ["REG"]))
    by_id = {g["id"]: g for g in games}
    assert by_id["g_with_score"]["home_score"] == 27
    # Boxscore fallback filled in the missing scores.
    assert by_id["g_missing_score"]["home_score"] == 14
    assert by_id["g_missing_score"]["away_score"] == 10
    # Both schedule + boxscore endpoints were hit.
    assert schedule_url in stub.requests
    assert boxscore_url in stub.requests


def test_fetch_from_sportradar_requires_api_key(monkeypatch):
    monkeypatch.delenv("SPORTRADAR_NFL_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        asyncio.run(backfill.fetch_from_sportradar(2024, ["REG"]))


# ---------------------------------------------------------------------------
# Downstream effect:  refit_all_leagues now produces a non-trivial NFL fit
# ---------------------------------------------------------------------------


def _synthetic_nfl_training_rows(
    n: int = 300, seed: int = 207,
) -> list[dict[str, Any]]:
    """Mimic the row shape `load_training_games` projects from the DB
    after the backfill lands real NFL games.

    Embeds a real elo_diff -> margin signal so `fit_regressor` learns
    a non-zero coefficient (otherwise the test would pass trivially
    for any fit).
    """
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        elo_diff = rng.gauss(0.0, 200.0)
        margin = 0.06 * elo_diff + rng.gauss(0.0, 13.5)  # NFL-shaped SD
        total = 44.0 + rng.gauss(0.0, 13.0)
        spread_line = round((-0.06 * elo_diff) * 2.0) / 2.0
        rows.append({
            "league": "NFL",
            "game_id": f"nfl-2024-{i}",
            "home_team": f"H{i}",
            "away_team": f"A{i}",
            "home_score": round(total / 2.0 + margin / 2.0),
            "away_score": round(total / 2.0 - margin / 2.0),
            "spread_line": spread_line,
            "total_line": round(total),
            "elo_diff": elo_diff,
            "home_elo": 1500.0 + elo_diff / 2.0,
            "opponent_elo": 1500.0 - elo_diff / 2.0,
            "recent_form_delta": 0.0,
            "opponent_recent_form_delta": 0.0,
        })
    return rows


def test_refit_all_leagues_produces_nontrivial_nfl_after_backfill(monkeypatch):
    """With the backfilled NFL rows in `games`, refit_all_leagues
    should produce an actually-fitted NFL regressor — n_train > 0,
    coefficients not all zero, residual SD finite — instead of the
    cold-start fallback the orchestrator currently sees."""

    async def _fake_load(league, *, limit=None):
        if league == "NFL":
            return _synthetic_nfl_training_rows(n=300)
        return []

    async def _inner():
        margin_regressor.reset_registry()
        monkeypatch.setattr(margin_regressor, "load_training_games", _fake_load)
        # Patch the persist hook so this test doesn't need a live DB.
        async def _noop_persist(*a, **kw):
            return None
        monkeypatch.setattr(margin_regressor, "_persist_fit", _noop_persist)
        summary = await margin_regressor.refit_all_leagues(min_games=50)
        d = summary.as_dict()
        assert d["fitted"].get("NFL") == 300, d
        nfl = margin_regressor.get_regressor("NFL")
        assert nfl is not None
        assert nfl.is_fitted()
        assert nfl.n_train == 300
        # The cold-start fallback produces n_train=0 and uses the
        # textbook residual SD.  After the fit we should see at
        # least one non-zero margin coefficient (the elo_diff slope)
        # and a finite residual SD measured from the data.
        import math
        coefs = list(getattr(nfl, "margin_coef", []) or [])
        assert any(abs(float(c)) > 1e-6 for c in coefs), coefs
        margin_sd = float(getattr(nfl, "margin_sd", 0.0))
        assert math.isfinite(margin_sd) and margin_sd > 0.0

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Downstream effect:  per-league calibrator improves Brier on holdout
# ---------------------------------------------------------------------------


def _synthetic_miscalibrated_nfl_outcomes(
    n: int = 600, seed: int = 207,
) -> tuple[list[float], list[int]]:
    """Generate (raw_probability, actual_outcome) pairs whose marginal
    distribution is systematically miscalibrated — the raw model
    over-confidently assigns ~0.7 / ~0.3 probabilities but the true
    outcome rates are closer to ~0.6 / ~0.4.

    An isotonic fit on this is exactly what the per-league NFL
    calibrator would learn from a season's worth of settled NFL
    trades; the holdout Brier should drop after calibration.
    """
    rng = random.Random(seed)
    probs: list[float] = []
    ys: list[int] = []
    for _ in range(n):
        # Mix of buckets covering the typical distribution of
        # post-meta-model probabilities for NFL trades.
        p = rng.choice([0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80])
        # True outcome rate compresses extreme probs hard towards 0.5
        # (classic over-confidence pattern).  At p=0.8 the side really
        # only wins ~62%, which is exactly what isotonic fixes.
        true_p = 0.5 + 0.4 * (p - 0.5)
        y = 1 if rng.random() < true_p else 0
        probs.append(p)
        ys.append(y)
    return probs, ys


def test_isotonic_calibrator_improves_holdout_brier_for_nfl():
    probs, ys = _synthetic_miscalibrated_nfl_outcomes(n=800, seed=207)
    # 70/30 train/holdout split.
    cut = int(len(probs) * 0.7)
    train_p, train_y = probs[:cut], ys[:cut]
    hold_p, hold_y = probs[cut:], ys[cut:]

    state = calibrators._fit_isotonic(train_p, train_y)
    assert state["type"] == "isotonic"
    assert state["n"] == len(train_p)

    raw_brier = calibrators._brier(hold_p, hold_y)
    cal_probs = [calibrators._apply_isotonic(p, state) for p in hold_p]
    cal_brier = calibrators._brier(cal_probs, hold_y)

    # The calibrator should *strictly* reduce holdout Brier on this
    # systematically over-confident slice.
    assert cal_brier < raw_brier, (cal_brier, raw_brier)
    # Sanity: improvement is non-trivial (at least 1% relative).
    assert (raw_brier - cal_brier) / raw_brier > 0.01
