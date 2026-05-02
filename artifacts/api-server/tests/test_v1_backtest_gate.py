"""Tests for the weekly V1 backtest gate (Task #756 / roadmap T-22).

Covers:
* `detect_regressions` — better, worse, and no-change deltas.
* `run_weekly_v1_backtest` — happy-path persistence row shape.
* `dashboard.get_v1_backtest_gate` — `latest + history` slice shape.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        daily_report_sent_at_iso: str | None = None,
        raise_on_dedupe: bool = False,
    ) -> None:
        self._rows = rows
        self._daily = daily_report_sent_at_iso
        self._raise_on_dedupe = raise_on_dedupe

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM v1_backtest_gate_runs" in query:
            limit = int(args[0]) if args else 8
            return self._rows[:limit]
        return []

    async def fetchrow(
        self, query: str, *args: Any
    ) -> dict[str, Any] | None:
        if "FROM notification_dedupe" in query:
            if self._raise_on_dedupe:
                raise RuntimeError("dedupe table missing")
            if self._daily is None:
                return None
            from datetime import datetime as _dt, timezone as _tz

            return {
                "sent_at_iso": self._daily,
                "sent_at": _dt.fromisoformat(
                    self._daily.rstrip("Z")
                ).replace(tzinfo=_tz.utc),
            }
        return None


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
    assert out["latest"] is None
    assert out["history"] == []
    # F761 — freshness present even with an empty table.
    assert out["freshness"]["v1_backtest"]["status"] == "missing"
    assert out["freshness"]["daily_report"]["status"] == "missing"


def test_get_v1_backtest_gate_clamps_history_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-range limits get clamped to [1, 50]."""

    captured: dict[str, Any] = {}

    class _Capture:
        async def fetch(self, _q: str, *args: Any) -> list[Any]:
            captured["limit"] = args[0] if args else None
            return []

        async def fetchrow(self, _q: str, *_a: Any) -> Any:
            return None

    monkeypatch.setattr(dashboard, "db", _Capture())
    _run(dashboard.get_v1_backtest_gate(history_limit=999))
    assert captured["limit"] == 50
    _run(dashboard.get_v1_backtest_gate(history_limit=0))
    assert captured["limit"] == 8  # falsy → default


# ---------------------------------------------------------------------------
# F761 — freshness block on get_v1_backtest_gate()
# ---------------------------------------------------------------------------


def _iso_ago(seconds: float) -> str:
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    t = _dt.now(_tz.utc) - _td(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(run_at_iso: str) -> dict[str, Any]:
    return {
        "id": 1,
        "run_at": run_at_iso,
        "overall_verdict": "PASS",
        "parity_gate_status": json.dumps({"overall": "PASS"}),
        "per_league_brier": json.dumps({"NBA": 0.18}),
        "regressed_leagues": [],
        "report_path": None,
        "duration_seconds": 11.0,
        "notes": None,
    }


def test_freshness_v1_backtest_just_inside_threshold_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At exactly 9 days old the V1 backtest is still `fresh`."""
    nine_days = 9 * 24 * 3600 - 5  # tiny buffer to dodge race
    monkeypatch.setattr(
        dashboard,
        "db",
        _DashboardFakeDB([_row(_iso_ago(nine_days))]),
    )
    out = _run(dashboard.get_v1_backtest_gate(history_limit=8))
    assert out["freshness"]["v1_backtest"]["status"] == "fresh"


def test_freshness_v1_backtest_past_threshold_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One second past 9 days flips to `stale`."""
    over = 9 * 24 * 3600 + 60
    monkeypatch.setattr(
        dashboard,
        "db",
        _DashboardFakeDB([_row(_iso_ago(over))]),
    )
    out = _run(dashboard.get_v1_backtest_gate(history_limit=8))
    fresh = out["freshness"]["v1_backtest"]
    assert fresh["status"] == "stale"
    assert fresh["age_seconds"] is not None and fresh["age_seconds"] >= over


def test_freshness_daily_report_just_inside_threshold_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At exactly 36h since last dispatch the daily report is `fresh`."""
    monkeypatch.setattr(
        dashboard,
        "db",
        _DashboardFakeDB(
            [_row(_iso_ago(60))],
            daily_report_sent_at_iso=_iso_ago(36 * 3600 - 30),
        ),
    )
    out = _run(dashboard.get_v1_backtest_gate(history_limit=8))
    assert out["freshness"]["daily_report"]["status"] == "fresh"


def test_freshness_daily_report_past_threshold_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dashboard,
        "db",
        _DashboardFakeDB(
            [_row(_iso_ago(60))],
            daily_report_sent_at_iso=_iso_ago(36 * 3600 + 600),
        ),
    )
    out = _run(dashboard.get_v1_backtest_gate(history_limit=8))
    assert out["freshness"]["daily_report"]["status"] == "stale"


def test_freshness_daily_report_missing_when_no_dedupe_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dashboard,
        "db",
        _DashboardFakeDB(
            [_row(_iso_ago(60))],
            daily_report_sent_at_iso=None,
        ),
    )
    out = _run(dashboard.get_v1_backtest_gate(history_limit=8))
    assert out["freshness"]["daily_report"]["status"] == "missing"
    assert out["freshness"]["daily_report"]["last_sent_at"] is None


def test_freshness_daily_report_swallows_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing dedupe-table query degrades to `missing`, never raises."""
    monkeypatch.setattr(
        dashboard,
        "db",
        _DashboardFakeDB([_row(_iso_ago(60))], raise_on_dedupe=True),
    )
    out = _run(dashboard.get_v1_backtest_gate(history_limit=8))
    assert out["freshness"]["daily_report"]["status"] == "missing"
    assert out["freshness"]["daily_report"].get("error")


# ---------------------------------------------------------------------------
# F762 — auto-build investor deck after weekly cron
# ---------------------------------------------------------------------------


def test_run_weekly_v1_backtest_auto_builds_investor_deck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a JSON report is written, the cron auto-runs `build_deck()`
    in a worker thread and stamps the resulting PPTX path into the
    persisted row's ``notes`` field as JSON."""
    fake = _GateFakeDB(prior_row=None)
    monkeypatch.setattr(gate, "db", fake)

    async def _stub_run(**_kw: Any) -> dict[str, Any]:
        return _stub_audit_report({"NBA": 0.18})

    monkeypatch.setattr(
        "scripts.backtest_v1_archive.run", _stub_run, raising=False
    )

    # Capture the build_deck call without actually invoking python-pptx.
    captured: dict[str, Any] = {}

    async def _patched_to_thread(_func, *_args, **kwargs):
        captured["output_path"] = kwargs.get("output_path")
        captured["report_path"] = kwargs.get("report_path")
        out = kwargs.get("output_path")
        if out is not None:
            out.write_bytes(b"PPTX-stub")
        return out

    monkeypatch.setattr(gate.asyncio, "to_thread", _patched_to_thread)
    monkeypatch.setattr(
        gate, "_write_json_enabled", lambda: True, raising=False
    )

    out = _run(gate.run_weekly_v1_backtest(limit=100))

    assert out["report_path"] is not None
    assert out["investor_deck_path"] is not None
    assert out["investor_deck_path"].endswith("investor_deck_latest.pptx")
    # Deck output sits next to the JSON report.
    assert captured["output_path"] == Path(out["investor_deck_path"])
    assert captured["report_path"] == Path(out["report_path"])

    # Persisted notes carry the deck path as JSON for the daily report
    # (`collect_latest_investor_deck`) to surface.
    assert len(fake.rows) == 1
    notes_raw = fake.rows[0]["notes"]
    assert notes_raw is not None
    decoded = json.loads(notes_raw)
    assert decoded["investor_deck_path"] == out["investor_deck_path"]


def test_run_weekly_v1_backtest_deck_failure_does_not_break_cron(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `build_deck()` blows up the cron still persists the gate row."""
    fake = _GateFakeDB(prior_row=None)
    monkeypatch.setattr(gate, "db", fake)

    async def _stub_run(**_kw: Any) -> dict[str, Any]:
        return _stub_audit_report({"NBA": 0.18})

    monkeypatch.setattr(
        "scripts.backtest_v1_archive.run", _stub_run, raising=False
    )

    async def _boom(_func, *_a, **_kw):
        raise RuntimeError("python-pptx exploded")

    monkeypatch.setattr(gate.asyncio, "to_thread", _boom)

    out = _run(gate.run_weekly_v1_backtest(limit=100))
    assert out["overall_verdict"] == "PASS"
    assert out["investor_deck_path"] is None
    # Row is still written; notes stays None when no deck path.
    assert len(fake.rows) == 1
    assert fake.rows[0]["notes"] is None


def test_run_weekly_v1_backtest_skips_deck_when_no_report_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `write_json=True` no deck is built — there is no input."""
    fake = _GateFakeDB(prior_row=None)
    monkeypatch.setattr(gate, "db", fake)

    async def _stub_run(**_kw: Any) -> dict[str, Any]:
        return _stub_audit_report({"NBA": 0.18})

    monkeypatch.setattr(
        "scripts.backtest_v1_archive.run", _stub_run, raising=False
    )

    called: list[bool] = []

    async def _patched_to_thread(_func, *_a, **_kw):
        called.append(True)
        return None

    monkeypatch.setattr(gate.asyncio, "to_thread", _patched_to_thread)

    out = _run(gate.run_weekly_v1_backtest(limit=100, write_json=False))
    assert out["report_path"] is None
    assert out["investor_deck_path"] is None
    assert called == []
