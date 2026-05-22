"""Tests for the safety-threshold tuner scheduler + report loader
(task #814)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import safety_threshold_report
from app.notifications import safety_threshold_tuner


def _run(coro):
    return asyncio.run(coro)


def _write_report(
    directory: Path,
    name: str,
    *,
    sample_count: int = 100,
    sharpe: float | None = -0.25,
    ruin: float | None = 0.05,
    segment: int | None = 2,
    extra_verticals: dict[str, dict[str, float | int | None]] | None = None,
) -> Path:
    payload = {
        "generated_at": "2026-05-22T00:00:00+00:00",
        "source": "fixture:test",
        "sweeps": [
            {
                "vertical": "__all__",
                "sample_count": sample_count,
                "recommended": {
                    "sharpe_min_ratio": sharpe,
                    "ruin_max_probability": ruin,
                    "segment_max_losses": segment,
                },
            }
        ],
    }
    for v, rec in (extra_verticals or {}).items():
        payload["sweeps"].append(
            {"vertical": v, "sample_count": rec.pop("__samples", 50), "recommended": rec}
        )
    p = directory / name
    p.write_text(json.dumps(payload))
    return p


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_loader_returns_empty_when_directory_missing(tmp_path: Path) -> None:
    out = safety_threshold_report.load_latest(tmp_path / "missing")
    assert out["available"] is False
    assert out["rows"] == []


def test_loader_returns_latest_with_no_prior_drift(tmp_path: Path) -> None:
    _write_report(tmp_path, "safety_threshold_tuning_2026-05-20T000000Z.json")
    out = safety_threshold_report.load_latest(tmp_path)
    assert out["available"] is True
    assert out["report_filename"] == "safety_threshold_tuning_2026-05-20T000000Z.json"
    assert out["prior_report_filename"] is None
    # __all__ row exists with three knobs, no prior so deltas are None.
    assert out["rows"][0]["vertical"] == "__all__"
    knobs = out["rows"][0]["knobs"]
    assert knobs["sharpe_min_ratio"]["current"] == -0.25
    assert knobs["sharpe_min_ratio"]["prior"] is None
    assert knobs["sharpe_min_ratio"]["delta"] is None
    assert knobs["sharpe_min_ratio"]["changed"] is False
    assert knobs["segment_max_losses"]["current"] == 2


def test_loader_computes_drift_vs_prior(tmp_path: Path) -> None:
    _write_report(
        tmp_path,
        "safety_threshold_tuning_2026-05-19T000000Z.json",
        sharpe=-0.25,
        ruin=0.05,
        segment=3,
    )
    _write_report(
        tmp_path,
        "safety_threshold_tuning_2026-05-20T000000Z.json",
        sharpe=0.0,
        ruin=0.05,
        segment=2,
    )
    out = safety_threshold_report.load_latest(tmp_path)
    assert out["available"] is True
    assert out["report_filename"] == "safety_threshold_tuning_2026-05-20T000000Z.json"
    assert out["prior_report_filename"] == "safety_threshold_tuning_2026-05-19T000000Z.json"
    knobs = out["rows"][0]["knobs"]
    # Sharpe shifted up by 0.25 — must flag as changed.
    assert knobs["sharpe_min_ratio"]["current"] == 0.0
    assert knobs["sharpe_min_ratio"]["prior"] == -0.25
    assert knobs["sharpe_min_ratio"]["delta"] == pytest.approx(0.25)
    assert knobs["sharpe_min_ratio"]["changed"] is True
    # Ruin unchanged — delta = 0, NOT changed.
    assert knobs["ruin_max_probability"]["delta"] == pytest.approx(0.0)
    assert knobs["ruin_max_probability"]["changed"] is False
    # Segment shifted from 3 to 2.
    assert knobs["segment_max_losses"]["current"] == 2
    assert knobs["segment_max_losses"]["prior"] == 3
    assert knobs["segment_max_losses"]["delta"] == pytest.approx(-1.0)
    assert knobs["segment_max_losses"]["changed"] is True


def test_loader_handles_unparseable_report(tmp_path: Path) -> None:
    bad = tmp_path / "safety_threshold_tuning_2026-05-22T000000Z.json"
    bad.write_text("{not json")
    out = safety_threshold_report.load_latest(tmp_path)
    assert out["available"] is False
    assert "failed to parse" in (out["reason"] or "")


def test_loader_sorts_all_vertical_first(tmp_path: Path) -> None:
    _write_report(
        tmp_path,
        "safety_threshold_tuning_2026-05-20T000000Z.json",
        extra_verticals={
            "sports": {
                "sharpe_min_ratio": -0.25,
                "ruin_max_probability": 0.01,
                "segment_max_losses": 2,
                "__samples": 200,
            },
            "crypto": {
                "sharpe_min_ratio": None,
                "ruin_max_probability": None,
                "segment_max_losses": None,
                "__samples": 5,
            },
        },
    )
    out = safety_threshold_report.load_latest(tmp_path)
    assert [r["vertical"] for r in out["rows"]] == ["__all__", "crypto", "sports"]
    assert out["rows"][2]["sample_count"] == 200


# ---------------------------------------------------------------------------
# Scheduler / run_after_retrain
# ---------------------------------------------------------------------------


def test_run_after_retrain_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke test the in-process hook: it must produce a fresh report
    file under the configured reports dir and return ok=True."""
    # Point the script's REPORTS_DIR override at a temp dir.
    from scripts import tune_safety_thresholds as tune_mod

    monkeypatch.setattr(tune_mod, "REPORTS_DIR", tmp_path)

    # Patch the trade loader so we don't need a live DB.
    async def _fake_load_trades(_vert: str | None) -> list:
        return []

    monkeypatch.setattr(tune_mod, "_load_trades", _fake_load_trades)

    summary = _run(safety_threshold_tuner.run_after_retrain(reason="unit-test"))
    assert summary["ok"] is True
    assert summary["reason"] == "unit-test"
    assert summary["report_path"] is not None
    written = Path(summary["report_path"])
    assert written.exists()
    payload = json.loads(written.read_text())
    # Always at least the __all__ sweep, even with no trades.
    assert any(s.get("vertical") == "__all__" for s in payload.get("sweeps", []))


def test_run_after_retrain_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep failure must not raise — the caller should see
    ``ok=False`` so the retrain pipeline can continue."""
    from scripts import tune_safety_thresholds as tune_mod

    async def _boom(_vert: str | None) -> list:
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(tune_mod, "_load_trades", _boom)

    summary = _run(safety_threshold_tuner.run_after_retrain(reason="unit-test"))
    assert summary["ok"] is False
    assert "simulated DB outage" in summary["error"]
