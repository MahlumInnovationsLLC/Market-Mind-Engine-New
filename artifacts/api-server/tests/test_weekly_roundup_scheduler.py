"""Tests for the weekly P&L roundup scheduler.

Covers the three behaviours added alongside the Friday 22:00 UTC digest:

  * Day-of-week / time gating — the loop only fires on the configured
    weekday after the configured wall-clock minute, and respects the
    `WEEKLY_ROUNDUP_DOW` / `_HOUR_UTC` / `_MINUTE_UTC` overrides.
  * ISO-week dedupe — a process restart inside the same Friday window
    cannot double-send because `alerts.dedupe_or_skip` claims a key
    of the form ``weekly_roundup:<ISO-week>`` for 7 days.
  * `collect_summary` aggregation — the realized-P&L query is bounded
    to the trailing 7 days ending at ``now``, and the function
    degrades gracefully when the underlying query fails.

The asyncpg pool is never opened — the dedupe shim from
``test_alert_dedupe`` is reused via a small in-memory ``_FakeStore``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.notifications import weekly_roundup as _wr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeDedupeStore:
    """Mirrors the `notification_dedupe` ON CONFLICT semantics used by
    `alerts.dedupe_or_skip` so we can assert that the scheduler is
    correctly serialising itself across restarts."""

    def __init__(self) -> None:
        self.rows: dict[str, datetime] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "notification_dedupe" in query, query
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        existing = self.rows.get(key)
        if existing is None or existing <= now:
            self.rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None


def _freeze_now(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    """Patch `datetime` inside the weekly_roundup module so that
    `datetime.now(...)` returns ``when``. The scheduler only consults
    `datetime.now()`, so a thin shim is enough."""

    class _FrozenDT:
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            return when

    monkeypatch.setattr(_wr, "datetime", _FrozenDT)


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeDedupeStore:
    store = _FakeDedupeStore()
    monkeypatch.setattr(_db, "fetchrow", store.fetchrow)
    # Reset module-level scheduler state so tests don't bleed.
    _wr._last_sent_week = None
    _alerts.reset_suppression_counts()
    return store


async def _drive_one_iteration() -> None:
    """Run the scheduler loop body exactly once.

    The loop body is a (possibly skipped) DB await followed by
    `wait_for(_stop.wait(), 60s)` — once we signal stop the wait
    returns and the loop exits via its `else: return`.
    """
    # Fresh Event bound to whichever loop `asyncio.run` just spun up.
    _wr._stop = asyncio.Event()
    task = asyncio.create_task(_wr._loop())
    # Yield enough times for the awaited dedupe call to complete and for
    # execution to park at the inner `wait_for(_stop.wait(), 60s)` line.
    for _ in range(20):
        await asyncio.sleep(0)
    _wr._stop.set()
    await asyncio.wait_for(task, timeout=2.0)


# ---------------------------------------------------------------------------
# ISO-week key + 7-day window helpers
# ---------------------------------------------------------------------------


def test_iso_week_key_matches_isocalendar() -> None:
    """The key is what we use as the dedupe identifier — it must agree
    with the stdlib's own ISO-week calculation, including the
    `52->W01` rollover at year end."""
    when = datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc)
    iso = when.isocalendar()
    assert _wr._iso_week_key(when) == f"{iso[0]:04d}-W{iso[1]:02d}"

    # Late-December dates whose ISO year rolls forward (W53 / W01 of
    # the next year) must keep the key consistent with isocalendar().
    edge = datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc)
    edge_iso = edge.isocalendar()
    assert _wr._iso_week_key(edge) == f"{edge_iso[0]:04d}-W{edge_iso[1]:02d}"


def test_week_window_utc_spans_exactly_seven_days() -> None:
    when = datetime(2026, 4, 24, 22, 0, 0, tzinfo=timezone.utc)
    start_iso, end_iso = _wr._week_window_utc(when)
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    assert end == when
    assert end - start == timedelta(days=7)


# ---------------------------------------------------------------------------
# Day-of-week + time gating
# ---------------------------------------------------------------------------


def test_does_not_fire_on_wrong_day_of_week(
    monkeypatch: pytest.MonkeyPatch, fake_store: _FakeDedupeStore
) -> None:
    fired: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fired.append(1)
        return {"queued": True}

    monkeypatch.setattr(_wr, "fire_now", _fake_fire_now)
    # Thursday 22:00 UTC — DOW 3, not the configured 4 (Friday).
    _freeze_now(monkeypatch, datetime(2026, 4, 23, 22, 0, tzinfo=timezone.utc))

    asyncio.run(_drive_one_iteration())

    assert fired == []
    # Persistent dedupe was never even attempted.
    assert fake_store.rows == {}
    assert _wr._last_sent_week is None


def test_does_not_fire_before_target_hour(
    monkeypatch: pytest.MonkeyPatch, fake_store: _FakeDedupeStore
) -> None:
    fired: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fired.append(1)
        return {}

    monkeypatch.setattr(_wr, "fire_now", _fake_fire_now)
    # Friday 21:00 UTC — one hour before the configured 22:00.
    _freeze_now(monkeypatch, datetime(2026, 4, 24, 21, 0, tzinfo=timezone.utc))

    asyncio.run(_drive_one_iteration())

    assert fired == []
    assert fake_store.rows == {}


def test_does_not_fire_one_minute_before_target(
    monkeypatch: pytest.MonkeyPatch, fake_store: _FakeDedupeStore
) -> None:
    """Same hour but earlier minute — the gate must respect the
    minute precision, not just the hour, otherwise an off-by-one in
    the comparison would let it fire at 22:00 sharp regardless of
    `WEEKLY_ROUNDUP_MINUTE_UTC`."""
    fired: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fired.append(1)
        return {}

    monkeypatch.setattr(_wr, "fire_now", _fake_fire_now)
    monkeypatch.setenv("WEEKLY_ROUNDUP_MINUTE_UTC", "30")
    # Friday 22:29 UTC — one minute before the configured 22:30.
    _freeze_now(monkeypatch, datetime(2026, 4, 24, 22, 29, tzinfo=timezone.utc))

    asyncio.run(_drive_one_iteration())

    assert fired == []


def test_fires_at_the_configured_window(
    monkeypatch: pytest.MonkeyPatch, fake_store: _FakeDedupeStore
) -> None:
    fired: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fired.append(1)
        return {"queued": True}

    monkeypatch.setattr(_wr, "fire_now", _fake_fire_now)
    when = datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc)  # Friday 22:00
    _freeze_now(monkeypatch, when)

    asyncio.run(_drive_one_iteration())

    assert len(fired) == 1
    expected_key = "weekly_roundup:" + _wr._iso_week_key(when)
    assert expected_key in fake_store.rows
    assert _wr._last_sent_week == _wr._iso_week_key(when)


def test_fires_after_target_time_within_same_day(
    monkeypatch: pytest.MonkeyPatch, fake_store: _FakeDedupeStore
) -> None:
    """A scheduler that boots at 23:30 on Friday must still fire — the
    gate is "past target time", not "exactly at target time"."""
    fired: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fired.append(1)
        return {}

    monkeypatch.setattr(_wr, "fire_now", _fake_fire_now)
    _freeze_now(monkeypatch, datetime(2026, 4, 24, 23, 30, tzinfo=timezone.utc))

    asyncio.run(_drive_one_iteration())

    assert len(fired) == 1


def test_overridden_dow_and_time_are_respected(
    monkeypatch: pytest.MonkeyPatch, fake_store: _FakeDedupeStore
) -> None:
    """Operators can override the send window via env vars — the
    scheduler must read the overrides each tick, not just at boot."""
    monkeypatch.setenv("WEEKLY_ROUNDUP_DOW", "0")        # Monday
    monkeypatch.setenv("WEEKLY_ROUNDUP_HOUR_UTC", "9")
    monkeypatch.setenv("WEEKLY_ROUNDUP_MINUTE_UTC", "30")

    fired: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fired.append(1)
        return {}

    monkeypatch.setattr(_wr, "fire_now", _fake_fire_now)

    # Monday 09:29 UTC — one minute too early.
    _freeze_now(monkeypatch, datetime(2026, 4, 27, 9, 29, tzinfo=timezone.utc))
    asyncio.run(_drive_one_iteration())
    assert fired == []

    # Monday 09:30 UTC — exactly the configured target.
    _wr._last_sent_week = None
    _freeze_now(monkeypatch, datetime(2026, 4, 27, 9, 30, tzinfo=timezone.utc))
    asyncio.run(_drive_one_iteration())
    assert len(fired) == 1


# ---------------------------------------------------------------------------
# ISO-week dedupe — restart safety
# ---------------------------------------------------------------------------


def test_two_ticks_inside_same_window_fire_only_once(
    monkeypatch: pytest.MonkeyPatch, fake_store: _FakeDedupeStore
) -> None:
    """The in-memory `_last_sent_week` short-circuits any further
    `dedupe_or_skip` calls within the same Friday — a busy scheduler
    that wakes every minute must not produce a digest per minute."""
    fired: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fired.append(1)
        return {}

    monkeypatch.setattr(_wr, "fire_now", _fake_fire_now)
    _freeze_now(monkeypatch, datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc))

    asyncio.run(_drive_one_iteration())
    asyncio.run(_drive_one_iteration())

    assert len(fired) == 1


def test_restart_inside_same_window_is_blocked_by_persistent_dedupe(
    monkeypatch: pytest.MonkeyPatch, fake_store: _FakeDedupeStore
) -> None:
    """Simulate a process restart inside the same Friday window:
    the in-memory `_last_sent_week` marker is lost, so the loop will
    re-enter the gate and try to claim the dedupe key — but the
    persistent `notification_dedupe` row from the prior boot is still
    in TTL, so the claim fails and `fire_now` is not invoked again.

    This is the regression test for "operator does `kubectl rollout
    restart` at 22:05 Friday and now everyone gets two emails"."""
    fired: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fired.append(1)
        return {}

    monkeypatch.setattr(_wr, "fire_now", _fake_fire_now)
    when = datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc)
    _freeze_now(monkeypatch, when)

    asyncio.run(_drive_one_iteration())
    assert len(fired) == 1
    week_key = _wr._iso_week_key(when)
    assert f"weekly_roundup:{week_key}" in fake_store.rows

    # Restart: keep the persistent dedupe row, drop the in-memory marker.
    _wr._last_sent_week = None
    asyncio.run(_drive_one_iteration())

    # Still exactly one fire — second tick was blocked at dedupe_or_skip.
    assert len(fired) == 1
    # And the in-memory cache was repopulated as a fast-path for the
    # remaining ticks of the day.
    assert _wr._last_sent_week == week_key


def test_next_iso_week_uses_a_fresh_dedupe_key(
    monkeypatch: pytest.MonkeyPatch, fake_store: _FakeDedupeStore
) -> None:
    """A different ISO-week key bypasses the prior week's dedupe row
    so the following Friday produces another digest even while the
    previous claim is still within its 7-day TTL."""
    fired: list[int] = []

    async def _fake_fire_now(*, dry_run: bool = False) -> dict[str, Any]:
        fired.append(1)
        return {}

    monkeypatch.setattr(_wr, "fire_now", _fake_fire_now)

    when_a = datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc)  # Friday W17
    _freeze_now(monkeypatch, when_a)
    asyncio.run(_drive_one_iteration())
    assert len(fired) == 1
    key_a = _wr._iso_week_key(when_a)
    assert f"weekly_roundup:{key_a}" in fake_store.rows

    # One week later — different ISO week, fresh dedupe key.
    _wr._last_sent_week = None
    when_b = datetime(2026, 5, 1, 22, 0, tzinfo=timezone.utc)   # Friday W18
    _freeze_now(monkeypatch, when_b)
    asyncio.run(_drive_one_iteration())

    assert len(fired) == 2
    key_b = _wr._iso_week_key(when_b)
    assert key_b != key_a
    assert f"weekly_roundup:{key_b}" in fake_store.rows


# ---------------------------------------------------------------------------
# collect_summary — 7-day aggregation window
# ---------------------------------------------------------------------------


def test_collect_summary_bounds_trades_query_to_last_seven_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The realized-P&L query targets the `trades` table and is bounded
    to `[now - 7d, now)` so a Friday digest never aggregates into the
    next Friday's window."""
    captured: dict[str, Any] = {}

    class _FakeDB:
        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
            captured["realized_query"] = query
            captured["realized_args"] = args
            return {"realized": 12.34, "n": 7, "wins": 4}

        async def fetchval(self, query: str, *args: Any) -> float:
            captured["unrealized_query"] = query
            return 5.25

    monkeypatch.setattr(_wr, "db", _FakeDB())

    when = datetime(2026, 4, 24, 22, 0, 0, tzinfo=timezone.utc)
    summary = asyncio.run(_wr.collect_summary(when))

    # Query targets `trades` with a half-open [start, end) window on
    # `settled_at` — the helper converts both bounds to ISO strings.
    assert "FROM trades" in captured["realized_query"]
    assert "settled_at IS NOT NULL" in captured["realized_query"]
    assert "settled_at >=" in captured["realized_query"]
    assert "settled_at <" in captured["realized_query"]
    start_iso, end_iso = captured["realized_args"]
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    assert end == when
    assert end - start == timedelta(days=7)

    # Values are surfaced to the email payload in the expected units.
    assert summary["realized_pnl_cents"] == 1234
    assert summary["trades_settled"] == 7
    assert summary["win_rate_pct"] == round(100.0 * 4 / 7, 1)
    assert summary["unrealized_pnl_cents"] == 525
    assert summary["week_ending_utc"] == "2026-04-24"


def test_collect_summary_handles_empty_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A week with no settled trades still produces a valid payload —
    `win_rate_pct` is None (not a divide-by-zero) and the cents counts
    are 0 rather than missing keys."""

    class _FakeDB:
        async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
            return {"realized": 0.0, "n": 0, "wins": 0}

        async def fetchval(self, query: str, *args: Any) -> float:
            return 0.0

    monkeypatch.setattr(_wr, "db", _FakeDB())

    when = datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc)
    summary = asyncio.run(_wr.collect_summary(when))
    assert summary["realized_pnl_cents"] == 0
    assert summary["trades_settled"] == 0
    assert summary["win_rate_pct"] is None
    assert summary["unrealized_pnl_cents"] == 0


def test_collect_summary_degrades_when_realized_query_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial DB outage on the `trades` query must not take down
    the whole digest — we still want operators to see the unrealized
    snapshot from `positions`."""

    class _FakeDB:
        async def fetchrow(self, query: str, *args: Any) -> None:
            raise RuntimeError("trades query exploded")

        async def fetchval(self, query: str, *args: Any) -> float:
            return 7.5

    monkeypatch.setattr(_wr, "db", _FakeDB())

    when = datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc)
    summary = asyncio.run(_wr.collect_summary(when))

    assert summary["realized_pnl_cents"] == 0
    assert summary["trades_settled"] == 0
    assert summary["win_rate_pct"] is None
    # Unrealized snapshot still flows through despite the realized failure.
    assert summary["unrealized_pnl_cents"] == 750
