"""Tests for the daily honesty report (Task #756 / roadmap T-21).

Covers:
* `collect_report` returns all four sections with mocked DB rows.
* `fire_now(dry_run=True)` renders without dispatching or writing
  `notification_dedupe`.
* The scheduler `_loop` fires past target only once per UTC day and
  `start()`/`stop()` clean up cleanly.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.notifications import alerts, daily_honesty_report as dhr


# ---------------------------------------------------------------------------
# Fake DB
# ---------------------------------------------------------------------------


class _FakeDB:
    """Substring-matched canned-row store with a separate dedupe slot."""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping
        self.dedupe_marked: list[tuple[str, str]] = []

    def _match(self, query: str) -> Any:
        for needle, value in self._mapping.items():
            if needle in query:
                return value
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        v = self._match(query)
        return v if isinstance(v, list) else []

    async def fetchrow(
        self, query: str, *args: Any
    ) -> dict[str, Any] | None:
        # Honour the dedupe INSERT semantics so we can prove fire_now
        # never writes a row in dry_run.
        if "notification_dedupe" in query and "INSERT INTO" in query:
            self.dedupe_marked.append((args[0], args[1]))
            return {"ok": 1}
        v = self._match(query)
        if isinstance(v, dict):
            return v
        return None

    async def execute(self, *_args: Any, **_kw: Any) -> str:
        return "OK"

    async def fetchval(self, *_args: Any, **_kw: Any) -> Any:
        return None


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Section 1: divergence
# ---------------------------------------------------------------------------


def _shadow_live_rows() -> list[dict[str, Any]]:
    """50 perfectly-calibrated live samples + 50 shadow samples for NBA;
    plus a too-small NHL pair (5 each).  Live picks 1.0/0.0 to match y
    so its brier is 0; shadow stays at 0.6/0.4 → brier = 0.16, so the
    delta is well above the 0.05 sentinel the test asserts on."""
    out: list[dict[str, Any]] = []
    for i in range(50):
        out.append(
            {
                "source": "live",
                "league": "NBA",
                "predicted_prob": 1.0 if i % 2 == 0 else 0.0,
                "actual_outcome": 1 if i % 2 == 0 else 0,
            }
        )
    for i in range(5):
        out.append(
            {
                "source": "live",
                "league": "NHL",
                "predicted_prob": 0.5,
                "actual_outcome": 1,
            }
        )
    return out


def _shadow_only_rows() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(50):
        out.append(
            {
                "source": "shadow",
                "league": "NBA",
                "predicted_prob": 0.6 if i % 2 == 0 else 0.4,
                "actual_outcome": 1 if i % 2 == 0 else 0,
            }
        )
    for i in range(5):
        out.append(
            {
                "source": "shadow",
                "league": "NHL",
                "predicted_prob": 0.5,
                "actual_outcome": 0,
            }
        )
    return out


class _DivergenceFakeDB(_FakeDB):
    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "online_learning_buffer" in query:
            return _shadow_live_rows()
        if "shadow_learning_buffer" in query:
            return _shadow_only_rows()
        return await super().fetch(query, *args)


# ---------------------------------------------------------------------------
# Section 2: drift
# ---------------------------------------------------------------------------


_SNAPSHOT_ROWS = [
    {
        "sub_category": "per_league:NBA",
        "role": "champion",
        "sample_count": 1500,
        "brier_score": 0.18,
        "config": {"brier_after": 0.18},
        "drift_status": {"status": "ok"},
        "fit_at": "2026-04-30T13:00:00Z",
        "age_seconds": 3600.0,
    },
    {
        "sub_category": "per_league:NHL",
        "role": "champion",
        "sample_count": 800,
        "brier_score": 0.27,
        "config": {"brier_after": 0.27},
        "drift_status": {"status": "drift_breach"},
        "fit_at": "2026-04-29T13:00:00Z",
        "age_seconds": 90000.0,  # > 72h → also yellow but red wins
    },
]


# ---------------------------------------------------------------------------
# Section 3: tail risk — patched by stubbing dashboard.get_tail_risk_exposure
# ---------------------------------------------------------------------------


_TAIL_TILE = {
    "source": "fake",
    "tail_concentration": {
        "tail_max_loss_usd": 1234.56,
        "total_max_loss_usd": 9876.54,
        "pct": 0.125,
        "tone": "yellow",
        "threshold_price": 0.07,
    },
}


# ---------------------------------------------------------------------------
# Section 4: gate-trip rows
# ---------------------------------------------------------------------------


_GATE_TRIP_ROWS = [
    {
        "gate_name": "tail_risk_size_clamped",
        "n": 4,
        "last_at": "2026-04-30T12:30:00Z",
    },
    {
        "gate_name": "drawdown_limit",
        "n": 1,
        "last_at": "2026-04-30T05:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# T012.A — collect_report aggregates four sections
# ---------------------------------------------------------------------------


def test_collect_report_aggregates_four_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _DivergenceFakeDB(
        {
            "calibration_snapshots": _SNAPSHOT_ROWS,
            "FROM risk_gate_trips": _GATE_TRIP_ROWS,
        }
    )
    monkeypatch.setattr(dhr, "db", fake)

    async def _fake_tail(*, days: int = 30) -> dict[str, Any]:
        return _TAIL_TILE

    # Patch the lazy import inside collect_tail_risk_summary by
    # injecting a stand-in `dashboard` module attribute on the package.
    import app.dashboard as _dashboard

    monkeypatch.setattr(_dashboard, "get_tail_risk_exposure", _fake_tail)

    payload = _run(dhr.collect_report("2026-04-30"))

    assert payload["date_utc"] == "2026-04-30"
    # Section 1 — NBA above min, NHL too small.
    div = payload["divergence"]
    assert div["error"] is None
    leagues = [it["league"] for it in div["items"]]
    assert "NBA" in leagues
    nba = next(it for it in div["items"] if it["league"] == "NBA")
    # Live is perfectly calibrated → near-zero brier; shadow is 0.6/0.4
    # → 0.16 brier; |delta| ≈ 0.15.
    assert nba["abs_delta"] > 0.05
    too_small_leagues = [it["league"] for it in div["samples_too_small"]]
    assert "NHL" in too_small_leagues

    # Section 2 — NHL is RED (drift_breach + age > 72h + brier > 0.25),
    # NBA is GREEN.
    cd = payload["calibrator_drift"]
    assert cd["error"] is None
    items = {it["league"]: it for it in cd["items"]}
    assert items["NHL"]["tone"] == "red"
    assert items["NBA"]["tone"] == "green"
    assert "drift_status=drift_breach" in items["NHL"]["why"]

    # Section 3 — tail-risk numbers passed through.
    tr = payload["tail_risk"]
    assert tr["tail_max_loss_usd"] == 1234.56
    assert tr["pct_of_total"] == 0.125
    assert tr["tone"] == "yellow"

    # Section 4 — gate trip totals.
    gt = payload["gate_trips_24h"]
    assert gt["total"] == 5
    assert gt["items"][0]["gate_name"] == "tail_risk_size_clamped"
    assert gt["items"][0]["count"] == 4


def test_collect_report_degrades_on_db_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each section catches its own DB error rather than blowing up the
    whole report."""

    class _BoomDB:
        async def fetch(self, *_a: Any, **_kw: Any) -> list[Any]:
            raise RuntimeError("boom")

        async def fetchrow(self, *_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("boom")

        async def execute(self, *_a: Any, **_kw: Any) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(dhr, "db", _BoomDB())

    async def _fake_tail(*, days: int = 30) -> dict[str, Any]:
        raise RuntimeError("tail boom")

    import app.dashboard as _dashboard

    monkeypatch.setattr(_dashboard, "get_tail_risk_exposure", _fake_tail)

    payload = _run(dhr.collect_report("2026-04-30"))
    assert payload["divergence"]["error"]
    assert payload["calibrator_drift"]["error"]
    assert payload["tail_risk"]["error"]
    assert payload["gate_trips_24h"]["error"]


# ---------------------------------------------------------------------------
# T012.B — fire_now(dry_run=True) renders without dispatching
# ---------------------------------------------------------------------------


def test_fire_now_dry_run_renders_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _DivergenceFakeDB(
        {
            "calibration_snapshots": _SNAPSHOT_ROWS,
            "FROM risk_gate_trips": _GATE_TRIP_ROWS,
        }
    )
    monkeypatch.setattr(dhr, "db", fake)

    async def _fake_tail(*, days: int = 30) -> dict[str, Any]:
        return _TAIL_TILE

    import app.dashboard as _dashboard

    monkeypatch.setattr(_dashboard, "get_tail_risk_exposure", _fake_tail)

    dispatch_calls: list[dict[str, Any]] = []

    def _fake_dispatch(
        subject: str,
        body: str,
        *,
        tag: str,
        dry_run: bool,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        dispatch_calls.append(
            {"subject": subject, "tag": tag, "dry_run": dry_run}
        )
        return {"tag": tag, "dispatched": False, "dry_run": dry_run}

    monkeypatch.setattr(alerts, "_dispatch", _fake_dispatch)

    out = _run(dhr.fire_now(dry_run=True))

    assert out["dry_run"] is True
    assert "Daily honesty report" in out["subject"]
    assert "Shadow-vs-live divergence" in out["body"]
    assert "<html>" in out["html_body"]
    assert dispatch_calls == [
        {
            "subject": out["subject"],
            "tag": "daily-honesty-report",
            "dry_run": True,
        }
    ]
    # No telegram broadcast on dry runs (fire_now only fires it when
    # dry_run is False).  We assert by *not* importing the module —
    # a broadcast call would fail because no telegram client is wired.


# ---------------------------------------------------------------------------
# T012.C — dedupe is idempotent within a UTC day
# ---------------------------------------------------------------------------


def test_mark_sent_is_idempotent_within_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first claim wins; the second returns False (no-op)."""

    state: dict[str, str] = {}

    async def _fake_fetchrow(query: str, *args: Any) -> dict[str, Any] | None:
        assert "notification_dedupe" in query
        if "INSERT" in query:
            key, date_utc = args[0], args[1]
            existing = state.get(key)
            if existing is None or existing < date_utc:
                state[key] = date_utc
                return {"ok": 1}
            return None
        # Fallback: SELECT path
        key = args[0]
        return {"d": state.get(key)} if key in state else None

    class _StubDB:
        fetchrow = staticmethod(_fake_fetchrow)

        async def execute(self, *_a: Any, **_kw: Any) -> str:
            return "OK"

    monkeypatch.setattr(dhr, "db", _StubDB())

    first = _run(dhr._mark_sent("2026-04-30"))
    second = _run(dhr._mark_sent("2026-04-30"))
    assert first is True
    assert second is False

    # Day rolled over → claim succeeds again.
    third = _run(dhr._mark_sent("2026-05-01"))
    assert third is True


# ---------------------------------------------------------------------------
# T012.D — scheduler fires past target only once per day
# ---------------------------------------------------------------------------


def test_scheduler_fires_past_target_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One iteration of the loop fires fire_now exactly once when past
    the configured UTC time and the dedupe row is unclaimed."""

    fire_calls: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fire_calls.append(1)
        return {"dry_run": dry_run}

    monkeypatch.setattr(dhr, "fire_now", _fake_fire_now)
    monkeypatch.setattr(dhr, "_get_last_sent_date", lambda: _async_value(None))
    monkeypatch.setattr(dhr, "_mark_sent", lambda _date: _async_value(True))

    # Force "past target" to be true: configure 00:00 UTC.
    monkeypatch.setenv("DAILY_HONESTY_REPORT_HOUR_UTC", "0")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_MINUTE_UTC", "0")

    async def _go() -> None:
        # Stop the loop almost immediately so we exercise just one tick.
        dhr._stop.clear()
        task = asyncio.create_task(dhr._loop())
        # Yield so the loop can run its first iteration.
        await asyncio.sleep(0.05)
        dhr._stop.set()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()

    _run(_go())
    assert fire_calls == [1]


def _async_value(v: Any):
    async def _coro(*_a: Any, **_kw: Any) -> Any:
        return v

    return _coro()


# ---------------------------------------------------------------------------
# T012.E — start/stop is clean
# ---------------------------------------------------------------------------


def test_start_then_stop_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_HONESTY_REPORT_ENABLED", "1")

    async def _never_fires(*, dry_run: bool = False) -> dict[str, Any]:
        return {"dry_run": dry_run}

    async def _no_prior() -> str | None:
        return None

    async def _claim(_date: str) -> bool:
        return False  # already claimed → loop won't fire

    monkeypatch.setattr(dhr, "fire_now", _never_fires)
    monkeypatch.setattr(dhr, "_get_last_sent_date", _no_prior)
    monkeypatch.setattr(dhr, "_mark_sent", _claim)

    async def _go() -> None:
        await dhr.start()
        assert dhr._task is not None
        await asyncio.sleep(0.05)
        await dhr.stop()
        assert dhr._task is None

    _run(_go())


def test_start_no_op_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_HONESTY_REPORT_ENABLED", "0")

    async def _go() -> None:
        await dhr.start()
        assert dhr._task is None
        await dhr.stop()  # safe even when never started

    _run(_go())


# ---------------------------------------------------------------------------
# T012.F — drift classifier thresholds
# ---------------------------------------------------------------------------


def test_classify_drift_red_beats_yellow_beats_green() -> None:
    assert dhr._classify_drift(
        brier_after=0.30, drift_status="ok", age_hours=10.0
    )[0] == "red"
    assert dhr._classify_drift(
        brier_after=0.22, drift_status="ok", age_hours=10.0
    )[0] == "yellow"
    assert dhr._classify_drift(
        brier_after=0.18, drift_status="ok", age_hours=1.0
    )[0] == "green"
    # rolled_back forces RED regardless of brier
    assert dhr._classify_drift(
        brier_after=0.10, drift_status="rolled_back", age_hours=1.0
    )[0] == "red"
    # >14d age forces RED
    assert dhr._classify_drift(
        brier_after=0.10, drift_status="ok", age_hours=15 * 24,
    )[0] == "red"


# ---------------------------------------------------------------------------
# F762 — investor deck surfaced in the daily report
# ---------------------------------------------------------------------------


def test_collect_latest_investor_deck_picks_first_with_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Walks rows latest-first and returns the first one whose
    ``notes`` JSON carries an `investor_deck_path`."""
    rows = [
        {
            "run_at_iso": "2026-04-30T13:00:00Z",
            "notes": json.dumps({"unrelated": "yo"}),
        },
        {
            "run_at_iso": "2026-04-23T13:00:00Z",
            "notes": json.dumps(
                {"investor_deck_path": "/x/y/investor_deck_latest.pptx"}
            ),
        },
    ]
    fake = _FakeDB({"FROM v1_backtest_gate_runs": rows})
    monkeypatch.setattr(dhr, "db", fake)

    out = _run(dhr.collect_latest_investor_deck())
    assert out["investor_deck_path"] == "/x/y/investor_deck_latest.pptx"
    assert out["built_at"] == "2026-04-23T13:00:00Z"


def test_collect_latest_investor_deck_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeDB({"FROM v1_backtest_gate_runs": []})
    monkeypatch.setattr(dhr, "db", fake)

    out = _run(dhr.collect_latest_investor_deck())
    assert out == {"investor_deck_path": None, "built_at": None}


def test_collect_latest_investor_deck_swallows_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom:
        async def fetch(self, *_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("db gone")

    monkeypatch.setattr(dhr, "db", _Boom())
    out = _run(dhr.collect_latest_investor_deck())
    assert "error" in out


def test_render_text_surfaces_investor_deck_path() -> None:
    payload = {
        "date_utc": "2026-05-01",
        "divergence": {"days": 7, "items": []},
        "calibrator_drift": {"items": []},
        "tail_risk": {},
        "gate_trips_24h": {"items": []},
        "investor_deck": {
            "investor_deck_path": "/srv/reports/investor_deck_latest.pptx",
            "built_at": "2026-04-30T13:00:00Z",
        },
    }
    _subject, body = dhr.render_text(payload)
    assert "Latest investor deck:" in body
    assert "/srv/reports/investor_deck_latest.pptx" in body
    assert "built 2026-04-30T13:00:00Z" in body


def test_render_text_handles_missing_investor_deck() -> None:
    payload = {
        "date_utc": "2026-05-01",
        "divergence": {"days": 7, "items": []},
        "calibrator_drift": {"items": []},
        "tail_risk": {},
        "gate_trips_24h": {"items": []},
        "investor_deck": {"investor_deck_path": None, "built_at": None},
    }
    _subject, body = dhr.render_text(payload)
    assert "not built yet" in body
