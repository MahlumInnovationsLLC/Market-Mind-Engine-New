"""Tests for the weekly V1 backtest gate (Task #756 / roadmap T-22).

Covers:
* `detect_regressions` — better, worse, and no-change deltas.
* `run_weekly_v1_backtest` — happy-path persistence row shape.
* `dashboard.get_v1_backtest_gate` — `latest + history` slice shape.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app import dashboard
from app.notifications import v1_backtest_gate as gate


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# detect_regressions
# ---------------------------------------------------------------------------


def test_detect_regressions_better_unchanged_worse() -> None:
    prior = {"NBA": 0.20, "NHL": 0.18, "MLB": 0.22}
    current = {
        "NBA": 0.10,   # better — must NOT regress
        "NHL": 0.18,   # unchanged — must NOT regress
        "MLB": 0.30,   # worse by 0.08 — REGRESSION
    }
    out = gate.detect_regressions(current=current, prior=prior, tolerance=0.02)
    assert [r["league"] for r in out] == ["MLB"]
    assert out[0]["delta"] == pytest.approx(0.08)
    assert out[0]["prior"] == pytest.approx(0.22)
    assert out[0]["current"] == pytest.approx(0.30)


def test_detect_regressions_just_inside_tolerance_passes() -> None:
    prior = {"NBA": 0.20}
    current = {"NBA": 0.219}  # delta = 0.019 < 0.02 tolerance
    out = gate.detect_regressions(current=current, prior=prior, tolerance=0.02)
    assert out == []


def test_detect_regressions_no_prior_returns_empty() -> None:
    out = gate.detect_regressions(
        current={"NBA": 0.5}, prior=None, tolerance=0.02
    )
    assert out == []


def test_detect_regressions_skips_leagues_missing_from_prior() -> None:
    """A new league has no baseline yet, so it cannot regress."""
    prior = {"NBA": 0.2}
    current = {"NBA": 0.20, "NEW_LEAGUE": 0.99}
    out = gate.detect_regressions(current=current, prior=prior, tolerance=0.02)
    assert out == []


def test_per_league_brier_falls_back_to_v1_raw() -> None:
    report = {
        "per_league": {
            "NBA": {"brier_calibrated": 0.18},
            "NHL": {"brier_calibrated": None, "brier_v1_raw": 0.22},
            "MLB": {},  # neither — skipped
            "JUNK": "not-a-dict",
        }
    }
    out = gate._per_league_brier(report)
    assert out == {"NBA": 0.18, "NHL": 0.22}


# ---------------------------------------------------------------------------
# run_weekly_v1_backtest — happy path with the offline script stubbed
# ---------------------------------------------------------------------------


class _GateFakeDB:
    """In-memory `v1_backtest_gate_runs` table."""

    def __init__(self, prior_row: dict[str, Any] | None = None) -> None:
        self.rows: list[dict[str, Any]] = []
        self._next_id = 1
        self.prior_row = prior_row

    async def fetch(
        self, query: str, *args: Any
    ) -> list[dict[str, Any]]:
        if "FROM v1_backtest_gate_runs" in query:
            limit = int(args[0]) if args else 8
            return list(reversed(self.rows))[:limit]
        return []

    async def fetchrow(
        self, query: str, *args: Any
    ) -> dict[str, Any] | None:
        if "INSERT INTO v1_backtest_gate_runs" in query:
            (
                overall,
                parity_json,
                plb_json,
                regressed,
                report_path,
                duration,
                notes,
            ) = args
            row = {
                "id": self._next_id,
                "run_at": "2026-04-30T13:00:00Z",
                "overall_verdict": overall,
                "parity_gate_status": json.loads(parity_json),
                "per_league_brier": json.loads(plb_json),
                "regressed_leagues": list(regressed),
                "report_path": report_path,
                "duration_seconds": float(duration),
                "notes": notes,
            }
            self._next_id += 1
            self.rows.append(row)
            return {"id": row["id"]}
        if "FROM v1_backtest_gate_runs" in query:
            return self.prior_row
        return None

    async def execute(self, *_a: Any, **_kw: Any) -> str:
        return "OK"


def _stub_audit_report(per_league_brier: dict[str, float]) -> dict[str, Any]:
    return {
        "started_at": "2026-04-30T13:00:00Z",
        "completed_at": "2026-04-30T13:00:30Z",
        "row_input_count": 50000,
        "row_kept_count": 4900,
        "per_league": {
            lg: {
                "brier_calibrated": b,
                "brier_v1_raw": b + 0.01,
                "verdict": "PARITY",
                "calibrator_type": "isotonic",
                "sample_count": 1000,
                "brier_delta": -0.01,
            }
            for lg, b in per_league_brier.items()
        },
        "parity_gate_status": {
            "overall": "PASS",
            "summary": {"pass": 3, "fail": 0},
            "gates": {
                "archived_features_replay": {"status": "PASS"},
                "surrogate_features_replay": {"status": "PASS"},
                "two_pp_parity_gate": {"status": "PASS"},
            },
        },
    }


def test_run_weekly_v1_backtest_persists_with_no_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First run ever — no prior row, regressed_leagues must be []."""
    fake = _GateFakeDB(prior_row=None)
    monkeypatch.setattr(gate, "db", fake)

    async def _stub_run(**_kw: Any) -> dict[str, Any]:
        return _stub_audit_report({"NBA": 0.18, "NHL": 0.22})

    monkeypatch.setattr(
        "scripts.backtest_v1_archive.run", _stub_run, raising=False
    )

    out = _run(gate.run_weekly_v1_backtest(limit=100, write_json=False))

    assert out["overall_verdict"] == "PASS"
    assert out["regressed_leagues"] == []
    assert out["per_league_brier"] == {"NBA": 0.18, "NHL": 0.22}
    assert out["id"] == 1
    # Persisted row mirrors the return shape.
    assert len(fake.rows) == 1
    row = fake.rows[0]
    assert row["overall_verdict"] == "PASS"
    assert row["per_league_brier"] == {"NBA": 0.18, "NHL": 0.22}
    assert row["regressed_leagues"] == []


def test_run_weekly_v1_backtest_detects_regression_vs_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prior row says NHL=0.18, current run says NHL=0.30 → regression."""
    prior = {
        "id": 1,
        "run_at": "2026-04-23T13:00:00Z",
        "overall_verdict": "PASS",
        "parity_gate_status": json.dumps({"overall": "PASS"}),
        "per_league_brier": json.dumps({"NBA": 0.18, "NHL": 0.18}),
        "regressed_leagues": [],
        "report_path": None,
        "duration_seconds": 12.5,
    }
    fake = _GateFakeDB(prior_row=prior)
    monkeypatch.setattr(gate, "db", fake)

    async def _stub_run(**_kw: Any) -> dict[str, Any]:
        return _stub_audit_report({"NBA": 0.18, "NHL": 0.30})

    monkeypatch.setattr(
        "scripts.backtest_v1_archive.run", _stub_run, raising=False
    )

    out = _run(gate.run_weekly_v1_backtest(limit=100, write_json=False))
    assert out["regressed_leagues"] == ["NHL"]
    assert out["regression_detail"][0]["league"] == "NHL"
    assert out["regression_detail"][0]["delta"] == pytest.approx(0.12)
    # Persisted row matches what the dashboard tile would render red on.
    assert fake.rows[0]["regressed_leagues"] == ["NHL"]


def test_run_weekly_v1_backtest_handles_replay_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the offline script raises, we return ERROR — never blow up."""
    fake = _GateFakeDB(prior_row=None)
    monkeypatch.setattr(gate, "db", fake)

    async def _stub_run(**_kw: Any) -> dict[str, Any]:
        raise RuntimeError("calibrator load failed")

    monkeypatch.setattr(
        "scripts.backtest_v1_archive.run", _stub_run, raising=False
    )

    out = _run(gate.run_weekly_v1_backtest(limit=10, write_json=False))
    assert out["overall_verdict"] == "ERROR"
    assert "calibrator load failed" in out["error"]
    # An ERROR row MUST be persisted so the weekly history isn't silent
    # about crashed runs (otherwise the dashboard tile keeps showing
    # last week's PASS as if nothing happened).
    assert len(fake.rows) == 1
    err_row = fake.rows[0]
    assert err_row["overall_verdict"] == "ERROR"
    assert err_row["parity_gate_status"]["overall"] == "ERROR"
    assert "calibrator load failed" in err_row["parity_gate_status"]["error"]
    assert err_row["per_league_brier"] == {}
    assert err_row["regressed_leagues"] == []
    assert err_row["report_path"] is None


# ---------------------------------------------------------------------------
# dashboard.get_v1_backtest_gate
# ---------------------------------------------------------------------------


class _DashboardFakeDB:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM v1_backtest_gate_runs" in query:
            limit = int(args[0]) if args else 8
            return self._rows[:limit]
        return []


def test_get_v1_backtest_gate_returns_latest_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "id": 3,
            "run_at": "2026-04-30T13:00:00Z",
            "overall_verdict": "FAIL",
            "parity_gate_status": json.dumps(
                {"overall": "FAIL", "gates": {"x": {"status": "FAIL"}}}
            ),
            "per_league_brier": json.dumps({"NBA": 0.30}),
            "regressed_leagues": ["NBA"],
            "report_path": None,
            "duration_seconds": 12.3,
            "notes": None,
        },
        {
            "id": 2,
            "run_at": "2026-04-23T13:00:00Z",
            "overall_verdict": "PASS",
            "parity_gate_status": json.dumps({"overall": "PASS"}),
            "per_league_brier": json.dumps({"NBA": 0.18}),
            "regressed_leagues": [],
            "report_path": None,
            "duration_seconds": 11.0,
            "notes": None,
        },
    ]
    monkeypatch.setattr(dashboard, "db", _DashboardFakeDB(rows))

    out = _run(dashboard.get_v1_backtest_gate(history_limit=8))
    assert out["latest"]["overall_verdict"] == "FAIL"
    assert out["latest"]["regressed_leagues"] == ["NBA"]
    assert out["latest"]["parity_gate_status"]["overall"] == "FAIL"
    assert len(out["history"]) == 2
    assert out["history"][1]["overall_verdict"] == "PASS"


def test_get_v1_backtest_gate_empty_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard, "db", _DashboardFakeDB([]))
    out = _run(dashboard.get_v1_backtest_gate(history_limit=8))
    assert out == {"latest": None, "history": []}


def test_get_v1_backtest_gate_clamps_history_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-range limits get clamped to [1, 50]."""

    captured: dict[str, Any] = {}

    class _Capture:
        async def fetch(self, _q: str, *args: Any) -> list[Any]:
            captured["limit"] = args[0] if args else None
            return []

    monkeypatch.setattr(dashboard, "db", _Capture())
    _run(dashboard.get_v1_backtest_gate(history_limit=999))
    assert captured["limit"] == 50
    _run(dashboard.get_v1_backtest_gate(history_limit=0))
    assert captured["limit"] == 8  # falsy → default
