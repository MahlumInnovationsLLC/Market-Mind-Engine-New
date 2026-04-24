"""Tests for the historical entertainment-signal backfill (task #225)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthedUser, require_admin
from app.entertainment import backfill as ent_backfill
from app.entertainment import signals as ent_signals_mod
from app.entertainment.signals import SignalRow
from app.feeds import entertainment_signals as feeds_mod
from app.main import app


# ---------------------------------------------------------------------------
# Tiny in-memory db substitute keyed on (source, signal_id).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.system_settings: dict[str, str] = {}

    async def execute(self, query: str, *args) -> None:
        q = " ".join(query.split()).lower()
        if q.startswith("insert into entertainment_signals"):
            (
                source, signal_id, signal_type, title, event_at,
                value, settled, outcome, payload_json,
            ) = args
            key = (source, signal_id)
            existing = self.rows.get(key)
            row = {
                "source": source,
                "signal_id": signal_id,
                "signal_type": signal_type,
                "title": title,
                "event_at": event_at,
                "value": value if value is not None else (
                    existing.get("value") if existing else None),
                "settled": (existing.get("settled", False) if existing else False) or bool(settled),
                "outcome": outcome if outcome is not None else (
                    existing.get("outcome") if existing else None),
                "payload": json.loads(payload_json) if payload_json else {},
            }
            self.rows[key] = row
            return None
        if q.startswith("insert into system_settings"):
            key, value = args[0], args[1]
            self.system_settings[key] = value
            return None
        # Unknown query — quiet no-op for resilience in tests.
        return None

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split()).lower()
        if q.startswith("select value from system_settings where key"):
            key = args[0]
            if key in self.system_settings:
                return {"value": self.system_settings[key]}
            return None
        return None

    async def fetch(self, query: str, *args):
        return []


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    store = _FakeStore()
    monkeypatch.setattr(feeds_mod.db, "execute", store.execute)
    monkeypatch.setattr(ent_backfill.db, "execute", store.execute)
    monkeypatch.setattr(ent_backfill.db, "fetchrow", store.fetchrow)
    ent_backfill.reset_status_for_tests()
    return store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _box_office_fetcher_factory(call_log: list[tuple[datetime, datetime]]):
    async def _fetch(start: datetime, end: datetime) -> list[dict]:
        call_log.append((start, end))
        # Two synthetic top-2 rows per window — deterministic ids.
        return [
            {
                "source": "box_office_mojo_history",
                "signal_id": f"hist/{end:%Y-%m-%d}/1/MOVIEA",
                "signal_type": "BOX",
                "title": "MOVIEA",
                "event_at": end,
                "value": 120.0,
                "settled": True,
                "outcome": "released",
                "payload": {"rank": 1},
            },
            {
                "source": "box_office_mojo_history",
                "signal_id": f"hist/{end:%Y-%m-%d}/2/MOVIEB",
                "signal_type": "BOX",
                "title": "MOVIEB",
                "event_at": end,
                "value": 80.0,
                "settled": True,
                "outcome": "released",
                "payload": {"rank": 2},
            },
        ]
    return _fetch


# ---------------------------------------------------------------------------
# Window iteration
# ---------------------------------------------------------------------------


def test_iter_windows_walks_back_in_cadence_strides():
    end = datetime(2025, 1, 29, tzinfo=timezone.utc)
    start = end - timedelta(days=21)
    windows = ent_backfill._iter_windows(start, end, cadence_days=7)
    # 21d / 7d = 3 windows, newest-first.
    assert len(windows) == 3
    assert windows[0][1] == end
    # Each successive window ends where the previous one started.
    for i in range(1, len(windows)):
        assert windows[i][1] == windows[i - 1][0]


def test_iter_windows_caps_first_window_at_start():
    end = datetime(2025, 1, 10, tzinfo=timezone.utc)
    start = end - timedelta(days=10)
    windows = ent_backfill._iter_windows(start, end, cadence_days=7)
    # Two windows: [start, start+7d] and [start+7d, end] – walked
    # back so the *last* window's start equals our start bound.
    assert windows[-1][0] == start


# ---------------------------------------------------------------------------
# run_backfill orchestration
# ---------------------------------------------------------------------------


def test_run_backfill_invokes_each_fetcher_and_upserts(
    fake_db: _FakeStore,
) -> None:
    call_log: list[tuple[datetime, datetime]] = []
    fetchers = [
        ent_backfill.HistoricalFetcher(
            source="box_office_mojo_history",
            cadence_days=7,
            fetch=_box_office_fetcher_factory(call_log),
        ),
    ]
    snap = asyncio.run(ent_backfill.run_backfill(days=21, fetchers=fetchers))
    # Three weekly windows × two rows each = 6 deterministic upserts.
    assert len(call_log) == 3
    assert snap["state"] == "completed"
    assert snap["rows_total"] == 6
    assert snap["rows_by_source"]["box_office_mojo_history"] == 6
    assert snap["days"] == 21
    assert snap["sources_failed"] == []
    # Idempotency: same call IDs land in the in-memory store exactly
    # once per (source, signal_id).
    assert len(fake_db.rows) == 6
    # Status persisted to system_settings under our reserved key.
    persisted = fake_db.system_settings.get(ent_backfill._STATUS_KEY)
    assert persisted is not None
    assert json.loads(persisted)["rows_total"] == 6


def test_run_backfill_is_idempotent(fake_db: _FakeStore) -> None:
    call_log: list[tuple[datetime, datetime]] = []
    fetchers = [
        ent_backfill.HistoricalFetcher(
            source="box_office_mojo_history",
            cadence_days=7,
            fetch=_box_office_fetcher_factory(call_log),
        ),
    ]
    asyncio.run(ent_backfill.run_backfill(days=14, fetchers=fetchers))
    rows_after_first = dict(fake_db.rows)
    asyncio.run(ent_backfill.run_backfill(days=14, fetchers=fetchers))
    # Re-running the backfill must not duplicate rows.
    assert set(fake_db.rows.keys()) == set(rows_after_first.keys())


def test_run_backfill_continues_after_one_source_crashes(
    fake_db: _FakeStore,
) -> None:
    async def _broken_fetcher(start, end):
        raise RuntimeError("boom")

    async def _good_fetcher(start, end):
        return [{
            "source": "spotify_charts_history",
            "signal_id": f"hist/{end:%Y-%m-%d}/1/A/T",
            "signal_type": "SPOTIFY",
            "title": "A - T",
            "event_at": end,
            "value": 1.0,
            "settled": True,
            "outcome": "charted",
            "payload": {},
        }]

    fetchers = [
        ent_backfill.HistoricalFetcher(
            source="box_office_mojo_history",
            cadence_days=7, fetch=_broken_fetcher,
        ),
        ent_backfill.HistoricalFetcher(
            source="spotify_charts_history",
            cadence_days=7, fetch=_good_fetcher,
        ),
    ]
    snap = asyncio.run(ent_backfill.run_backfill(days=14, fetchers=fetchers))
    assert snap["state"] == "completed"
    # Broken fetcher's error logged, but the other source still ran.
    assert snap["rows_by_source"]["spotify_charts_history"] >= 1
    assert snap["rows_by_source"]["box_office_mojo_history"] == 0


def test_run_backfill_clamps_days_to_safe_bounds(
    fake_db: _FakeStore,
) -> None:
    call_log: list[tuple[datetime, datetime]] = []
    fetchers = [
        ent_backfill.HistoricalFetcher(
            source="box_office_mojo_history",
            cadence_days=7,
            fetch=_box_office_fetcher_factory(call_log),
        ),
    ]
    snap = asyncio.run(ent_backfill.run_backfill(days=999_999, fetchers=fetchers))
    assert snap["days"] == ent_backfill.MAX_BACKFILL_DAYS
    snap2 = asyncio.run(ent_backfill.run_backfill(days=-5, fetchers=fetchers))
    assert snap2["days"] == ent_backfill.MIN_BACKFILL_DAYS


# ---------------------------------------------------------------------------
# Builtin parsers (HTML / CSV / JSON)
# ---------------------------------------------------------------------------


def test_box_office_html_parser_extracts_titles_and_grosses():
    html = """
    <table>
      <tr><td>1</td><td><a>Avatar 3</a></td><td>$120,500,000</td></tr>
      <tr><td>2</td><td>Inside Out 3</td><td>$45,000,000</td></tr>
    </table>
    """
    week_end = datetime(2025, 1, 17, tzinfo=timezone.utc)
    rows = ent_backfill._box_office_rows_from_html(html, week_end)
    assert len(rows) == 2
    assert rows[0]["title"] == "Avatar 3"
    assert rows[0]["value"] == pytest.approx(120.5)
    assert rows[0]["signal_type"] == "BOX"
    assert rows[0]["settled"] is True
    assert "iso_week" in rows[0]["payload"]


def test_awards_window_filters_by_year_range(monkeypatch: pytest.MonkeyPatch):
    doc = {
        "OSCAR": [
            {"year": 2018, "race": "BESTPIC", "winner": "OLD"},
            {"year": 2024, "race": "BESTPIC", "winner": "NEW"},
        ],
        "EMMY": [
            {"year": 2024, "race": "DRAMA", "winner": "SHOGUN"},
        ],
        "GRAMMY": "not-a-list",
    }
    body = json.dumps(doc)

    async def _fake_get(*_args, **_kw):
        return body

    monkeypatch.setattr(feeds_mod, "_http_get_text", _fake_get)
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = asyncio.run(ent_backfill._fetch_awards_window(start, end))
    titles = {(r["signal_type"], r["title"]) for r in rows}
    assert ("OSCAR", "NEW") in titles
    assert ("EMMY", "SHOGUN") in titles
    # 2018 falls outside the [2023..2025] window — must not appear.
    assert ("OSCAR", "OLD") not in titles


# ---------------------------------------------------------------------------
# Single-flight lock
# ---------------------------------------------------------------------------


def test_concurrent_backfills_are_rejected(fake_db: _FakeStore) -> None:
    async def _runner() -> None:
        gate = asyncio.Event()
        release = asyncio.Event()

        async def _slow(start, end):
            gate.set()
            await release.wait()
            return []

        fetchers = [
            ent_backfill.HistoricalFetcher(
                source="box_office_mojo_history",
                cadence_days=7, fetch=_slow,
            ),
        ]
        first = asyncio.create_task(
            ent_backfill.run_backfill(days=14, fetchers=fetchers)
        )
        await gate.wait()
        with pytest.raises(RuntimeError):
            await ent_backfill.run_backfill(days=14, fetchers=fetchers)
        assert ent_backfill.is_running() is True
        release.set()
        await first

    asyncio.run(_runner())
    assert ent_backfill.is_running() is False


# ---------------------------------------------------------------------------
# Integration: model picks up new history after backfill
# ---------------------------------------------------------------------------


def test_model_uses_backfilled_history_for_lognormal_sigma(
    fake_db: _FakeStore,
) -> None:
    """End-to-end check: with no history the box-office head's sigma
    falls back to the default; after a backfill seeds settled rows,
    the at-baseline result reports the *fitted* sigma."""
    from app.entertainment import model as ent_model

    upcoming = SignalRow(
        source="box_office_mojo_rss",
        signal_id="bom/AVATAR3",
        signal_type="BOX",
        title="AVATAR3",
        event_at=datetime.now(timezone.utc) + timedelta(days=2),
        value=None, settled=False, outcome=None, payload={},
    )

    # Pre-backfill: no settled rows → fallback sigma.
    pre = ent_model.at_baseline_probability(upcoming, [])
    assert pre.contributing_history == 0
    pre_sigma = pre.feature_summary["sigma_log"]
    assert pre_sigma == ent_model._DEFAULT_LOG_SIGMA["BOX"]

    # Run a small backfill that seeds 6 settled BOX rows with a
    # noticeably different sigma than the default.
    async def _seeded(start, end):
        rows = []
        # Each window emits one row with a controlled value so the
        # fitted log-sigma is non-trivial and != fallback.
        spread = abs((end.day % 5) - 2) * 30 + 50
        rows.append({
            "source": "box_office_mojo_history",
            "signal_id": f"hist/{end:%Y-%m-%d}/A",
            "signal_type": "BOX",
            "title": f"PRIOR_{end:%Y%m%d}",
            "event_at": end,
            "value": float(spread),
            "settled": True,
            "outcome": "released",
            "payload": {},
        })
        return rows

    fetchers = [
        ent_backfill.HistoricalFetcher(
            source="box_office_mojo_history",
            cadence_days=7, fetch=_seeded,
        ),
    ]
    asyncio.run(ent_backfill.run_backfill(days=42, fetchers=fetchers))

    # Read back the seeded rows from the in-memory store and feed them
    # into the model — this mirrors what `settled_history_for_type`
    # does at runtime.
    seeded_rows = [
        SignalRow(
            source=r["source"], signal_id=r["signal_id"],
            signal_type=r["signal_type"], title=r["title"],
            event_at=r["event_at"], value=r["value"],
            settled=r["settled"], outcome=r["outcome"],
            payload=r["payload"],
        ) for r in fake_db.rows.values()
    ]
    assert len(seeded_rows) >= 3
    post = ent_model.at_baseline_probability(upcoming, seeded_rows)
    assert post.contributing_history >= 3
    # Fitted sigma must differ from the hard-coded fallback — i.e.,
    # the model is now actually using the backfilled history.
    assert post.feature_summary["sigma_log"] != pytest.approx(pre_sigma)


# ---------------------------------------------------------------------------
# Admin route: POST /api/admin/entertainment/backfill
# ---------------------------------------------------------------------------


@pytest.fixture
def override_admin():
    async def _fake_admin() -> AuthedUser:
        return AuthedUser(user_id="ops_admin", session_id="s", claims={})

    app.dependency_overrides[require_admin] = _fake_admin
    yield
    app.dependency_overrides.pop(require_admin, None)


def test_admin_backfill_endpoint_kicks_off_run(
    fake_db: _FakeStore, override_admin
) -> None:
    captured: dict = {}

    async def _fake_run(*, days: int, fetchers=None) -> dict:
        captured["days"] = days
        return {"state": "completed", "rows_total": 0, "days": days,
                "rows_by_source": {}, "sources_failed": [],
                "started_at": None, "completed_at": None,
                "duration_seconds": 0.0, "last_error": None}

    with patch.object(ent_backfill, "run_backfill", side_effect=_fake_run):
        client = TestClient(app)
        r = client.post("/api/admin/entertainment/backfill",
                        json={"days": 60})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["started"] is True
        assert body["days"] == 60

    # Wait briefly for the background task to flush.
    async def _drain():
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drain())


def test_admin_backfill_endpoint_rejects_concurrent_runs(
    fake_db: _FakeStore, override_admin
) -> None:
    with patch.object(ent_backfill, "is_running", return_value=True), \
         patch.object(ent_backfill, "get_status", return_value={
             "state": "running", "started_at": "x"}):
        client = TestClient(app)
        r = client.post("/api/admin/entertainment/backfill", json={"days": 30})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "backfill_already_running"


def test_admin_backfill_endpoint_validates_days_bounds(
    fake_db: _FakeStore, override_admin,
) -> None:
    client = TestClient(app)
    r = client.post("/api/admin/entertainment/backfill", json={"days": 5})
    assert r.status_code == 422
    r = client.post("/api/admin/entertainment/backfill",
                    json={"days": 365 * 10})
    assert r.status_code == 422


def test_admin_backfill_status_endpoint_surfaces_persisted_snapshot(
    fake_db: _FakeStore, override_admin,
) -> None:
    fake_db.system_settings[ent_backfill._STATUS_KEY] = json.dumps({
        "state": "completed",
        "rows_total": 1234,
        "rows_by_source": {"box_office_mojo_history": 1234},
        "started_at": "2026-04-01T00:00:00+00:00",
        "completed_at": "2026-04-01T00:05:00+00:00",
        "duration_seconds": 300.0,
        "days": 730,
        "sources_failed": [],
        "last_error": None,
    })
    client = TestClient(app)
    r = client.get("/api/admin/entertainment/backfill-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_persisted"]["rows_total"] == 1234
    assert body["current"]["state"] in {"idle", "completed"}


# ---------------------------------------------------------------------------
# /api/entertainment/calendar surfaces the persisted backfill snapshot
# ---------------------------------------------------------------------------


def test_calendar_route_includes_backfill_status(
    fake_db: _FakeStore,
) -> None:
    """The calendar payload must carry the persisted backfill row so
    the dashboard panel can show the last-run timestamp + counts."""
    fake_db.system_settings[ent_backfill._STATUS_KEY] = json.dumps({
        "state": "completed",
        "rows_total": 42,
        "rows_by_source": {"box_office_mojo_history": 42},
        "started_at": "2026-04-01T00:00:00+00:00",
        "completed_at": "2026-04-01T00:05:00+00:00",
        "duration_seconds": 300.0,
        "days": 730,
        "sources_failed": [],
        "last_error": None,
    })

    async def _no_upcoming(within_hours=168.0, limit=50, signal_type=None):
        return []

    with patch.object(ent_signals_mod, "upcoming_signals",
                      side_effect=_no_upcoming):
        client = TestClient(app)
        r = client.get("/api/entertainment/calendar")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["backfill"] is not None
    assert body["backfill"]["rows_total"] == 42
