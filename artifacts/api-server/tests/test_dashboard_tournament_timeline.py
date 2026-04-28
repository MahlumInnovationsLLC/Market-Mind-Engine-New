"""Tests for the per-event tournament leaderboard timeline panel
(task #698).

The helper walks every snapshot stored for one event_id, groups rows
by ``fetched_at``, and joins each position-ordered roster with the
Plackett-Luce probability the orchestrator priced off.  We stub the
underlying ``db`` calls to exercise the shaping logic without touching
a real Postgres.

Coverage:
  * Every saved snapshot is grouped by ``fetched_at`` and the roster
    rendered in position order with PL probabilities derived through
    the same pipeline the live cycle uses.
  * Schedule metadata (league + display name + status) is joined so
    the panel header reads ``PGA · The Masters`` instead of a raw id.
  * Empty / missing-table degrades to ``snapshots: []`` rather than
    raising — the panel renders the empty state.
  * A snapshot too thin to drive Plackett-Luce surfaces with
    ``probability=None`` rather than a degenerate uniform prior.
  * Blank-name rows are filtered so the panel never renders an empty
    name cell.
  * The HTTP endpoint round-trips the helper payload and forwards the
    ``event_id`` query param behind the standard approved-user gate.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import dashboard, db, main as main_module
from app.auth import AuthedUser, require_approved_user
from app.main import app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_timeline_groups_snapshots_with_plackett_luce_probabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: rows for one event_id are grouped by fetched_at,
    each snapshot keeps its position-ordered roster, and every entry
    carries the PL probability derived from that snapshot's field."""
    ts1 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 4, 27, 12, 5, tzinfo=timezone.utc)

    leaderboard_rows = [
        {"position": 1, "name": "Rahm", "fetched_at": ts1},
        {"position": 2, "name": "Scheffler", "fetched_at": ts1},
        {"position": 3, "name": "Morikawa", "fetched_at": ts1},
        {"position": 1, "name": "Scheffler", "fetched_at": ts2},
        {"position": 2, "name": "Rahm", "fetched_at": ts2},
        {"position": 3, "name": "Morikawa", "fetched_at": ts2},
    ]
    schedule_rows = [
        {
            "league": "PGA",
            "home_team": "The Masters",
            "away_team": "",
            "status": "inprogress",
        },
    ]

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_tournament_leaderboards" in query:
            return leaderboard_rows
        if "sportradar_schedules" in query:
            return schedule_rows
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)

    out = _run(
        dashboard.get_tournament_leaderboard_timeline("sr:tournament:pga-1"),
    )

    assert out["event_id"] == "sr:tournament:pga-1"
    assert out["league"] == "PGA"
    assert out["name"] == "The Masters"
    assert out["status"] == "inprogress"

    snapshots = out["snapshots"]
    assert len(snapshots) == 2
    assert snapshots[0]["fetched_at"] == ts1.isoformat()
    assert snapshots[1]["fetched_at"] == ts2.isoformat()

    # Position-ordered roster preserved per snapshot.
    assert [(e["position"], e["name"]) for e in snapshots[0]["entries"]] == [
        (1, "Rahm"), (2, "Scheffler"), (3, "Morikawa"),
    ]
    assert [(e["position"], e["name"]) for e in snapshots[1]["entries"]] == [
        (1, "Scheffler"), (2, "Rahm"), (3, "Morikawa"),
    ]

    # Every entry carries a finite probability and each snapshot's
    # probabilities sum to ~1.0 (Plackett-Luce simplex).
    for snap in snapshots:
        probs = [e["probability"] for e in snap["entries"]]
        assert all(p is not None and 0.0 < p < 1.0 for p in probs)
        assert abs(sum(probs) - 1.0) < 1e-9

    # When the leader flips between snapshots the highest probability
    # follows them — that's the whole point of replaying mid-event.
    snap1_by_name = {e["name"]: e["probability"] for e in snapshots[0]["entries"]}
    snap2_by_name = {e["name"]: e["probability"] for e in snapshots[1]["entries"]}
    assert snap1_by_name["Rahm"] > snap1_by_name["Scheffler"]
    assert snap2_by_name["Scheffler"] > snap2_by_name["Rahm"]


def test_timeline_returns_empty_shape_when_no_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rows for the event id → empty snapshots list, schedule meta
    still surfaced so the header can render something useful."""
    schedule_rows = [
        {
            "league": "F1",
            "home_team": "Miami GP",
            "away_team": "",
            "status": "scheduled",
        },
    ]

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_tournament_leaderboards" in query:
            return []
        if "sportradar_schedules" in query:
            return schedule_rows
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)
    out = _run(
        dashboard.get_tournament_leaderboard_timeline("sr:race:f1-1"),
    )
    assert out == {
        "event_id": "sr:race:f1-1",
        "league": "F1",
        "name": "Miami GP",
        "status": "scheduled",
        "snapshots": [],
    }


def test_timeline_swallows_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing table / DB hiccup must degrade to the empty shape so a
    fresh dev DB without the migration doesn't blank the panel."""

    async def _boom(query: str, *args: Any) -> list[dict[str, Any]]:
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(db, "fetch", _boom)
    out = _run(
        dashboard.get_tournament_leaderboard_timeline("sr:tournament:pga-1"),
    )
    assert out["snapshots"] == []
    assert out["event_id"] == "sr:tournament:pga-1"


def test_timeline_handles_thin_snapshot_without_degenerate_probabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot with <2 named entrants can't drive Plackett-Luce — it
    still surfaces in the timeline but its entries carry
    ``probability=None`` so the panel doesn't fake a uniform prior.
    """
    ts1 = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 4, 27, 12, 5, tzinfo=timezone.utc)

    leaderboard_rows = [
        # Thin snapshot — only one entry survives the ratings filter.
        {"position": 1, "name": "Lone Wolf", "fetched_at": ts1},
        # Healthy snapshot — two entries → real probabilities.
        {"position": 1, "name": "Rahm", "fetched_at": ts2},
        {"position": 2, "name": "Scheffler", "fetched_at": ts2},
    ]

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_tournament_leaderboards" in query:
            return leaderboard_rows
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)
    out = _run(
        dashboard.get_tournament_leaderboard_timeline("sr:tournament:pga-1"),
    )
    snaps = out["snapshots"]
    assert len(snaps) == 2
    assert snaps[0]["entries"] == [
        {"position": 1, "name": "Lone Wolf", "probability": None},
    ]
    healthy_probs = [e["probability"] for e in snaps[1]["entries"]]
    assert all(p is not None for p in healthy_probs)
    assert abs(sum(healthy_probs) - 1.0) < 1e-9


def test_timeline_drops_blank_name_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: blank-name rows must be filtered so the panel never
    renders a row with no name."""
    ts = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)

    leaderboard_rows = [
        {"position": 1, "name": "", "fetched_at": ts},
        {"position": 2, "name": "Scheffler", "fetched_at": ts},
        {"position": 3, "name": "Rahm", "fetched_at": ts},
    ]

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_tournament_leaderboards" in query:
            return leaderboard_rows
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)
    out = _run(
        dashboard.get_tournament_leaderboard_timeline("sr:tournament:pga-1"),
    )
    snap = out["snapshots"][0]
    assert [(e["position"], e["name"]) for e in snap["entries"]] == [
        (2, "Scheffler"), (3, "Rahm"),
    ]


def test_timeline_returns_empty_for_empty_event_id() -> None:
    """An empty/blank event id short-circuits to the empty shape so
    the panel can render gracefully before the user picks a tournament.
    """
    out = _run(dashboard.get_tournament_leaderboard_timeline(""))
    assert out["event_id"] == ""
    assert out["snapshots"] == []


def test_timeline_returns_every_saved_snapshot_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators must be able to scrub the entire saved history of a
    long-running tournament — the helper must not silently truncate
    when no explicit ``max_snapshots`` is requested.

    Regression guard for the original 240-snapshot cap that hid
    earlier snapshots from a marathon multi-week tournament.
    """
    base = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
    leaderboard_rows: list[dict[str, Any]] = []
    snapshot_count = 500  # comfortably past any short cap
    for i in range(snapshot_count):
        ts = datetime(
            base.year, base.month, base.day, 0, 0, tzinfo=timezone.utc,
        ).replace(minute=i % 60, hour=(i // 60) % 24)
        # Use a unique microsecond per snapshot to guarantee distinct
        # fetched_at values.
        ts = ts.replace(microsecond=i)
        leaderboard_rows.append(
            {"position": 1, "name": "Rahm", "fetched_at": ts},
        )
        leaderboard_rows.append(
            {"position": 2, "name": "Scheffler", "fetched_at": ts},
        )

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_tournament_leaderboards" in query:
            return leaderboard_rows
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)
    out = _run(
        dashboard.get_tournament_leaderboard_timeline("sr:tournament:pga-1"),
    )
    assert len(out["snapshots"]) == snapshot_count, (
        "default call must return every saved snapshot — found "
        f"{len(out['snapshots'])} of {snapshot_count}."
    )


def test_timeline_honors_explicit_max_snapshots_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that *want* a bounded slice can pass ``max_snapshots``;
    the cap keeps the most-recent N snapshots so the trailing window
    matches what an operator usually cares about.
    """
    base = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    leaderboard_rows: list[dict[str, Any]] = []
    for i in range(10):
        ts = base.replace(minute=i)
        leaderboard_rows.append(
            {"position": 1, "name": f"E{i}-1", "fetched_at": ts},
        )
        leaderboard_rows.append(
            {"position": 2, "name": f"E{i}-2", "fetched_at": ts},
        )

    async def _fake_fetch(query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_tournament_leaderboards" in query:
            return leaderboard_rows
        return []

    monkeypatch.setattr(db, "fetch", _fake_fetch)
    out = _run(
        dashboard.get_tournament_leaderboard_timeline(
            "sr:tournament:pga-1", max_snapshots=3,
        ),
    )
    assert len(out["snapshots"]) == 3
    # The trailing window must be the most recent snapshots, not the
    # first three — a cap that drops the latest snapshots would defeat
    # the point.
    assert [s["fetched_at"] for s in out["snapshots"]] == [
        base.replace(minute=7).isoformat(),
        base.replace(minute=8).isoformat(),
        base.replace(minute=9).isoformat(),
    ]


def test_endpoint_forwards_event_id_and_returns_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP route is a thin wrapper — it must surface the helper's
    payload behind the standard approved-user gate, and forward
    ``event_id`` + the snapshot caps."""
    payload: dict[str, Any] = {
        "event_id": "sr:tournament:pga-1",
        "league": "PGA",
        "name": "The Masters",
        "status": "inprogress",
        "snapshots": [
            {
                "fetched_at": "2026-04-27T12:00:00+00:00",
                "entries": [
                    {"position": 1, "name": "Rahm", "probability": 0.7},
                    {"position": 2, "name": "Scheffler", "probability": 0.3},
                ],
            }
        ],
    }
    seen: dict[str, Any] = {}

    async def _fake(
        event_id: str,
        *,
        max_snapshots: int | None = None,
        per_snapshot_limit: int = 200,
    ) -> dict[str, Any]:
        seen["event_id"] = event_id
        seen["max_snapshots"] = max_snapshots
        seen["per_snapshot_limit"] = per_snapshot_limit
        return payload

    monkeypatch.setattr(
        main_module.dashboard, "get_tournament_leaderboard_timeline", _fake,
    )

    async def _fake_user() -> AuthedUser:
        return AuthedUser(user_id="op_1", session_id="sess_t", claims={})

    app.dependency_overrides[require_approved_user] = _fake_user
    try:
        client = TestClient(app)
        r = client.get(
            "/api/dashboard/tournament-leaderboard-timeline"
            "?event_id=sr:tournament:pga-1"
            "&max_snapshots=50&per_snapshot_limit=20"
        )
    finally:
        app.dependency_overrides.pop(require_approved_user, None)

    assert r.status_code == 200, r.text
    assert r.json() == payload
    assert seen == {
        "event_id": "sr:tournament:pga-1",
        "max_snapshots": 50,
        "per_snapshot_limit": 20,
    }


def test_endpoint_requires_event_id() -> None:
    """The endpoint must reject calls that omit ``event_id`` — without
    it the helper has nothing to look up.
    """

    async def _fake_user() -> AuthedUser:
        return AuthedUser(user_id="op_1", session_id="sess_t", claims={})

    app.dependency_overrides[require_approved_user] = _fake_user
    try:
        client = TestClient(app)
        r = client.get("/api/dashboard/tournament-leaderboard-timeline")
    finally:
        app.dependency_overrides.pop(require_approved_user, None)

    assert r.status_code == 422, r.text
