"""Tests for the live-state freshness page (task #862).

The Live-Games-Now dashboard tile reddens a sport when its oldest
``sportradar_live_state`` row exceeds 2× the per-sport freshness
budget, but the operator has to be looking at the dashboard to
notice. ``notifications.live_state_staleness.check_live_state_staleness``
closes that gap by paging when the condition has persisted for N
consecutive background checks, debouncing the page through
``alerts.dedupe_or_skip``, and auto-clearing with a recovery notice
when freshness returns inside budget.

Covered behaviours:

  1. Fresh feeds and below-threshold staleness stay quiet.
  2. The page only fires after N consecutive over-threshold checks.
  3. A sustained incident pages once inside the dedupe TTL and a
     long-lived incident re-pages after the dedupe window expires.
  4. Recovery emits a companion notice and drops the dedupe row so a
     fresh incident can page immediately.
  5. An idle sport (no live games) resets the counter without paging.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.notifications import live_state_staleness as _stale


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDedupe:
    """In-memory stand-in for ``notification_dedupe`` claims."""

    def __init__(self) -> None:
        self.rows: dict[str, datetime] = {}

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "notification_dedupe" in query
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        if key not in self.rows or self.rows[key] <= now:
            self.rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None

    async def execute(self, query: str, *args: Any) -> None:
        # The recovery path drops the dedupe row.
        assert "DELETE FROM notification_dedupe" in query
        self.rows.pop(args[0], None)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tile_per_sport: dict[str, dict[str, Any]],
) -> tuple[_FakeDedupe, list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Wire up the in-memory dedupe table, capture every dispatched
    alert, and pin ``dashboard.get_live_now_tile`` to the supplied
    per-sport snapshot.

    Returns ``(fake_dedupe, dispatched_alerts, mutable_per_sport)``.
    Mutating ``mutable_per_sport`` between calls lets a test simulate
    the staleness recovering across cycles without re-installing the
    monkeypatch.
    """
    _stale.reset_state()

    fake = _FakeDedupe()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(_db, "execute", fake.execute)

    per_sport = dict(tile_per_sport)

    async def _fake_tile() -> dict[str, Any]:
        return {"per_sport": dict(per_sport)}

    from app import dashboard as _dashboard
    monkeypatch.setattr(_dashboard, "get_live_now_tile", _fake_tile)

    dispatched: list[dict[str, Any]] = []

    def _fake_dispatch(
        subject: str,
        text_body: str,
        *,
        tag: str,
        dry_run: bool,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        dispatched.append(
            {"subject": subject, "body": text_body, "tag": tag}
        )
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)

    # Stub Telegram fan-out so the helper never tries a real network call.
    import sys
    import types
    tg_pkg_name = "app.telegram"
    if tg_pkg_name not in sys.modules:
        sys.modules[tg_pkg_name] = types.ModuleType(tg_pkg_name)
    notify_mod = types.ModuleType(f"{tg_pkg_name}.notify")
    notify_mod.broadcast_in_background = lambda _msg: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{tg_pkg_name}.notify", notify_mod)

    return fake, dispatched, per_sport


# ---------------------------------------------------------------------------
# Quiet paths
# ---------------------------------------------------------------------------


def test_fresh_feed_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """All sports within budget → no page, no recovery, no over-threshold."""
    # NBA budget is 60s; tile reports 10s old → well inside.
    _, dispatched, _ = _install_fakes(
        monkeypatch,
        tile_per_sport={
            "nba": {"live_games": 2, "oldest_staleness_seconds": 10.0},
        },
    )

    res = asyncio.run(_stale.check_live_state_staleness())

    assert res["fired"] == []
    assert res["recovered"] == []
    assert res["suppressed"] == []
    assert res["over_threshold"] == []
    assert dispatched == []
    assert res["per_sport"]["nba"]["consecutive_checks"] == 0


def test_idle_sport_does_not_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero live games short-circuits before any threshold compare."""
    _, dispatched, _ = _install_fakes(
        monkeypatch,
        tile_per_sport={
            # No live games — staleness is irrelevant.
            "nba": {"live_games": 0, "oldest_staleness_seconds": 9999.0},
        },
    )

    res = asyncio.run(_stale.check_live_state_staleness())

    assert res["fired"] == []
    assert dispatched == []
    assert res["per_sport"]["nba"]["consecutive_checks"] == 0


# ---------------------------------------------------------------------------
# Consecutive-checks debouncing
# ---------------------------------------------------------------------------


def test_single_stale_check_does_not_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default threshold is 2 consecutive checks — one trip stays quiet."""
    # NBA budget 60s; 200s > 2×60 = 120s → over the line.
    _, dispatched, _ = _install_fakes(
        monkeypatch,
        tile_per_sport={
            "nba": {"live_games": 3, "oldest_staleness_seconds": 200.0},
        },
    )

    res = asyncio.run(_stale.check_live_state_staleness())

    assert res["fired"] == []
    assert res["over_threshold"] == []
    assert dispatched == []
    assert res["per_sport"]["nba"]["consecutive_checks"] == 1


def test_two_consecutive_checks_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two over-threshold checks in a row trip the page (default N=2)."""
    _, dispatched, _ = _install_fakes(
        monkeypatch,
        tile_per_sport={
            "nba": {"live_games": 3, "oldest_staleness_seconds": 200.0},
        },
    )

    asyncio.run(_stale.check_live_state_staleness())
    res = asyncio.run(_stale.check_live_state_staleness())

    assert res["fired"] == ["nba"]
    assert res["over_threshold"] == ["nba"]
    assert len(dispatched) == 1
    assert "[LIVE-STATE STALE]" in dispatched[0]["subject"]
    assert "NBA" in dispatched[0]["subject"]
    body = dispatched[0]["body"]
    assert "200" in body
    assert "60" in body  # budget
    assert "2x" in body or "2×" in body or "2" in body  # multiplier mention


def test_streak_resets_on_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One stale check + one fresh check resets the counter — no page."""
    _, dispatched, per = _install_fakes(
        monkeypatch,
        tile_per_sport={
            "nba": {"live_games": 3, "oldest_staleness_seconds": 200.0},
        },
    )

    asyncio.run(_stale.check_live_state_staleness())
    per["nba"] = {"live_games": 3, "oldest_staleness_seconds": 10.0}
    res = asyncio.run(_stale.check_live_state_staleness())

    # Never paged in the first place, so no recovery notice — just a
    # quiet reset.
    assert res["fired"] == []
    assert res["recovered"] == []
    assert dispatched == []
    assert res["per_sport"]["nba"]["consecutive_checks"] == 0


def test_custom_threshold_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``consecutive_checks=1`` pages on the first over-threshold sample."""
    _, dispatched, _ = _install_fakes(
        monkeypatch,
        tile_per_sport={
            "nba": {"live_games": 3, "oldest_staleness_seconds": 200.0},
        },
    )

    res = asyncio.run(
        _stale.check_live_state_staleness(consecutive_checks=1)
    )

    assert res["fired"] == ["nba"]
    assert len(dispatched) == 1


# ---------------------------------------------------------------------------
# Dedupe + recovery
# ---------------------------------------------------------------------------


def test_sustained_stale_pages_once_inside_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sport that stays stale for many cycles pages exactly once
    inside the dedupe TTL; the cycle log marks the suppressed pages."""
    _, dispatched, _ = _install_fakes(
        monkeypatch,
        tile_per_sport={
            "nba": {"live_games": 3, "oldest_staleness_seconds": 200.0},
        },
    )

    asyncio.run(_stale.check_live_state_staleness())
    asyncio.run(_stale.check_live_state_staleness())
    assert len(dispatched) == 1

    third = asyncio.run(_stale.check_live_state_staleness())
    assert third["fired"] == []
    assert third["suppressed"] == ["nba"]
    assert third["over_threshold"] == ["nba"]
    assert len(dispatched) == 1


def test_dedupe_expires_and_repages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the dedupe row expires, a still-wedged feed re-pages."""
    fake, dispatched, _ = _install_fakes(
        monkeypatch,
        tile_per_sport={
            "nba": {"live_games": 3, "oldest_staleness_seconds": 200.0},
        },
    )

    asyncio.run(
        _stale.check_live_state_staleness(dedupe_ttl_seconds=60)
    )
    asyncio.run(
        _stale.check_live_state_staleness(dedupe_ttl_seconds=60)
    )
    assert len(dispatched) == 1

    # Force-expire every dedupe row.
    for k in list(fake.rows.keys()):
        fake.rows[k] = datetime.now(timezone.utc) - timedelta(seconds=1)

    res = asyncio.run(
        _stale.check_live_state_staleness(dedupe_ttl_seconds=60)
    )
    assert res["fired"] == ["nba"]
    assert len(dispatched) == 2


def test_recovery_after_alert_clears_dedupe_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When freshness returns after a page, fire the recovery notice
    and drop the dedupe row so a fresh incident can page immediately
    rather than waiting out the TTL."""
    fake, dispatched, per = _install_fakes(
        monkeypatch,
        tile_per_sport={
            "nba": {"live_games": 3, "oldest_staleness_seconds": 200.0},
        },
    )

    asyncio.run(_stale.check_live_state_staleness())
    asyncio.run(_stale.check_live_state_staleness())
    assert len(dispatched) == 1
    # Stale dedupe row is live.
    stale_key = _alerts._compose_key(_stale.STALE_ALERT_TYPE, "nba")
    assert stale_key in fake.rows

    # Recovery cycle.
    per["nba"] = {"live_games": 3, "oldest_staleness_seconds": 10.0}
    res = asyncio.run(_stale.check_live_state_staleness())

    assert res["recovered"] == ["nba"]
    assert res["fired"] == []
    assert len(dispatched) == 2
    recovery = dispatched[-1]
    assert recovery["tag"] == "live-state-recovered"
    assert "[LIVE-STATE OK]" in recovery["subject"]
    assert "NBA" in recovery["subject"]
    # Dedupe row dropped → next incident can re-page immediately.
    assert stale_key not in fake.rows

    # Immediate re-stale (still inside the would-have-been TTL) pages
    # again after the consecutive-check threshold.
    per["nba"] = {"live_games": 3, "oldest_staleness_seconds": 250.0}
    asyncio.run(_stale.check_live_state_staleness())
    res2 = asyncio.run(_stale.check_live_state_staleness())
    assert res2["fired"] == ["nba"]
    assert len(dispatched) == 3


def test_idle_after_alert_still_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live games dropping to zero counts as recovery for an alerted
    sport — the on-call should know the incident closed even if it
    closed because the games ended."""
    _, dispatched, per = _install_fakes(
        monkeypatch,
        tile_per_sport={
            "nba": {"live_games": 3, "oldest_staleness_seconds": 200.0},
        },
    )

    asyncio.run(_stale.check_live_state_staleness())
    asyncio.run(_stale.check_live_state_staleness())
    assert len(dispatched) == 1

    per["nba"] = {"live_games": 0, "oldest_staleness_seconds": None}
    res = asyncio.run(_stale.check_live_state_staleness())

    assert res["recovered"] == ["nba"]
    assert dispatched[-1]["tag"] == "live-state-recovered"


# ---------------------------------------------------------------------------
# Alert helper bodies
# ---------------------------------------------------------------------------


def test_live_state_stale_helper_body() -> None:
    out = _alerts.live_state_stale(
        sport="nba",
        oldest_staleness_seconds=210.0,
        budget_seconds=60.0,
        multiplier=2.0,
        consecutive_checks=3,
        threshold_checks=2,
        live_games=4,
        dashboard_url="https://example.com/#live-now",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "live-state-stale"
    assert "[LIVE-STATE STALE]" in out["subject"]
    assert "NBA" in out["subject"]
    body = out["body_preview"]
    assert "210" in body
    assert "60" in body
    assert "consecutive_checks" in body
    assert "https://example.com/#live-now" in body


def test_live_state_recovered_helper_body() -> None:
    out = _alerts.live_state_recovered(
        sport="nba",
        oldest_staleness_seconds=12.0,
        budget_seconds=60.0,
        dashboard_url="https://example.com/#live-now",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "live-state-recovered"
    assert "[LIVE-STATE OK]" in out["subject"]
    assert "NBA" in out["subject"]
    body = out["body_preview"]
    assert "12" in body
    assert "60" in body
