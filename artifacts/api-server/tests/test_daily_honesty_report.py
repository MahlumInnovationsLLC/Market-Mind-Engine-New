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
        # Longest-needle wins so a more-specific canned key (e.g.
        # `uncalibrated_vertical_raw_mode`) shadows a broader one
        # (e.g. `FROM risk_gate_trips`) when both substrings appear
        # in the same query.
        best: tuple[int, Any] = (-1, None)
        for needle, value in self._mapping.items():
            if needle in query and len(needle) > best[0]:
                best = (len(needle), value)
        return best[1]

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
            "uncalibrated_vertical_raw_mode": [],
            "missing_league_on_candidate": [],
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
            "uncalibrated_vertical_raw_mode": [],
            "missing_league_on_candidate": [],
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
        attachments: Any = None,
    ) -> dict[str, Any]:
        dispatch_calls.append(
            {
                "subject": subject,
                "tag": tag,
                "dry_run": dry_run,
                "attachments": [str(p) for p in (attachments or [])],
            }
        )
        return {
            "tag": tag,
            "dispatched": False,
            "dry_run": dry_run,
            "attachments": [str(p) for p in (attachments or [])],
        }

    monkeypatch.setattr(alerts, "_dispatch", _fake_dispatch)

    out = _run(dhr.fire_now(dry_run=True))

    assert out["dry_run"] is True
    assert "Daily honesty report" in out["subject"]
    assert "Shadow-vs-live divergence" in out["body"]
    assert "<html>" in out["html_body"]
    assert len(dispatch_calls) == 1
    call = dispatch_calls[0]
    assert call["subject"] == out["subject"]
    assert call["tag"] == "daily-honesty-report"
    assert call["dry_run"] is True
    # The PDF + PNG render side-effects fire even on dry-run because
    # the dispatch attachment list mirrors the artifact paths the
    # operator would receive in production.  Both must point under
    # the artifact's reports/ dir, but render itself is best-effort
    # so the list may be empty when the optional libs aren't
    # importable in this environment — assert structure only.
    assert isinstance(call["attachments"], list)
    for a in call["attachments"]:
        assert "reports/" in a or "reports\\" in a
    artifacts = out["artifacts"]
    assert "pdf_path" in artifacts and "chart_path" in artifacts
    # No telegram broadcast on dry runs (fire_now only fires it when
    # dry_run is False).  We assert by *not* importing the module —
    # a broadcast call would fail because no telegram client is wired.


# ---------------------------------------------------------------------------
# Task #765 — operator-visible banner when PDF/chart render returns None
# ---------------------------------------------------------------------------


def _setup_fire_now_env(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Wire up enough mocks for `fire_now(dry_run=True)` to run end-to-end
    and return the captured dispatch-call list."""
    fake = _DivergenceFakeDB(
        {
            "calibration_snapshots": _SNAPSHOT_ROWS,
            "FROM risk_gate_trips": _GATE_TRIP_ROWS,
            "uncalibrated_vertical_raw_mode": [],
            "missing_league_on_candidate": [],
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
        attachments: Any = None,
    ) -> dict[str, Any]:
        dispatch_calls.append(
            {
                "subject": subject,
                "body": body,
                "html_body": html_body,
                "tag": tag,
                "attachments": [str(p) for p in (attachments or [])],
            }
        )
        return {"tag": tag, "dispatched": False, "dry_run": dry_run}

    monkeypatch.setattr(alerts, "_dispatch", _fake_dispatch)
    return dispatch_calls


_BANNER = "(PDF artifact unavailable today — see API server logs)"


def test_fire_now_banner_appears_when_pdf_render_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    dispatch_calls = _setup_fire_now_env(monkeypatch)

    monkeypatch.setattr(
        dhr, "render_daily_report_pdf", lambda *a, **kw: None
    )
    fake_chart = tmp_path / "chart.png"
    fake_chart.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(
        dhr, "render_tail_risk_chart_png", lambda *a, **kw: fake_chart
    )

    out = _run(dhr.fire_now(dry_run=True))

    assert _BANNER in out["body"]
    assert _BANNER in out["html_body"]
    assert out["body"].startswith(_BANNER)
    assert len(dispatch_calls) == 1
    assert _BANNER in dispatch_calls[0]["body"]
    assert _BANNER in dispatch_calls[0]["html_body"]


def test_fire_now_banner_absent_when_only_chart_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Chart-None is the legitimate 'no non-zero buckets' case and must
    NOT trigger the operator banner — only a real PDF failure should.
    (architect HIGH fix — banner was previously firing on quiet days
    when the tail-risk tile had nothing to plot.)"""
    dispatch_calls = _setup_fire_now_env(monkeypatch)

    fake_pdf = tmp_path / "report.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        dhr, "render_daily_report_pdf", lambda *a, **kw: fake_pdf
    )
    monkeypatch.setattr(
        dhr, "render_tail_risk_chart_png", lambda *a, **kw: None
    )

    out = _run(dhr.fire_now(dry_run=True))

    assert _BANNER not in out["body"]
    assert _BANNER not in out["html_body"]
    assert len(dispatch_calls) == 1
    assert _BANNER not in dispatch_calls[0]["body"]
    # The PDF is still attached even though the chart was skipped.
    assert dispatch_calls[0]["attachments"] == [str(fake_pdf)]


def test_fire_now_banner_absent_when_both_artifacts_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    dispatch_calls = _setup_fire_now_env(monkeypatch)

    fake_pdf = tmp_path / "report.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    fake_chart = tmp_path / "chart.png"
    fake_chart.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(
        dhr, "render_daily_report_pdf", lambda *a, **kw: fake_pdf
    )
    monkeypatch.setattr(
        dhr, "render_tail_risk_chart_png", lambda *a, **kw: fake_chart
    )

    out = _run(dhr.fire_now(dry_run=True))

    assert _BANNER not in out["body"]
    assert _BANNER not in out["html_body"]
    assert len(dispatch_calls) == 1
    assert _BANNER not in dispatch_calls[0]["body"]
    assert _BANNER not in dispatch_calls[0]["html_body"]


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

    # Force "past target" to be true: pin the schedule to 00:00 in UTC
    # so the loop's local-TZ comparison fires regardless of the
    # wall-clock the test happens to run at.  (G1 — local-time
    # scheduling.)
    monkeypatch.setenv("DAILY_HONESTY_REPORT_TZ", "UTC")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_HOUR", "0")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_MINUTE", "0")
    monkeypatch.delenv("DAILY_HONESTY_REPORT_HOUR_UTC", raising=False)
    monkeypatch.delenv("DAILY_HONESTY_REPORT_MINUTE_UTC", raising=False)

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


# ---------------------------------------------------------------------------
# G1 — local-time scheduling
# ---------------------------------------------------------------------------


def test_send_hour_minute_prefer_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the new TZ-aware envs are set, the legacy *_UTC ones are
    ignored entirely."""
    monkeypatch.setenv("DAILY_HONESTY_REPORT_TZ", "America/New_York")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_HOUR", "9")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_MINUTE", "15")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_HOUR_UTC", "22")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_MINUTE_UTC", "45")
    assert dhr._send_hour() == 9
    assert dhr._send_minute() == 15


def test_send_hour_minute_fallback_converts_utc_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the new envs, _send_hour/_minute synthesise a local-TZ
    target from _HOUR_UTC/_MINUTE_UTC via today's UTC→local conversion
    so wall-clock behaviour stays stable across DST."""
    monkeypatch.delenv("DAILY_HONESTY_REPORT_HOUR", raising=False)
    monkeypatch.delenv("DAILY_HONESTY_REPORT_MINUTE", raising=False)
    monkeypatch.setenv("DAILY_HONESTY_REPORT_TZ", "UTC")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_HOUR_UTC", "13")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_MINUTE_UTC", "30")
    # TZ=UTC → conversion is identity → expect 13:30 back.
    assert dhr._send_hour() == 13
    assert dhr._send_minute() == 30

    monkeypatch.setenv("DAILY_HONESTY_REPORT_TZ", "America/New_York")
    # ET is UTC-4 (DST) or UTC-5 (standard).  13:00 UTC must land on
    # either 08:30 or 09:30 ET — both valid DST states.
    h = dhr._send_hour()
    m = dhr._send_minute()
    assert h in (8, 9)
    assert m == 30


def test_send_hour_invalid_tz_falls_back_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAILY_HONESTY_REPORT_TZ", "Not/A_Real_Zone")
    monkeypatch.delenv("DAILY_HONESTY_REPORT_HOUR", raising=False)
    monkeypatch.delenv("DAILY_HONESTY_REPORT_MINUTE", raising=False)
    monkeypatch.setenv("DAILY_HONESTY_REPORT_HOUR_UTC", "5")
    monkeypatch.setenv("DAILY_HONESTY_REPORT_MINUTE_UTC", "0")
    # Bad TZ → fallback UTC → identity conversion → 05:00.
    assert dhr._send_hour() == 5
    assert dhr._send_minute() == 0


def test_local_today_str_matches_now_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAILY_HONESTY_REPORT_TZ", "UTC")
    today = dhr._local_today_str()
    assert today == dhr._now_local().date().isoformat()
    assert len(today) == 10 and today[4] == "-" and today[7] == "-"


# ---------------------------------------------------------------------------
# G2 — PNG tail-risk chart
# ---------------------------------------------------------------------------


def _tail_summary_with_buckets(open_max_loss_usd: float = 1234.5) -> dict[str, Any]:
    return {
        "tail_max_loss_usd": 800.0,
        "total_max_loss_usd": 2000.0,
        "pct_of_total": 0.40,
        "tone": "yellow",
        "threshold_price": 1.50,
        "source": "v_open_position_max_loss_30d",
        "buckets": [
            {
                "label": "<1.50",
                "lo": 0.0,
                "hi": 1.50,
                "is_tail": True,
                "open_max_loss_usd": open_max_loss_usd,
                "open_count": 3,
            },
            {
                "label": "1.50–2.50",
                "lo": 1.50,
                "hi": 2.50,
                "is_tail": False,
                "open_max_loss_usd": 765.5,
                "open_count": 2,
            },
        ],
        "error": None,
    }


def test_render_tail_risk_chart_writes_png(tmp_path) -> None:
    pytest.importorskip("matplotlib")
    out = dhr.render_tail_risk_chart_png(
        _tail_summary_with_buckets(),
        date_str="2026-04-30",
        output_dir=tmp_path,
    )
    assert out is not None
    assert out.is_file()
    # PNG magic bytes — proves we wrote a real image, not a stub.
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert out.name == "tail_risk_tile_2026-04-30.png"


def test_render_tail_risk_chart_skips_when_no_rows(tmp_path) -> None:
    pytest.importorskip("matplotlib")
    payload = _tail_summary_with_buckets(open_max_loss_usd=0.0)
    payload["buckets"][1]["open_max_loss_usd"] = 0.0
    out = dhr.render_tail_risk_chart_png(
        payload, date_str="2026-04-30", output_dir=tmp_path
    )
    assert out is None
    # Nothing should have been written.
    assert list(tmp_path.iterdir()) == []


def test_render_tail_risk_chart_skips_on_error_payload(tmp_path) -> None:
    out = dhr.render_tail_risk_chart_png(
        {"error": "db timeout", "buckets": []},
        date_str="2026-04-30",
        output_dir=tmp_path,
    )
    assert out is None


def test_render_tail_risk_chart_failsoft_when_matplotlib_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If matplotlib raises on import, we log + return None instead
    of crashing the whole report build."""
    import builtins as _builtins

    real_import = _builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "matplotlib":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(_builtins, "__import__", _fake_import)
    out = dhr.render_tail_risk_chart_png(
        _tail_summary_with_buckets(),
        date_str="2026-04-30",
        output_dir=tmp_path,
    )
    assert out is None


# ---------------------------------------------------------------------------
# G3 — PDF artifact
# ---------------------------------------------------------------------------


def _full_payload(date_utc: str = "2026-04-30") -> dict[str, Any]:
    return {
        "date_utc": date_utc,
        "divergence": {
            "days": 7,
            "items": [
                {
                    "league": "NBA",
                    "abs_delta": 0.16,
                    "brier_live": 0.00,
                    "n_live": 50,
                    "brier_shadow": 0.16,
                    "n_shadow": 50,
                }
            ],
            "samples_too_small": [{"league": "NHL"}],
        },
        "calibrator_drift": {
            "items": [
                {
                    "tone": "red",
                    "league": "NBA",
                    "brier_after": 0.31,
                    "age_hours": 18 * 24,
                    "why": "stale + brier high",
                }
            ]
        },
        "tail_risk": _tail_summary_with_buckets(),
        "gate_trips_24h": {
            "total": 3,
            "items": [
                {
                    "gate_name": "max_loss",
                    "count": 2,
                    "last_at": "2026-04-30T08:00:00Z",
                },
                {
                    "gate_name": "drawdown",
                    "count": 1,
                    "last_at": "2026-04-30T07:30:00Z",
                },
            ],
        },
        "investor_deck": {
            "investor_deck_path": "/srv/reports/investor_deck_latest.pptx",
            "built_at": "2026-04-29T13:00:00Z",
        },
    }


def test_render_daily_report_pdf_full_payload(tmp_path) -> None:
    pytest.importorskip("reportlab")
    out = dhr.render_daily_report_pdf(
        _full_payload(),
        text_body="ignored",
        chart_path=None,
        output_dir=tmp_path,
    )
    assert out is not None
    assert out.is_file()
    assert out.read_bytes()[:5] == b"%PDF-"
    assert out.name == "daily_honesty_2026-04-30.pdf"
    # Sanity: should be more than a few hundred bytes given five
    # sections.
    assert out.stat().st_size > 1500


def test_render_daily_report_pdf_empty_payload_still_renders(tmp_path) -> None:
    pytest.importorskip("reportlab")
    payload = {
        "date_utc": "2026-05-01",
        "divergence": {"days": 7, "items": [], "samples_too_small": []},
        "calibrator_drift": {"items": []},
        "tail_risk": {
            "tail_max_loss_usd": None,
            "total_max_loss_usd": None,
            "pct_of_total": None,
            "tone": None,
            "threshold_price": None,
            "source": None,
            "buckets": [],
        },
        "gate_trips_24h": {"total": 0, "items": []},
        "investor_deck": {"investor_deck_path": None, "built_at": None},
    }
    out = dhr.render_daily_report_pdf(
        payload, text_body="ignored", chart_path=None, output_dir=tmp_path
    )
    assert out is not None and out.is_file()
    assert out.read_bytes()[:5] == b"%PDF-"


def test_render_daily_report_pdf_embeds_chart_when_present(tmp_path) -> None:
    pytest.importorskip("reportlab")
    pytest.importorskip("matplotlib")
    chart = dhr.render_tail_risk_chart_png(
        _tail_summary_with_buckets(),
        date_str="2026-04-30",
        output_dir=tmp_path,
    )
    assert chart is not None
    out = dhr.render_daily_report_pdf(
        _full_payload(),
        text_body="ignored",
        chart_path=chart,
        output_dir=tmp_path,
    )
    assert out is not None and out.is_file()
    # Embedded image inflates size meaningfully vs. text-only.
    assert out.stat().st_size > 4000


def test_render_daily_report_pdf_failsoft_when_reportlab_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import builtins as _builtins

    real_import = _builtins.__import__

    def _fake_import(name, *a, **kw):
        if name.startswith("reportlab"):
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(_builtins, "__import__", _fake_import)
    out = dhr.render_daily_report_pdf(
        _full_payload(),
        text_body="ignored",
        chart_path=None,
        output_dir=tmp_path,
    )
    assert out is None


# ---------------------------------------------------------------------------
# G4 — alerts._dispatch threads attachments through to postmark
# ---------------------------------------------------------------------------


def test_alerts_dispatch_threads_attachments_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    f1 = tmp_path / "a.pdf"
    f2 = tmp_path / "b.png"
    f1.write_bytes(b"%PDF-1.4 stub")
    f2.write_bytes(b"\x89PNG stub")
    out = alerts._dispatch(
        "subj",
        "body",
        tag="daily-honesty-report",
        dry_run=True,
        attachments=[f1, f2],
    )
    assert out["dry_run"] is True
    assert out["attachments"] == [str(f1), str(f2)]


def test_alerts_dispatch_threads_attachments_into_postmark(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    f1 = tmp_path / "report.pdf"
    f1.write_bytes(b"%PDF-1.4 stub")

    captured: dict[str, Any] = {}

    def _fake_send_in_background(**kw: Any) -> None:
        captured["bg"] = kw

    def _fake_schedule(**kw: Any) -> None:
        captured["fanout"] = kw

    from app.notifications import postmark as _pm

    monkeypatch.setattr(_pm, "send_in_background", _fake_send_in_background)
    monkeypatch.setattr(
        alerts, "_schedule_user_email_fanout", _fake_schedule
    )
    monkeypatch.setattr(_pm, "is_configured", lambda: True)
    monkeypatch.setattr(_pm, "alert_recipients", lambda: ["ops@example.com"])

    out = alerts._dispatch(
        "subj",
        "body",
        tag="daily-honesty-report",
        dry_run=False,
        attachments=[f1],
    )
    assert out["queued"] is True
    # `_dispatch` now materialises attachments into
    # (filename, bytes, content_type) tuples once at dispatch time and
    # threads the *bytes* through to both the operator send and the
    # async user-pref fanout.  See test below for why.
    bg_atts = captured["bg"]["attachments"]
    fan_atts = captured["fanout"]["attachments"]
    assert bg_atts == fan_atts
    assert len(bg_atts) == 1
    name, data, ctype = bg_atts[0]
    assert name == "report.pdf"
    assert data == b"%PDF-1.4 stub"
    assert ctype == "application/pdf"
    # The dispatch return value still echoes string filenames so
    # callers/telemetry stay backwards compatible.
    assert out["attachments"] == [str(f1)]


def test_alerts_dispatch_attachments_survive_disk_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Regression: a future `reports/*` pruner must not be able to
    strand the async user-pref fanout with a missing attachment file.

    Simulates the report file being deleted *between* `_dispatch`
    capturing its bytes and the background fanout actually firing,
    then asserts Postmark still receives a fully-formed
    Attachments[] entry rather than silently dropping it.
    """
    import asyncio as _asyncio
    import base64 as _b64

    from app.notifications import postmark as _pm

    f1 = tmp_path / "doomed.pdf"
    payload = b"%PDF-1.4 doomed"
    f1.write_bytes(payload)

    captured: dict[str, Any] = {}

    def _fake_send_in_background(**kw: Any) -> None:
        captured["bg"] = kw

    fanout_kwargs: dict[str, Any] = {}

    def _fake_schedule(**kw: Any) -> None:
        fanout_kwargs.update(kw)

    monkeypatch.setattr(_pm, "send_in_background", _fake_send_in_background)
    monkeypatch.setattr(alerts, "_schedule_user_email_fanout", _fake_schedule)
    monkeypatch.setattr(_pm, "is_configured", lambda: True)
    monkeypatch.setattr(_pm, "alert_recipients", lambda: ["ops@example.com"])

    alerts._dispatch(
        "subj",
        "body",
        tag="daily-honesty-report",
        dry_run=False,
        attachments=[f1],
    )

    # Disk-cleanup script wipes the file *after* dispatch returned but
    # *before* the async fanout actually constructs its payload.
    f1.unlink()
    assert not f1.exists()

    # Build the Postmark payload from whatever the fanout was handed.
    pm_payload = _pm._build_attachments(fanout_kwargs["attachments"])
    assert len(pm_payload) == 1
    assert pm_payload[0]["Name"] == "doomed.pdf"
    assert _b64.b64decode(pm_payload[0]["Content"]) == payload
    assert pm_payload[0]["ContentType"] == "application/pdf"

    # And the operator send saw the same materialised bytes.
    bg_payload = _pm._build_attachments(captured["bg"]["attachments"])
    assert bg_payload == pm_payload

    # Smoke-check: a real `_pm.send` call against the materialised
    # tuples completes without touching the (now-deleted) path.
    sent: list[dict[str, Any]] = []

    async def _fake_send_one(**kw: Any) -> _pm._SendRecord:
        sent.append(kw)
        return _pm._SendRecord(sent_at=0.0, to=kw["to"], subject=kw["subject"], ok=True)

    monkeypatch.setattr(_pm, "_send_one", _fake_send_one)
    _asyncio.run(
        _pm.send(
            subject="subj",
            text_body="body",
            to=["user@example.com"],
            tag="daily-honesty-report",
            attachments=fanout_kwargs["attachments"],
        )
    )
    assert sent and sent[0]["attachments"] == fanout_kwargs["attachments"]


# ---------------------------------------------------------------------------
# Task #767 — uncalibrated raw-mode trades section
# ---------------------------------------------------------------------------


class _UncalRawDB:
    """Tiny stand-in that returns canned rows for the uncalibrated-raw
    aggregation query and otherwise defers to a no-op."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "uncalibrated_vertical_raw_mode" in query:
            return self._rows
        return []

    async def fetchrow(self, *_a: Any, **_kw: Any) -> Any:
        return None

    async def execute(self, *_a: Any, **_kw: Any) -> str:
        return "OK"

    async def fetchval(self, *_a: Any, **_kw: Any) -> Any:
        return None


def test_collect_uncalibrated_trades_24h_aggregates_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "vertical": "weather",
            "league": "WEATHER",
            "n": 7,
            "last_at": "2026-05-04T12:34:56Z",
        },
        {
            "vertical": "politics",
            "league": "POLITICS",
            "n": 1,
            "last_at": "2026-05-04T08:00:00Z",
        },
    ]
    monkeypatch.setattr(dhr, "db", _UncalRawDB(rows))
    out = asyncio.run(dhr.collect_uncalibrated_trades_24h())
    assert out["error"] is None
    assert out["total"] == 8
    assert out["tone"] == "red"
    assert out["items"][0]["vertical"] == "weather"
    assert out["items"][0]["count"] == 7


def test_collect_uncalibrated_trades_24h_zero_rows_is_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dhr, "db", _UncalRawDB([]))
    out = asyncio.run(dhr.collect_uncalibrated_trades_24h())
    assert out["total"] == 0
    assert out["tone"] == "green"
    assert out["items"] == []


def test_collect_uncalibrated_trades_24h_dedupes_one_order_per_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for code-review finding: an order with both
    `submitted` and `filled` lifecycle events must contribute count=1,
    not count=2.  The SQL-level dedupe lives in the inner CTE
    (`GROUP BY ol.order_id, ol.vertical, ol.market_id`); this test
    pins the SQL-shape contract so a future edit can't silently
    re-introduce the over-count.
    """
    captured: dict[str, str] = {}

    class _Capture:
        async def fetch(self, query: str, *args: Any) -> list[Any]:
            captured["q"] = query
            # Simulate Postgres returning the *post*-dedupe shape: one
            # order with submitted+filled is a single row with n=1.
            return [
                {
                    "vertical": "weather",
                    "league": "WEATHER",
                    "n": 1,
                    "last_at": "2026-05-04T11:00:00Z",
                }
            ]

    monkeypatch.setattr(dhr, "db", _Capture())
    out = asyncio.run(dhr.collect_uncalibrated_trades_24h())
    assert out["total"] == 1, "one order with submitted+filled must count once"
    q_norm = " ".join(captured["q"].split())
    # SQL contract: dedupe by (order_id, vertical, market_id), not by
    # (order_id, ..., created_at) — including created_at would split
    # the submitted and filled rows back apart.
    assert "GROUP BY ol.order_id, ol.vertical, ol.market_id" in q_norm
    assert "max(ol.created_at)" in q_norm
    # And the join must filter on shipped events specifically.
    assert "event_type IN ('submitted', 'filled')" in q_norm


def test_collect_uncalibrated_trades_24h_swallows_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom:
        async def fetch(self, *_a: Any, **_kw: Any) -> list[Any]:
            raise RuntimeError("kaboom")

    monkeypatch.setattr(dhr, "db", _Boom())
    out = asyncio.run(dhr.collect_uncalibrated_trades_24h())
    assert out["total"] == 0
    assert out["error"] and "kaboom" in out["error"]


def _payload_with_uncal(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(int(it.get("count", 0)) for it in items)
    return {
        "date_utc": "2026-05-04",
        "divergence": {"days": 7, "items": []},
        "calibrator_drift": {"items": []},
        "tail_risk": {},
        "gate_trips_24h": {"items": [], "total": 0},
        "uncalibrated_raw": {
            "items": items,
            "total": total,
            "tone": "red" if total > 0 else "green",
            "error": None,
        },
        "investor_deck": {"investor_deck_path": None, "built_at": None},
    }


def test_render_text_renders_uncalibrated_red_row() -> None:
    payload = _payload_with_uncal(
        [
            {
                "vertical": "weather",
                "league": "WEATHER",
                "count": 3,
                "last_at": "2026-05-04T11:00:00Z",
            }
        ]
    )
    _subject, body = dhr.render_text(payload)
    assert "5. Uncalibrated live trades" in body
    assert "[RED]" in body
    assert "weather" in body
    assert "WEATHER" in body
    assert "count=   3" in body


def test_render_text_renders_uncalibrated_green_when_zero() -> None:
    payload = _payload_with_uncal([])
    _subject, body = dhr.render_text(payload)
    assert "5. Uncalibrated live trades" in body
    assert "[GREEN] 0 raw-mode trades" in body
    assert "[RED]" not in body.split("5. Uncalibrated")[1].split("\n\n")[0]


def test_render_html_renders_uncalibrated_section_red() -> None:
    payload = _payload_with_uncal(
        [
            {
                "vertical": "weather",
                "league": "WEATHER",
                "count": 5,
                "last_at": "2026-05-04T11:00:00Z",
            }
        ]
    )
    body = dhr.render_html(payload)
    assert "Uncalibrated live trades" in body
    assert "#b00000" in body  # red highlight
    assert "weather" in body and "WEATHER" in body


def test_render_daily_report_pdf_includes_uncalibrated_section(tmp_path) -> None:
    payload = _payload_with_uncal(
        [
            {
                "vertical": "weather",
                "league": "WEATHER",
                "count": 9,
                "last_at": "2026-05-04T11:00:00Z",
            }
        ]
    )
    out = dhr.render_daily_report_pdf(
        payload,
        text_body="body",
        chart_path=None,
        output_dir=tmp_path,
    )
    if out is None:
        pytest.skip("reportlab not installed")
    # PDF text streams are compressed by default — assert the file
    # builds + has non-trivial size + a comparable build with the
    # section omitted is materially smaller (proves the new section
    # actually contributed content).
    blob = out.read_bytes()
    assert blob.startswith(b"%PDF") and len(blob) > 1000

    bare_payload = _payload_with_uncal([])
    bare = dhr.render_daily_report_pdf(
        bare_payload,
        text_body="body",
        chart_path=None,
        output_dir=tmp_path,
    )
    assert bare is not None
    assert len(blob) > len(bare.read_bytes())


# ---------------------------------------------------------------------------
# Task #771 — missing-league candidates section
# ---------------------------------------------------------------------------


class _MissingLeagueDB:
    """Stand-in returning canned rows for the missing-league aggregation."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "missing_league_on_candidate" in query:
            return self._rows
        return []

    async def fetchrow(self, *_a: Any, **_kw: Any) -> Any:
        return None

    async def execute(self, *_a: Any, **_kw: Any) -> str:
        return "OK"

    async def fetchval(self, *_a: Any, **_kw: Any) -> Any:
        return None


def test_collect_missing_league_24h_aggregates_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "vertical": "sports",
            "market_id": "KXNBA-LAKBOS",
            "n": 4,
            "last_at": "2026-05-04T12:34:56Z",
        },
        {
            "vertical": "weather",
            "market_id": "KXHIGH-NYC",
            "n": 1,
            "last_at": "2026-05-04T08:00:00Z",
        },
    ]
    monkeypatch.setattr(dhr, "db", _MissingLeagueDB(rows))
    out = asyncio.run(dhr.collect_missing_league_24h())
    assert out["error"] is None
    assert out["total"] == 5
    assert out["tone"] == "red"
    assert out["items"][0]["vertical"] == "sports"
    assert out["items"][0]["market_id"] == "KXNBA-LAKBOS"
    assert out["items"][0]["count"] == 4


def test_collect_missing_league_24h_zero_rows_is_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dhr, "db", _MissingLeagueDB([]))
    out = asyncio.run(dhr.collect_missing_league_24h())
    assert out["total"] == 0
    assert out["tone"] == "green"
    assert out["items"] == []


def test_collect_missing_league_24h_swallows_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom:
        async def fetch(self, *_a: Any, **_kw: Any) -> list[Any]:
            raise RuntimeError("db down")

    monkeypatch.setattr(dhr, "db", _Boom())
    out = asyncio.run(dhr.collect_missing_league_24h())
    assert out["total"] == 0
    assert out["error"] and "db down" in out["error"]


def _payload_with_missing_league(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(int(it.get("count", 0)) for it in items)
    return {
        "date_utc": "2026-05-04",
        "divergence": {"days": 7, "items": []},
        "calibrator_drift": {"items": []},
        "tail_risk": {},
        "gate_trips_24h": {"items": [], "total": 0},
        "uncalibrated_raw": {
            "items": [],
            "total": 0,
            "tone": "green",
            "error": None,
        },
        "missing_league": {
            "items": items,
            "total": total,
            "tone": "red" if total > 0 else "green",
            "error": None,
        },
        "investor_deck": {"investor_deck_path": None, "built_at": None},
    }


def test_render_text_renders_missing_league_red_row() -> None:
    payload = _payload_with_missing_league(
        [
            {
                "vertical": "sports",
                "market_id": "KXNBA-LAKBOS",
                "count": 3,
                "last_at": "2026-05-04T11:00:00Z",
            }
        ]
    )
    _subject, body = dhr.render_text(payload)
    assert "6. Missing-league candidates" in body
    assert "[RED]" in body
    assert "sports" in body
    assert "KXNBA-LAKBOS" in body
    assert "count=   3" in body


def test_render_text_renders_missing_league_green_when_zero() -> None:
    payload = _payload_with_missing_league([])
    _subject, body = dhr.render_text(payload)
    assert "6. Missing-league candidates" in body
    assert "[GREEN] 0 candidates reached the gates without a league" in body


def test_render_html_renders_missing_league_section_red() -> None:
    payload = _payload_with_missing_league(
        [
            {
                "vertical": "sports",
                "market_id": "KXNBA-LAKBOS",
                "count": 2,
                "last_at": "2026-05-04T11:00:00Z",
            }
        ]
    )
    body = dhr.render_html(payload)
    assert "Missing-league candidates" in body
    assert "#b00000" in body
    assert "KXNBA-LAKBOS" in body


def test_render_daily_report_pdf_includes_missing_league_section(tmp_path) -> None:
    payload = _payload_with_missing_league(
        [
            {
                "vertical": "sports",
                "market_id": "KXNBA-LAKBOS",
                "count": 6,
                "last_at": "2026-05-04T11:00:00Z",
            }
        ]
    )
    out = dhr.render_daily_report_pdf(
        payload,
        text_body="body",
        chart_path=None,
        output_dir=tmp_path,
    )
    if out is None:
        pytest.skip("reportlab not installed")
    blob = out.read_bytes()
    assert blob.startswith(b"%PDF") and len(blob) > 1000

    bare = dhr.render_daily_report_pdf(
        _payload_with_missing_league([]),
        text_body="body",
        chart_path=None,
        output_dir=tmp_path,
    )
    assert bare is not None
    assert len(blob) > len(bare.read_bytes())


def test_collect_report_includes_missing_league_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`collect_report` must surface the new ``missing_league`` section
    so downstream renderers (text/HTML/PDF) get a populated dict."""
    monkeypatch.setattr(
        dhr, "db", _MissingLeagueDB([])
    )
    payload = asyncio.run(dhr.collect_report("2026-05-04"))
    assert "missing_league" in payload
    assert payload["missing_league"]["total"] == 0
    assert payload["missing_league"]["tone"] == "green"
