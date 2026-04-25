"""Tests for the periodic NFL backfill feed (`app.feeds.nfl_backfill`).

Verifies:
  * The "latest completed season" calculation flips on March 1.
  * The season window covers August Y through July Y+1 (so Super Bowl
    in February of Y+1 is included).
  * `poll_nfl_backfill_once` triggers the backfill *only* when the
    season is missing from `games` AND a Sportradar key is configured.
  * Re-running the poll once the season is present is a true no-op
    (no Sportradar fetch, no upsert).
  * Missing API key short-circuits even when the season is missing.
  * `register_nfl_backfill_feed` plumbs the right enabled / skipped
    state through the registry depending on env vars.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.feeds import nfl_backfill
from app.feeds.registry import FeedRegistry


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_latest_completed_nfl_season_flips_on_march_1():
    # Mid-February 2026: this season's Super Bowl hasn't happened yet
    # in our conservative model -> latest *completed* season is 2024.
    feb = datetime(2026, 2, 14, tzinfo=timezone.utc)
    assert nfl_backfill.latest_completed_nfl_season(feb) == 2024
    # March 1 onwards: the just-finished season is the latest.
    mar = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert nfl_backfill.latest_completed_nfl_season(mar) == 2025
    # During the regular season window (October), the most recently
    # completed season is still the previous year's.
    oct_ = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert nfl_backfill.latest_completed_nfl_season(oct_) == 2025
    # January edge: pre-Super Bowl, season - 1 is still in flight.
    jan = datetime(2026, 1, 20, tzinfo=timezone.utc)
    assert nfl_backfill.latest_completed_nfl_season(jan) == 2024


def test_season_window_covers_super_bowl_in_february_of_next_year():
    start, end = nfl_backfill.season_window_iso(2024)
    assert start.startswith("2024-08-01")
    assert end.startswith("2025-08-01")
    # Super Bowl LIX (Feb 9, 2025) sits comfortably inside the window.
    sb = datetime(2025, 2, 9, 23, 30, tzinfo=timezone.utc).isoformat()
    assert start <= sb < end


# ---------------------------------------------------------------------------
# Poll behaviour
# ---------------------------------------------------------------------------


class _FakeDb:
    """Stand-in for `app.db.fetch` that returns a configurable count."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.calls.append((query, args))
        return [{"n": int(self.count)}]


@pytest.fixture
def fixed_now() -> datetime:
    # Mid-April 2026: latest completed season = 2025.
    return datetime(2026, 4, 25, tzinfo=timezone.utc)


def test_poll_triggers_backfill_when_season_missing(monkeypatch, fixed_now):
    fake_db = _FakeDb(count=0)
    monkeypatch.setattr(nfl_backfill, "_has_sportradar_key", lambda: True)
    # `count_nfl_games_for_season` does `from .. import db` internally;
    # stub the `db.fetch` it ends up calling.
    import app.db as _db
    monkeypatch.setattr(_db, "fetch", fake_db.fetch)

    backfill_calls: list[int] = []

    async def _fake_backfill_season(season: int) -> int:
        backfill_calls.append(season)
        return 285  # plausible REG (272) + PST (13) row count

    monkeypatch.setattr(nfl_backfill, "backfill_season", _fake_backfill_season)

    written = asyncio.run(nfl_backfill.poll_nfl_backfill_once(now=fixed_now))
    assert written == 285
    assert backfill_calls == [2025]
    # The cheap count happened first.
    assert fake_db.calls and "FROM games" in fake_db.calls[0][0]


def test_poll_is_noop_when_season_already_present(monkeypatch, fixed_now):
    fake_db = _FakeDb(count=285)
    monkeypatch.setattr(nfl_backfill, "_has_sportradar_key", lambda: True)
    import app.db as _db
    monkeypatch.setattr(_db, "fetch", fake_db.fetch)

    backfill_calls: list[int] = []

    async def _fake_backfill_season(season: int) -> int:
        backfill_calls.append(season)
        return 999

    monkeypatch.setattr(nfl_backfill, "backfill_season", _fake_backfill_season)

    written = asyncio.run(nfl_backfill.poll_nfl_backfill_once(now=fixed_now))
    assert written == 0
    # Critical: no Sportradar fetch fired when season was already present.
    assert backfill_calls == []


def test_poll_skips_when_db_count_fails(monkeypatch, fixed_now):
    """Transient DB failure must not cause an unnecessary backfill."""
    class _BrokenDb:
        async def fetch(self, *a, **kw):
            raise RuntimeError("supabase down")

    monkeypatch.setattr(nfl_backfill, "_has_sportradar_key", lambda: True)
    import app.db as _db
    monkeypatch.setattr(_db, "fetch", _BrokenDb().fetch)

    backfill_calls: list[int] = []

    async def _fake_backfill_season(season: int) -> int:
        backfill_calls.append(season)
        return 1

    monkeypatch.setattr(nfl_backfill, "backfill_season", _fake_backfill_season)

    written = asyncio.run(nfl_backfill.poll_nfl_backfill_once(now=fixed_now))
    assert written == 0
    # The whole point: don't burn a Sportradar fetch when we can't tell.
    assert backfill_calls == []


def test_poll_skips_when_no_api_key_even_if_missing(monkeypatch, fixed_now):
    fake_db = _FakeDb(count=0)
    monkeypatch.setattr(nfl_backfill, "_has_sportradar_key", lambda: False)
    import app.db as _db
    monkeypatch.setattr(_db, "fetch", fake_db.fetch)

    backfill_calls: list[int] = []

    async def _fake_backfill_season(season: int) -> int:
        backfill_calls.append(season)
        return 1

    monkeypatch.setattr(nfl_backfill, "backfill_season", _fake_backfill_season)

    written = asyncio.run(nfl_backfill.poll_nfl_backfill_once(now=fixed_now))
    assert written == 0
    assert backfill_calls == []


# ---------------------------------------------------------------------------
# Feed registration
# ---------------------------------------------------------------------------


def _isolated_registry(monkeypatch) -> FeedRegistry:
    reg = FeedRegistry()
    monkeypatch.setattr(nfl_backfill, "registry", reg)
    return reg


def test_register_enabled_when_key_present(monkeypatch):
    reg = _isolated_registry(monkeypatch)
    monkeypatch.setenv("SPORTRADAR_NFL_KEY", "sk_test")
    monkeypatch.delenv("AXIOM_NFL_BACKFILL_DISABLED", raising=False)
    nfl_backfill.register_nfl_backfill_feed()
    st = reg.status_of("nfl_backfill")
    assert st is not None
    assert st.enabled is True
    assert st.skipped_reason is None
    assert st.cadence_seconds >= 3600


def test_register_disabled_when_key_missing(monkeypatch):
    reg = _isolated_registry(monkeypatch)
    monkeypatch.delenv("SPORTRADAR_NFL_KEY", raising=False)
    monkeypatch.delenv("SPORTRADAR_API_KEY", raising=False)
    monkeypatch.delenv("AXIOM_NFL_BACKFILL_DISABLED", raising=False)
    nfl_backfill.register_nfl_backfill_feed()
    st = reg.status_of("nfl_backfill")
    assert st is not None
    assert st.enabled is False
    assert "SPORTRADAR" in (st.skipped_reason or "")


def test_register_disabled_via_env_flag(monkeypatch):
    reg = _isolated_registry(monkeypatch)
    monkeypatch.setenv("SPORTRADAR_NFL_KEY", "sk_test")
    monkeypatch.setenv("AXIOM_NFL_BACKFILL_DISABLED", "1")
    nfl_backfill.register_nfl_backfill_feed()
    st = reg.status_of("nfl_backfill")
    assert st is not None
    assert st.enabled is False
    assert "AXIOM_NFL_BACKFILL_DISABLED" in (st.skipped_reason or "")


def test_register_is_idempotent(monkeypatch):
    reg = _isolated_registry(monkeypatch)
    monkeypatch.setenv("SPORTRADAR_NFL_KEY", "sk_test")
    nfl_backfill.register_nfl_backfill_feed()
    nfl_backfill.register_nfl_backfill_feed()  # should not raise
    snap = reg.snapshot()
    assert sum(1 for s in snap if s["name"] == "nfl_backfill") == 1
