"""Tests for the `crypto_backtest_runs` retention sweep (task #582).

These verify:
  * `cleanup_once` issues the row-count + age trim DELETE and counts
    the rows the DB reports back.
  * Env vars override the row-count and day windows, with safety
    floors so a typo can't immediately wipe the table.
  * The trim policy keeps recent rows and the top-N per pair while
    deleting old beyond-N rows — exercised against an in-memory shim
    that mirrors the real SQL semantics.
  * `start`/`stop` spin a single background task that calls
    `cleanup_once` at least once and shuts down cleanly.
  * `AXIOM_CRYPTO_BACKTEST_RETENTION_ENABLED=0` short-circuits start.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import crypto_backtest_retention as cbr
from app import db as _db


_ENV_KEYS = (
    "AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS",
    "AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS",
    "AXIOM_CRYPTO_BACKTEST_CLEANUP_INTERVAL_SECONDS",
    "AXIOM_CRYPTO_BACKTEST_RETENTION_ENABLED",
)


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    cbr._reset_state_for_tests()
    yield
    cbr._reset_state_for_tests()


def test_defaults_match_spec() -> None:
    assert cbr._min_rows() == cbr.DEFAULT_MIN_ROWS == 50
    assert cbr._retention_days() == cbr.DEFAULT_RETENTION_DAYS == 90
    assert cbr._interval_seconds() == float(cbr.DEFAULT_INTERVAL_SECONDS)
    assert cbr._enabled_env() is True


def test_min_rows_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS", "0")
    assert cbr._min_rows() == 1
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS", "-7")
    assert cbr._min_rows() == 1


def test_retention_days_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS", "0")
    assert cbr._retention_days() == 1
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS", "-1")
    assert cbr._retention_days() == 1


def test_handles_garbage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS", "not-a-number")
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS", "")
    assert cbr._min_rows() == cbr.DEFAULT_MIN_ROWS
    assert cbr._retention_days() == cbr.DEFAULT_RETENTION_DAYS


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS", "20")
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS", "30")
    policy = cbr.retention_policy()
    assert policy["min_rows"] == 20
    assert policy["retention_days"] == 30


def test_interval_seconds_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_CLEANUP_INTERVAL_SECONDS", "10")
    # 5-minute floor so a misconfigured deploy can't hammer the DB.
    assert cbr._interval_seconds() == 300.0


def test_enabled_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_ENABLED", "0")
    assert cbr._enabled_env() is False


def test_parse_delete_status() -> None:
    assert cbr._parse_delete_status("DELETE 0") == 0
    assert cbr._parse_delete_status("DELETE 42") == 42
    assert cbr._parse_delete_status("") == 0
    assert cbr._parse_delete_status("UPDATE 3") == 0


def test_cleanup_once_issues_trim_query(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_execute(query: str, *args: Any) -> str:
        captured.append((query, args))
        return "DELETE 7"

    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS", "25")
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS", "60")

    deleted = asyncio.run(cbr.cleanup_once())

    assert deleted == 7
    assert len(captured) == 1
    query, args = captured[0]
    assert "DELETE FROM crypto_backtest_runs" in query
    assert "row_number()" in query
    assert "PARTITION BY symbol, timeframe" in query
    assert "ORDER BY completed_at DESC" in query
    assert "INTERVAL '1 day'" in query
    assert args == (25, 60)

    s = cbr.state()
    assert s["last_deleted"] == 7
    assert s["total_deleted"] == 7
    assert s["last_error"] is None
    assert s["last_tick_at"] is not None
    assert s["policy"] == {"min_rows": 25, "retention_days": 60}


def test_cleanup_once_swallows_db_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(_db, "execute", fake_execute)

    deleted = asyncio.run(cbr.cleanup_once())
    assert deleted == 0
    s = cbr.state()
    assert s["last_deleted"] == 0
    assert s["total_deleted"] == 0
    assert "connection reset" in (s["last_error"] or "")


def test_total_deleted_accumulates(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = iter(["DELETE 3", "DELETE 4"])

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        return next(counts)

    monkeypatch.setattr(_db, "execute", fake_execute)
    asyncio.run(cbr.cleanup_once())
    asyncio.run(cbr.cleanup_once())
    s = cbr.state()
    assert s["total_deleted"] == 7
    assert s["last_deleted"] == 4


# ---------------------------------------------------------------------------
# In-memory simulation of the trim SQL — verifies the trim *policy* does the
# right thing (recent + top-N kept, older-and-beyond-N deleted) end-to-end
# without a real Postgres.  Mirrors the SQL in `_TRIM_SQL`.
# ---------------------------------------------------------------------------


class _FakeBacktestRunsTable:
    """Tiny in-memory shim that mirrors `_TRIM_SQL` semantics."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        # Each row: {id, symbol, timeframe, completed_at}
        self.rows = rows

    async def execute(self, query: str, *args: Any) -> str:
        assert "DELETE FROM crypto_backtest_runs" in query
        min_rows, retention_days = int(args[0]), int(args[1])
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        # Group by (symbol, timeframe), order by completed_at DESC, id DESC,
        # mark rn > min_rows AND completed_at < cutoff for delete.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in self.rows:
            groups.setdefault((r["symbol"], r["timeframe"]), []).append(r)
        to_delete: set[int] = set()
        for _key, group in groups.items():
            group.sort(key=lambda r: (r["completed_at"], r["id"]), reverse=True)
            for rn, r in enumerate(group, start=1):
                if rn > min_rows and r["completed_at"] < cutoff:
                    to_delete.add(r["id"])
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["id"] not in to_delete]
        deleted = before - len(self.rows)
        return f"DELETE {deleted}"


def _make_rows() -> list[dict[str, Any]]:
    """Two pairs, mixed ages.  See assertions for expected outcome."""
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    next_id = 1
    # BTC/1h: 8 rows total — 5 recent (within 30d) + 3 ancient (200d ago).
    for i in range(5):
        rows.append({
            "id": next_id,
            "symbol": "BTC", "timeframe": "1h",
            "completed_at": now - timedelta(days=i),  # 0..4 days old
        })
        next_id += 1
    for i in range(3):
        rows.append({
            "id": next_id,
            "symbol": "BTC", "timeframe": "1h",
            "completed_at": now - timedelta(days=200 + i),
        })
        next_id += 1
    # ETH/15m: 6 rows — all > retention_days old, but only 6 total so
    # the top-N=5 floor still keeps 5 of them.
    for i in range(6):
        rows.append({
            "id": next_id,
            "symbol": "ETH", "timeframe": "15m",
            "completed_at": now - timedelta(days=120 + i),
        })
        next_id += 1
    return rows


def test_trim_policy_keeps_recent_and_top_n(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: cleanup_once via shim trims old-beyond-top-N only."""
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS", "5")
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS", "90")

    fake = _FakeBacktestRunsTable(_make_rows())
    monkeypatch.setattr(_db, "execute", fake.execute)

    deleted = asyncio.run(cbr.cleanup_once())

    # BTC/1h had 5 recent + 3 ancient. Top-5 are the recent ones, so
    # the 3 ancient rows fall outside both bands and should be deleted.
    # ETH/15m had 6 ancient; top-5 stay (row-count floor wins), the
    # 6th (oldest) is the only one outside both bands.
    assert deleted == 4

    surviving_btc = [
        r for r in fake.rows if r["symbol"] == "BTC" and r["timeframe"] == "1h"
    ]
    surviving_eth = [
        r for r in fake.rows if r["symbol"] == "ETH" and r["timeframe"] == "15m"
    ]
    assert len(surviving_btc) == 5
    assert len(surviving_eth) == 5
    # Every surviving BTC row is one of the recent (id 1..5) ones.
    assert {r["id"] for r in surviving_btc} == {1, 2, 3, 4, 5}
    # Surviving ETH rows are the 5 most-recent of the 6 — i.e. all but
    # the oldest (the highest-id, which is the oldest given how we
    # built the fixture).
    eth_ids = sorted(r["id"] for r in surviving_eth)
    assert eth_ids == [9, 10, 11, 12, 13]


def test_trim_policy_short_pair_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pair with fewer rows than min_rows is left entirely alone, even
    when every row is older than the day window."""
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS", "50")
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS", "30")

    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": i + 1,
            "symbol": "SOL", "timeframe": "1h",
            "completed_at": now - timedelta(days=400 + i),
        }
        for i in range(10)
    ]
    fake = _FakeBacktestRunsTable(rows)
    monkeypatch.setattr(_db, "execute", fake.execute)

    deleted = asyncio.run(cbr.cleanup_once())
    # All 10 rows are older than 30d, but the row-count floor (50)
    # means none of them have rn > 50, so none qualify for deletion.
    assert deleted == 0
    assert len(fake.rows) == 10


def test_trim_policy_recent_rows_always_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with a tiny min_rows, anything inside the day window is kept."""
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_MIN_ROWS", "1")
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_DAYS", "30")

    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": i + 1,
            "symbol": "BTC", "timeframe": "1h",
            "completed_at": now - timedelta(days=i),  # 0..9 days old, all fresh
        }
        for i in range(10)
    ]
    fake = _FakeBacktestRunsTable(rows)
    monkeypatch.setattr(_db, "execute", fake.execute)

    deleted = asyncio.run(cbr.cleanup_once())
    # All inside the 30d window, so nothing is deleted regardless of
    # the min_rows=1 cap — the age guard saves them.
    assert deleted == 0
    assert len(fake.rows) == 10


# ---------------------------------------------------------------------------
# Background loop start/stop
# ---------------------------------------------------------------------------


def test_start_runs_loop_and_stop_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        calls.append(1)
        return "DELETE 0"

    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_CLEANUP_INTERVAL_SECONDS", "300")

    async def go() -> None:
        await cbr.start()
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls, "expected cleanup_once to have run at least once"
        s = cbr.state()
        assert s["running"] is True
        await cbr.stop()
        assert cbr.state()["running"] is False

    asyncio.run(go())


def test_start_respects_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_CRYPTO_BACKTEST_RETENTION_ENABLED", "0")

    async def boom(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("DB must not be touched when disabled")

    monkeypatch.setattr(_db, "execute", boom)

    async def go() -> None:
        await cbr.start()
        assert cbr.state()["running"] is False
        await cbr.stop()

    asyncio.run(go())
