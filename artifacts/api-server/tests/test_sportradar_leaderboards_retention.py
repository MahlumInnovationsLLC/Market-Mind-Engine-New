"""Tests for the `sportradar_tournament_leaderboards` retention sweep
(task #649).

These verify:
  * `cleanup_once` issues the snapshot-rank + age trim DELETE and
    counts the rows the DB reports back.
  * Env vars override the snapshot-count and day windows, with safety
    floors so a typo can't immediately wipe the table.
  * The trim policy keeps the latest snapshot per event_id even when
    the cap is exceeded — exercised against an in-memory shim that
    mirrors the real SQL semantics (the explicit ask from the task).
  * `start`/`stop` spin a single background task that calls
    `cleanup_once` at least once and shuts down cleanly.
  * `AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED=0`
    short-circuits start.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app import sportradar_leaderboards_retention as srlr


_ENV_KEYS = (
    "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS",
    "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS",
    "AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS",
    "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED",
)


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    srlr._reset_state_for_tests()
    yield
    srlr._reset_state_for_tests()


def test_defaults_match_spec() -> None:
    assert srlr._min_snapshots() == srlr.DEFAULT_MIN_SNAPSHOTS == 500
    assert srlr._retention_days() == srlr.DEFAULT_RETENTION_DAYS == 90
    assert srlr._interval_seconds() == float(srlr.DEFAULT_INTERVAL_SECONDS)
    assert srlr._enabled_env() is True


def test_min_snapshots_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS", "0"
    )
    assert srlr._min_snapshots() == 1
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS", "-7"
    )
    assert srlr._min_snapshots() == 1


def test_retention_days_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS", "0"
    )
    assert srlr._retention_days() == 1
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS", "-1"
    )
    assert srlr._retention_days() == 1


def test_handles_garbage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS",
        "not-a-number",
    )
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS", ""
    )
    assert srlr._min_snapshots() == srlr.DEFAULT_MIN_SNAPSHOTS
    assert srlr._retention_days() == srlr.DEFAULT_RETENTION_DAYS


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS", "12"
    )
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS", "30"
    )
    policy = srlr.retention_policy()
    assert policy["min_snapshots"] == 12
    assert policy["retention_days"] == 30


def test_interval_seconds_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS", "10"
    )
    # 5-minute floor so a misconfigured deploy can't hammer the DB.
    assert srlr._interval_seconds() == 300.0


def test_enabled_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED", "0"
    )
    assert srlr._enabled_env() is False


def test_parse_delete_status() -> None:
    assert srlr._parse_delete_status("DELETE 0") == 0
    assert srlr._parse_delete_status("DELETE 137") == 137
    assert srlr._parse_delete_status("") == 0
    assert srlr._parse_delete_status("UPDATE 3") == 0


def test_cleanup_once_issues_trim_query(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_execute(query: str, *args: Any) -> str:
        captured.append((query, args))
        return "DELETE 42"

    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS", "100"
    )
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS", "60"
    )

    deleted = asyncio.run(srlr.cleanup_once())

    assert deleted == 42
    assert len(captured) == 1
    query, args = captured[0]
    assert "DELETE FROM sportradar_tournament_leaderboards" in query
    assert "row_number()" in query
    assert "PARTITION BY event_id" in query
    assert "ORDER BY fetched_at DESC" in query
    assert "DISTINCT event_id, fetched_at" in query
    assert "INTERVAL '1 day'" in query
    assert args == (100, 60)

    s = srlr.state()
    assert s["last_deleted"] == 42
    assert s["total_deleted"] == 42
    assert s["last_error"] is None
    assert s["last_tick_at"] is not None
    assert s["policy"] == {"min_snapshots": 100, "retention_days": 60}


def test_cleanup_once_swallows_db_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(_db, "execute", fake_execute)

    deleted = asyncio.run(srlr.cleanup_once())
    assert deleted == 0
    s = srlr.state()
    assert s["last_deleted"] == 0
    assert s["total_deleted"] == 0
    assert "connection reset" in (s["last_error"] or "")


def test_total_deleted_accumulates(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = iter(["DELETE 5", "DELETE 9"])

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        return next(counts)

    monkeypatch.setattr(_db, "execute", fake_execute)
    asyncio.run(srlr.cleanup_once())
    asyncio.run(srlr.cleanup_once())
    s = srlr.state()
    assert s["total_deleted"] == 14
    assert s["last_deleted"] == 9


# ---------------------------------------------------------------------------
# In-memory simulation of the trim SQL — verifies the trim *policy* does the
# right thing (latest snapshot per event_id always kept, old beyond-N
# snapshots deleted) end-to-end without a real Postgres.  Mirrors the SQL
# in `_TRIM_SQL`.
# ---------------------------------------------------------------------------


class _FakeLeaderboardTable:
    """Tiny in-memory shim that mirrors `_TRIM_SQL` semantics.

    Each row carries ``{event_id, fetched_at, position, name}``;
    snapshots are identified by ``(event_id, fetched_at)``.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def execute(self, query: str, *args: Any) -> str:
        assert "DELETE FROM sportradar_tournament_leaderboards" in query
        min_snapshots, retention_days = int(args[0]), int(args[1])
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        # Build the distinct snapshot set per event_id, ranked DESC by
        # fetched_at.  Mirrors the CTE in the real SQL.
        snapshots_by_event: dict[str, set[datetime]] = {}
        for r in self.rows:
            snapshots_by_event.setdefault(r["event_id"], set()).add(
                r["fetched_at"]
            )

        deletable_snapshots: set[tuple[str, datetime]] = set()
        for event_id, fetched_set in snapshots_by_event.items():
            ordered = sorted(fetched_set, reverse=True)
            for rn, fetched_at in enumerate(ordered, start=1):
                if rn > min_snapshots:
                    deletable_snapshots.add((event_id, fetched_at))

        before = len(self.rows)
        self.rows = [
            r for r in self.rows
            if not (
                (r["event_id"], r["fetched_at"]) in deletable_snapshots
                and r["fetched_at"] < cutoff
            )
        ]
        deleted = before - len(self.rows)
        return f"DELETE {deleted}"


def _make_snapshot(
    event_id: str,
    fetched_at: datetime,
    entrants: int,
    next_id: int,
) -> tuple[list[dict[str, Any]], int]:
    """Build one snapshot's worth of rows (one per entrant)."""
    rows = [
        {
            "id": next_id + i,
            "event_id": event_id,
            "fetched_at": fetched_at,
            "position": i + 1,
            "name": f"player-{i + 1}",
        }
        for i in range(entrants)
    ]
    return rows, next_id + entrants


def test_pruner_keeps_latest_snapshot_per_event_when_cap_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trim policy must always preserve the *latest* snapshot per
    event_id, even when the snapshot cap is exceeded.  This is the
    explicit acceptance criterion called out in the task spec.
    """
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS", "2"
    )
    # Day window so tight that recency alone won't save older snapshots
    # — this forces the snapshot-rank guard to do all the keeping.
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS", "1"
    )

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    next_id = 1
    # Two events, each with 5 snapshots taken 30 days apart so every
    # snapshot is well outside the 1-day age window.  Entrant counts
    # vary so we can verify the policy operates per-snapshot, not
    # per-row.
    pga_snapshots = [now - timedelta(days=30 * i) for i in range(5)]
    f1_snapshots = [now - timedelta(days=30 * i + 1) for i in range(5)]
    for ts in pga_snapshots:
        snap_rows, next_id = _make_snapshot("pga:event-1", ts, 150, next_id)
        rows.extend(snap_rows)
    for ts in f1_snapshots:
        snap_rows, next_id = _make_snapshot("f1:event-2", ts, 20, next_id)
        rows.extend(snap_rows)

    fake = _FakeLeaderboardTable(rows)
    monkeypatch.setattr(_db, "execute", fake.execute)

    deleted = asyncio.run(srlr.cleanup_once())

    # 5 snapshots per event minus the top-2 = 3 stale snapshots per
    # event * 2 events.  PGA snapshots have 150 entrants each; F1 have
    # 20.  Total deleted rows = 3 * 150 + 3 * 20 = 510.
    assert deleted == 510

    # Every event_id retains exactly 2 distinct snapshot timestamps —
    # the most-recent two — and no rows from any older snapshot
    # survive.
    by_event_kept: dict[str, set[datetime]] = {}
    for r in fake.rows:
        by_event_kept.setdefault(r["event_id"], set()).add(r["fetched_at"])

    assert by_event_kept["pga:event-1"] == set(pga_snapshots[:2])
    assert by_event_kept["f1:event-2"] == set(f1_snapshots[:2])

    # The strongest form of the acceptance criterion: the single
    # latest snapshot per event_id is always preserved.
    assert max(by_event_kept["pga:event-1"]) == pga_snapshots[0]
    assert max(by_event_kept["f1:event-2"]) == f1_snapshots[0]
    # And every entrant-row of the latest snapshot is intact (no
    # partial-snapshot deletion).
    latest_pga_rows = [
        r for r in fake.rows
        if r["event_id"] == "pga:event-1" and r["fetched_at"] == pga_snapshots[0]
    ]
    assert len(latest_pga_rows) == 150


def test_pruner_under_cap_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pair with fewer snapshots than the cap is left entirely alone,
    even when every snapshot is older than the day window."""
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS", "100"
    )
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS", "1"
    )

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    next_id = 1
    for i in range(10):
        snap_rows, next_id = _make_snapshot(
            "pga:cold-event",
            now - timedelta(days=400 + i),
            50,
            next_id,
        )
        rows.extend(snap_rows)
    fake = _FakeLeaderboardTable(rows)
    monkeypatch.setattr(_db, "execute", fake.execute)

    deleted = asyncio.run(srlr.cleanup_once())
    # All 10 snapshots are older than 1d, but only 10 < cap=100 so
    # none have rn > 100; nothing qualifies for deletion.
    assert deleted == 0
    assert len(fake.rows) == 500


def test_pruner_recent_snapshots_always_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with a tiny min_snapshots, anything inside the day window
    is kept (the age guard saves recent activity)."""
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_MIN_SNAPSHOTS", "1"
    )
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_DAYS", "30"
    )

    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    next_id = 1
    # 10 snapshots in the last 10 days — all inside the 30d window.
    for i in range(10):
        snap_rows, next_id = _make_snapshot(
            "pga:fresh-event",
            now - timedelta(days=i),
            25,
            next_id,
        )
        rows.extend(snap_rows)
    fake = _FakeLeaderboardTable(rows)
    monkeypatch.setattr(_db, "execute", fake.execute)

    deleted = asyncio.run(srlr.cleanup_once())
    # 9 snapshots have rn > 1, but every snapshot is inside the 30d
    # window so the age guard saves them all.
    assert deleted == 0
    assert len(fake.rows) == 250


# ---------------------------------------------------------------------------
# Background loop start/stop
# ---------------------------------------------------------------------------


def test_start_runs_loop_and_stop_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        calls.append(1)
        return "DELETE 0"

    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_CLEANUP_INTERVAL_SECONDS", "300"
    )

    async def go() -> None:
        await srlr.start()
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls, "expected cleanup_once to have run at least once"
        s = srlr.state()
        assert s["running"] is True
        await srlr.stop()
        assert srlr.state()["running"] is False

    asyncio.run(go())


def test_start_respects_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_SPORTRADAR_LEADERBOARDS_RETENTION_ENABLED", "0"
    )

    async def boom(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("DB must not be touched when disabled")

    monkeypatch.setattr(_db, "execute", boom)

    async def go() -> None:
        await srlr.start()
        assert srlr.state()["running"] is False
        await srlr.stop()

    asyncio.run(go())
