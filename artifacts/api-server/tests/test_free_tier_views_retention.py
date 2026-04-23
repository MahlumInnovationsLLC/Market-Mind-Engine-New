"""Tests for the `free_tier_market_views` retention sweep (task #165).

These verify:
  * `cleanup_once` issues an age-bounded DELETE keyed on the UTC
    ``view_date`` partition (matching the column the quota check
    itself reads), and counts the rows the DB reports back.
  * Env vars override the retention window and sweep cadence, with
    safety floors so a typo can't immediately wipe today's quota
    rows or hammer the DB.
  * `start`/`stop` spin a single background task that calls
    `cleanup_once` at least once and shuts down cleanly.
  * `AXIOM_FREE_TIER_VIEWS_RETENTION_ENABLED=0` short-circuits start.
  * Only stale rows are deleted — today's quota counts are preserved.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import pytest

from app import db as _db
from app import free_tier_views_retention as ftr


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    for k in (
        "AXIOM_FREE_TIER_VIEWS_RETENTION_DAYS",
        "AXIOM_FREE_TIER_VIEWS_CLEANUP_INTERVAL_SECONDS",
        "AXIOM_FREE_TIER_VIEWS_RETENTION_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)
    ftr._reset_state_for_tests()
    yield
    ftr._reset_state_for_tests()


def test_defaults_are_thirty_days_and_daily() -> None:
    assert ftr._retention_days() == 30
    assert ftr._interval_seconds() == 24 * 3600.0


def test_retention_days_floor_protects_against_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AXIOM_FREE_TIER_VIEWS_RETENTION_DAYS", "0")
    assert ftr._retention_days() == 1
    monkeypatch.setenv("AXIOM_FREE_TIER_VIEWS_RETENTION_DAYS", "-7")
    assert ftr._retention_days() == 1


def test_retention_days_handles_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_FREE_TIER_VIEWS_RETENTION_DAYS", "not-a-number"
    )
    assert ftr._retention_days() == ftr.DEFAULT_RETENTION_DAYS


def test_interval_seconds_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AXIOM_FREE_TIER_VIEWS_CLEANUP_INTERVAL_SECONDS", "5"
    )
    # Minimum sweep cadence is 60s so a misconfigured deploy can't
    # busy-loop on the DB.
    assert ftr._interval_seconds() == 60.0


def test_parse_delete_status() -> None:
    assert ftr._parse_delete_status("DELETE 0") == 0
    assert ftr._parse_delete_status("DELETE 42") == 42
    assert ftr._parse_delete_status("") == 0
    assert ftr._parse_delete_status("UPDATE 3") == 0


def test_cleanup_once_issues_age_bounded_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_execute(query: str, *args: Any) -> str:
        captured["query"] = query
        captured["args"] = args
        return "DELETE 11"

    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setenv("AXIOM_FREE_TIER_VIEWS_RETENTION_DAYS", "30")

    deleted = asyncio.run(ftr.cleanup_once())

    assert deleted == 11
    q = captured["query"]
    assert "DELETE FROM free_tier_market_views" in q
    # Must compare against the same UTC DATE column the quota check
    # reads, so the cutoff is naturally aligned to UTC midnight.
    assert "view_date <" in q
    assert "now() AT TIME ZONE 'utc'" in q
    assert "INTERVAL '1 day'" in q
    # Days come through as a positional parameter, not interpolated SQL.
    assert captured["args"] == (30,)
    s = ftr.state()
    assert s["last_deleted"] == 11
    assert s["total_deleted"] == 11
    assert s["last_error"] is None
    assert s["retention_days"] == 30


def test_cleanup_once_swallows_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(_db, "execute", boom)
    deleted = asyncio.run(ftr.cleanup_once())
    assert deleted == 0
    s = ftr.state()
    assert "connection reset" in (s["last_error"] or "")
    assert s["last_deleted"] == 0


def test_total_deleted_accumulates(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = iter(["DELETE 4", "DELETE 1", "DELETE 0"])

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        return next(counts)

    monkeypatch.setattr(_db, "execute", fake_execute)
    asyncio.run(ftr.cleanup_once())
    asyncio.run(ftr.cleanup_once())
    asyncio.run(ftr.cleanup_once())
    s = ftr.state()
    assert s["total_deleted"] == 5
    assert s["last_deleted"] == 0


def test_cleanup_only_drops_stale_rows_and_keeps_recent_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate the actual SQL semantics against an in-memory store
    of `(user_id, view_date, market_id)` rows.  Anything older than
    the retention window must vanish; today's quota row must survive
    so a free caller's daily count isn't reset out from under them.
    """
    today = date(2026, 4, 22)
    rows: set[tuple[str, date, str]] = {
        ("u1", today, "m-today"),                          # keep
        ("u1", today - timedelta(days=29), "m-recent"),    # keep
        ("u1", today - timedelta(days=30), "m-edge"),      # keep (== boundary)
        ("u1", today - timedelta(days=31), "m-stale"),     # delete
        ("u2", today - timedelta(days=120), "m-ancient"),  # delete
    }

    async def fake_execute(query: str, *args: Any) -> str:
        days = args[0]
        cutoff = today - timedelta(days=days)
        before = len(rows)
        for row in list(rows):
            if row[1] < cutoff:
                rows.discard(row)
        return f"DELETE {before - len(rows)}"

    monkeypatch.setattr(_db, "execute", fake_execute)

    deleted = asyncio.run(ftr.cleanup_once())
    assert deleted == 2

    survivors = {(uid, mkt) for (uid, _, mkt) in rows}
    assert ("u1", "m-today") in survivors
    assert ("u1", "m-recent") in survivors
    assert ("u1", "m-edge") in survivors
    assert ("u1", "m-stale") not in survivors
    assert ("u2", "m-ancient") not in survivors


def test_start_runs_loop_and_stop_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        calls.append(1)
        return "DELETE 0"

    monkeypatch.setattr(_db, "execute", fake_execute)
    # Set the floor cadence; we'll cancel before the second tick.
    monkeypatch.setenv(
        "AXIOM_FREE_TIER_VIEWS_CLEANUP_INTERVAL_SECONDS", "60"
    )

    async def go() -> None:
        await ftr.start()
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls, "expected cleanup_once to have run at least once"
        s = ftr.state()
        assert s["running"] is True
        await ftr.stop()
        assert ftr.state()["running"] is False

    asyncio.run(go())


def test_start_respects_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_FREE_TIER_VIEWS_RETENTION_ENABLED", "0")

    async def boom(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("DB must not be touched when disabled")

    monkeypatch.setattr(_db, "execute", boom)

    async def go() -> None:
        await ftr.start()
        assert ftr.state()["running"] is False
        # stop() on an unstarted module is a safe no-op.
        await ftr.stop()

    asyncio.run(go())
