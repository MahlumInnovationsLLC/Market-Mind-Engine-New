"""Tests for the surrogate-vs-V1 backtest report loader (task #739).

The loader is a pure read-only adapter over the JSON the backtest
script writes to ``scripts/reports/v1_backtest_sample_surrogate_*.json``.
We exercise:

  * Picking the most recent report when several share a directory.
  * Per-league delta + sample_count + surrogate-coverage% extraction.
  * The "no report on disk" placeholder shape.
  * Defensive parsing of malformed JSON.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import surrogate_report


def _write_report(directory: Path, name: str, payload: dict) -> Path:
    p = directory / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


SAMPLE_PAYLOAD = {
    "started_at": "2026-04-29T20:26:22.154158+00:00",
    "completed_at": "2026-04-29T20:26:24.389608+00:00",
    "row_input_count": 8000,
    "row_kept_count": 757,
    "per_league": {
        "MLB": {
            "sample_count": 41,
            "calibrator_applied_count": 41,
            "calibrator_type": "isotonic",
            "brier_v1_raw": 0.21,
            "brier_calibrated": 0.02,
            "delta_vs_v1": -0.19,
            "verdict": "improved",
        },
        "NBA": {
            "sample_count": 367,
            "calibrator_applied_count": 360,
            "calibrator_type": "isotonic",
            "brier_v1_raw": 0.36,
            "brier_calibrated": 0.27,
            "delta_vs_v1": -0.10,
            "verdict": "improved",
        },
        # A regression — must sort to the bottom of the table.
        "NCAAB": {
            "sample_count": 31,
            "calibrator_applied_count": 31,
            "calibrator_type": "platt",
            "brier_v1_raw": 0.40,
            "brier_calibrated": 0.45,
            "delta_vs_v1": 0.05,
            "verdict": "regressed",
        },
    },
    "summary": {"league_count": 3},
}


def test_load_latest_returns_unavailable_when_dir_missing(tmp_path: Path) -> None:
    out = surrogate_report.load_latest(tmp_path / "does-not-exist")
    assert out["available"] is False
    assert out["leagues"] == []
    assert out["report_path"] is None
    assert out["degraded_baseline"] is True
    assert "DEGRADED" in out["disclaimer"]


def test_load_latest_returns_unavailable_when_dir_empty(tmp_path: Path) -> None:
    out = surrogate_report.load_latest(tmp_path)
    assert out["available"] is False
    assert "no surrogate" in (out["reason"] or "").lower()


def test_latest_report_path_picks_newest_by_date(tmp_path: Path) -> None:
    _write_report(tmp_path, "v1_backtest_sample_surrogate_8k_2026-04-28.json", {})
    newer = _write_report(
        tmp_path, "v1_backtest_sample_surrogate_8k_2026-04-29.json", {}
    )
    # An unrelated report in the same directory must be ignored.
    _write_report(tmp_path, "v1_backtest_full_archive_2026-04-29.json", {})
    assert surrogate_report.latest_report_path(tmp_path) == newer


def test_latest_report_path_prefers_parsed_date_over_size_token(tmp_path: Path) -> None:
    """A bare ``surrogate_<date>.json`` written *after* an
    ``surrogate_8k_<older-date>.json`` must still win — naive
    lexicographic sort would prefer the size-token-suffixed older
    file because ``8k`` > "" character-by-character before the date
    is even reached."""
    _write_report(
        tmp_path, "v1_backtest_sample_surrogate_8k_2026-04-28.json", {}
    )
    newer = _write_report(
        tmp_path, "v1_backtest_sample_surrogate_2026-04-29.json", {}
    )
    assert surrogate_report.latest_report_path(tmp_path) == newer


def test_load_latest_extracts_per_league_metrics(tmp_path: Path) -> None:
    path = _write_report(
        tmp_path,
        "v1_backtest_sample_surrogate_8k_2026-04-29.json",
        SAMPLE_PAYLOAD,
    )
    out = surrogate_report.load_latest(tmp_path)

    assert out["available"] is True
    assert out["report_path"] == str(path)
    assert out["report_filename"] == path.name
    assert out["generated_at"] == SAMPLE_PAYLOAD["completed_at"]
    assert out["row_input_count"] == 8000
    assert out["row_kept_count"] == 757
    assert out["degraded_baseline"] is True

    by_league = {row["league"]: row for row in out["leagues"]}
    assert set(by_league) == {"MLB", "NBA", "NCAAB"}

    mlb = by_league["MLB"]
    assert mlb["sample_count"] == 41
    assert mlb["delta_vs_v1"] == pytest.approx(-0.19)
    # 41/41 -> 100.0% surrogate coverage.
    assert mlb["surrogate_coverage_pct"] == pytest.approx(100.0)
    assert mlb["calibrator_type"] == "isotonic"
    assert mlb["verdict"] == "improved"

    nba = by_league["NBA"]
    # 360 / 367 = 98.0926... -> rounded to 2dp = 98.09.
    assert nba["surrogate_coverage_pct"] == pytest.approx(98.09)


def test_load_latest_sorts_improvements_first_regressions_last(tmp_path: Path) -> None:
    _write_report(
        tmp_path,
        "v1_backtest_sample_surrogate_8k_2026-04-29.json",
        SAMPLE_PAYLOAD,
    )
    out = surrogate_report.load_latest(tmp_path)
    leagues = [row["league"] for row in out["leagues"]]
    # MLB (-0.19) is the biggest improvement so it pins to the top;
    # NCAAB is the only regression so it pins to the bottom.
    assert leagues[0] == "MLB"
    assert leagues[-1] == "NCAAB"
    assert "NBA" in leagues[1:-1]


def test_load_latest_handles_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "v1_backtest_sample_surrogate_8k_2026-04-29.json"
    p.write_text("{ not valid json", encoding="utf-8")
    out = surrogate_report.load_latest(tmp_path)
    assert out["available"] is False
    assert out["report_filename"] == p.name
    assert "failed to parse" in (out["reason"] or "").lower()
    # Even on parse failure the disclaimer + degraded flag must be
    # present so the dashboard banner still renders.
    assert out["degraded_baseline"] is True
    assert out["leagues"] == []


def test_load_latest_drops_non_dict_per_league_entries(tmp_path: Path) -> None:
    payload = {
        "completed_at": "2026-04-29T20:26:24+00:00",
        "per_league": {
            "MLB": {"sample_count": 5, "delta_vs_v1": -0.1},
            # Garbage entries must be silently dropped, not crash the
            # loader.
            "GARBAGE": "not-a-dict",
            "NULL": None,
        },
    }
    _write_report(tmp_path, "v1_backtest_sample_surrogate_x_2026-04-29.json", payload)
    out = surrogate_report.load_latest(tmp_path)
    assert out["available"] is True
    assert [row["league"] for row in out["leagues"]] == ["MLB"]


def test_reports_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_SURROGATE_REPORTS_DIR", str(tmp_path))
    _write_report(
        tmp_path,
        "v1_backtest_sample_surrogate_8k_2026-04-29.json",
        SAMPLE_PAYLOAD,
    )
    # Calling without an explicit directory must respect the env var.
    path = surrogate_report.latest_report_path()
    assert path is not None
    assert path.parent == tmp_path
