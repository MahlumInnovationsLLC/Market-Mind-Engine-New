"""Tests for the operator dashboard tournament-leaderboards panel
(task #650).

The helper hangs a thin shaping layer off
:func:`app.feeds.sportradar.latest_tournament_leaderboard`, so we stub
the underlying ``db`` calls + helper to exercise the panel-shaping
logic without touching a real Postgres.

Coverage:
  * Distinct event lookup is joined with ``sportradar_schedules`` to
    surface league + display name.
  * Each event carries the position-ordered roster + ``fetched_at``.
  * Empty state (no snapshots) returns ``{"events": []}`` rather than
    raising — the panel falls back gracefully.
  * Per-event snapshot failures are isolated so one missing event
    doesn't blank the rest of the panel.
  * The HTTP endpoint round-trips the helper payload behind the
    standard approved-user gate.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import dashboard, db, main as main_module
from app.auth import AuthedUser, require_approved_user
from app.feeds import sportradar
from app.main import app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_get_tournament_leaderboards_shapes_recent_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: distinct events are enriched with schedule metadata
    and each carries its position-ordered roster + fetched_at."""
    pga_ts = datetime(2026, 4, 27, 12, 5, tzinfo=timezone.utc)
    f1_ts = datetime(2026, 4, 27, 12, 10, tzinfo=timezone.utc)

    distinct_rows = [
        {"event_id": "sr:race:f1-1", "fetched_at": f1_ts},
        {"event_id": "sr:tournament:pga-1", "fetched_at": pga_ts},
    ]
    schedule_rows = [
        {
            "league": "PGA",
            "game_id": "sr:tournament:pga-1",
            "home_team": "The Masters",
            "away_team": "",
            "status": "inprogress",
        },
        {
            "league": "F1",
            "game_id": "sr:race:f1-1",
            "home_team": "Miami GP",
            "away_team": "",
            "status": "live",
        },
    ]

    fetched_queries: list[tuple[str, tuple]] = []

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        fetched_queries.append((query, args))
        if "sportradar_tournament_leaderboards" in query:
            return distinct_rows
        if "sportradar_schedules" in query:
            return schedule_rows
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)

    snaps: dict[str, list[dict[str, Any]]] = {
        "sr:tournament:pga-1": [
            {"position": 1, "name": "Rahm", "fetched_at": pga_ts,
             "event_id": "sr:tournament:pga-1"},
            {"position": 2, "name": "Scheffler", "fetched_at": pga_ts,
             "event_id": "sr:tournament:pga-1"},
            {"position": None, "name": "Withdrew", "fetched_at": pga_ts,
             "event_id": "sr:tournament:pga-1"},
        ],
        "sr:race:f1-1": [
            {"position": 1, "name": "Verstappen", "fetched_at": f1_ts,
             "event_id": "sr:race:f1-1"},
            {"position": 2, "name": "Norris", "fetched_at": f1_ts,
             "event_id": "sr:race:f1-1"},
        ],
    }

    async def _fake_latest(event_id: str) -> list[dict[str, Any]]:
        return list(snaps.get(event_id, []))

    monkeypatch.setattr(
        sportradar, "latest_tournament_leaderboard", _fake_latest,
    )

    out = _run(dashboard.get_tournament_leaderboards())
    events = out["events"]
    assert len(events) == 2
    by_id = {e["event_id"]: e for e in events}

    pga = by_id["sr:tournament:pga-1"]
    assert pga["league"] == "PGA"
    assert pga["name"] == "The Masters"
    assert pga["status"] == "inprogress"
    assert pga["fetched_at"] == pga_ts.isoformat()
    assert [(r["position"], r["name"]) for r in pga["entries"]] == [
        (1, "Rahm"), (2, "Scheffler"), (None, "Withdrew"),
    ]

    f1 = by_id["sr:race:f1-1"]
    assert f1["league"] == "F1"
    assert f1["name"] == "Miami GP"
    assert f1["fetched_at"] == f1_ts.isoformat()
    assert [(r["position"], r["name"]) for r in f1["entries"]] == [
        (1, "Verstappen"), (2, "Norris"),
    ]

    # The distinct lookup must restrict by the requested time window so
    # the panel doesn't dump every event we've ever priced off.
    distinct_query = next(
        q for q, _ in fetched_queries
        if "sportradar_tournament_leaderboards" in q
    )
    assert "GROUP BY event_id" in distinct_query


def test_get_tournament_leaderboards_returns_empty_when_no_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No snapshots in the window → empty payload, not an exception.

    The panel renders the "no snapshots recorded yet" empty state off
    this shape so a fresh install doesn't blank the dashboard.
    """

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)
    out = _run(dashboard.get_tournament_leaderboards())
    assert out == {"events": []}


def test_get_tournament_leaderboards_swallows_distinct_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing table / DB hiccup must degrade to ``{"events": []}`` so
    a fresh dev DB without the migration doesn't blank the dashboard."""

    async def _boom(query: str, *args: Any) -> list[dict[str, Any]]:
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(db, "fetch", _boom)
    out = _run(dashboard.get_tournament_leaderboards())
    assert out == {"events": []}


def test_get_tournament_leaderboards_isolates_per_event_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One missing snapshot must not poison the rest — that event still
    appears in the panel with an empty entries list so operators can
    see *which* event failed to render."""
    ts = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    distinct_rows = [
        {"event_id": "sr:tournament:pga-good", "fetched_at": ts},
        {"event_id": "sr:tournament:pga-broken", "fetched_at": ts},
    ]

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_tournament_leaderboards" in query:
            return distinct_rows
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)

    async def _fake_latest(event_id: str) -> list[dict[str, Any]]:
        if event_id == "sr:tournament:pga-broken":
            raise RuntimeError("snapshot read failed")
        return [
            {"position": 1, "name": "Rahm", "fetched_at": ts,
             "event_id": event_id},
        ]

    monkeypatch.setattr(
        sportradar, "latest_tournament_leaderboard", _fake_latest,
    )

    out = _run(dashboard.get_tournament_leaderboards())
    by_id = {e["event_id"]: e for e in out["events"]}
    assert set(by_id) == {"sr:tournament:pga-good", "sr:tournament:pga-broken"}
    assert by_id["sr:tournament:pga-good"]["entries"] == [
        {"position": 1, "name": "Rahm"},
    ]
    assert by_id["sr:tournament:pga-broken"]["entries"] == []


def test_get_tournament_leaderboards_drops_blank_entry_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: blank-name rows must be filtered so the panel never
    renders a position with an empty cell."""
    ts = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_tournament_leaderboards" in query:
            return [{"event_id": "sr:tournament:pga-1", "fetched_at": ts}]
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)

    async def _fake_latest(event_id: str) -> list[dict[str, Any]]:
        return [
            {"position": 1, "name": "", "fetched_at": ts, "event_id": event_id},
            {"position": 2, "name": "Scheffler", "fetched_at": ts,
             "event_id": event_id},
        ]

    monkeypatch.setattr(
        sportradar, "latest_tournament_leaderboard", _fake_latest,
    )
    out = _run(dashboard.get_tournament_leaderboards())
    assert out["events"][0]["entries"] == [{"position": 2, "name": "Scheffler"}]


def test_endpoint_returns_helper_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP route is a thin wrapper — it must surface the helper's
    payload behind the standard approved-user gate."""
    payload: dict[str, Any] = {
        "events": [
            {
                "event_id": "sr:tournament:pga-1",
                "league": "PGA",
                "name": "The Masters",
                "status": "inprogress",
                "fetched_at": "2026-04-27T12:05:00+00:00",
                "entries": [{"position": 1, "name": "Rahm"}],
            }
        ]
    }
    seen_kwargs: dict[str, Any] = {}

    async def _fake(
        *,
        since_hours: int = 168,
        per_event_limit: int = 60,
        max_events: int = 20,
    ) -> dict[str, Any]:
        seen_kwargs["since_hours"] = since_hours
        seen_kwargs["per_event_limit"] = per_event_limit
        seen_kwargs["max_events"] = max_events
        return payload

    monkeypatch.setattr(
        main_module.dashboard, "get_tournament_leaderboards", _fake,
    )

    async def _fake_user() -> AuthedUser:
        return AuthedUser(user_id="op_1", session_id="sess_t", claims={})

    app.dependency_overrides[require_approved_user] = _fake_user
    try:
        client = TestClient(app)
        r = client.get(
            "/api/dashboard/tournament-leaderboards"
            "?since_hours=24&per_event_limit=10&max_events=5"
        )
    finally:
        app.dependency_overrides.pop(require_approved_user, None)

    assert r.status_code == 200, r.text
    assert r.json() == payload
    assert seen_kwargs == {
        "since_hours": 24,
        "per_event_limit": 10,
        "max_events": 5,
    }
