"""Tests for the cap-bucket warn dispatch history surfaced on the
exposure dashboard panels (task #606).

The orchestrator-side cap-warn dispatcher (``cap_warnings``) emails the
on-call when an NFL exposure bucket crosses 80% of its cap, but
operators landing on the dashboard could not previously tell whether
a bucket had already paged this hour or whether the email was still
pending.  These tests pin the contract surfaced by
``dashboard.get_nfl_exposure`` and ``dashboard.get_leagues_exposure``:

  * Per-bucket ``recent_alert`` carries the last-paged timestamp,
    dedupe expiry, and "fired this cycle" / "suppressed this cycle"
    flags so the panel can show silence vs. quiet.
  * The top-level ``recent_warnings`` summary lets the panel render a
    single "dispatcher last ran at HH:MM, fired N, suppressed M" line.
  * Both endpoints fail soft when the dedupe table is unreachable —
    the operator console keeps rendering, the new fields go blank.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_last_cycle():
    """Each test owns its own last-cycle snapshot so ordering does not
    leak state between cases."""
    from app.notifications import cap_warnings as _cap

    _cap.reset_last_cycle()
    yield
    _cap.reset_last_cycle()


def _wire_aggregates(monkeypatch, *, bankroll=10_000.0):
    """Pin the orchestrator snapshot the dashboard reads so we can
    assert the bucket → recent_alert wiring without standing up a
    Postgres pool."""
    from app.orchestrator import cycle as cycle_mod

    per_league = {"NFL": 400.0}
    per_matchup = {"NFL:BUF@MIA": 300.0, "NFL:KC@DEN": 100.0}
    per_sunday = {"NFL": 250.0}

    async def _fake_aggregates(*, trading_mode="live"):
        return (
            5,
            450.0,
            {"sports": 450.0},
            per_league,
            per_matchup,
            per_sunday,
            {},
        )

    async def _fake_bankroll(*, trading_mode="live"):
        return bankroll

    monkeypatch.setattr(cycle_mod, "_open_position_aggregates", _fake_aggregates)
    monkeypatch.setattr(cycle_mod, "_latest_bankroll", _fake_bankroll)


def _stub_dedupe_rows(monkeypatch, rows):
    """Pin the ``notification_dedupe`` read so we don't need a live DB
    pool. Mirrors the asyncpg row shape (mapping access on `key` etc.).
    """
    from app import db as _db

    async def _fake_fetch(query, *args):
        assert "notification_dedupe" in query
        return rows

    monkeypatch.setattr(_db, "fetch", _fake_fetch)


def test_no_history_yields_null_recent_alerts(monkeypatch):
    """A cold start (no dedupe rows, dispatcher hasn't run) must render
    every bucket with ``recent_alert == None`` and a summary clearly
    flagged as not-yet-run, so the panel can show 'silent' rather than
    'paged 0 minutes ago'."""
    from app import dashboard

    _wire_aggregates(monkeypatch)
    _stub_dedupe_rows(monkeypatch, [])

    res = asyncio.run(dashboard.get_nfl_exposure())

    assert res["per_league"]["recent_alert"] is None
    assert res["sunday_window"]["recent_alert"] is None
    for m in res["per_matchup"]:
        assert m["recent_alert"] is None

    summary = res["recent_warnings"]
    assert summary["ran_at"] is None
    assert summary["fired_count"] == 0
    assert summary["suppressed_count"] == 0
    assert summary["tracked_buckets"] == 0


def test_recent_alert_surfaces_dedupe_row_per_bucket(monkeypatch):
    """A live dedupe row for ``nfl_cap_warn:NFL:per_league`` must land on
    the per-league bucket with last-sent + ttl-remaining, leaving other
    buckets untouched so the operator sees exactly which bucket paged."""
    from app import dashboard

    _wire_aggregates(monkeypatch)
    now = datetime.now(timezone.utc)
    sent_at = now - timedelta(minutes=7)
    expires_at = now + timedelta(minutes=53)
    _stub_dedupe_rows(
        monkeypatch,
        [
            {
                "key": "nfl_cap_warn:NFL:per_league",
                "sent_at": sent_at,
                "expires_at": expires_at,
            }
        ],
    )

    res = asyncio.run(dashboard.get_nfl_exposure())

    pl = res["per_league"]["recent_alert"]
    assert pl is not None
    assert pl["last_sent_at"] is not None
    # 53 minute TTL ± a couple of seconds for clock drift between the
    # fixture timestamp and the helper's `now`.
    assert 53 * 60 - 5 <= pl["ttl_seconds_remaining"] <= 53 * 60 + 1
    assert pl["fired_last_cycle"] is False
    assert pl["suppressed_last_cycle"] is False

    # Other buckets in the same snapshot must stay quiet — only the
    # bucket whose dedupe row exists carries `recent_alert`.
    assert res["sunday_window"]["recent_alert"] is None
    assert all(m["recent_alert"] is None for m in res["per_matchup"])


def test_recent_alert_flags_fired_vs_suppressed_in_last_cycle(monkeypatch):
    """The in-memory last-cycle snapshot must distinguish silence from
    quiet: a bucket the dispatcher *did* dispatch must read
    `fired_last_cycle=True`, a bucket it deliberately suppressed by
    dedupe must read `suppressed_last_cycle=True`."""
    from app import dashboard
    from app.notifications import cap_warnings as _cap

    _wire_aggregates(monkeypatch)
    now = datetime.now(timezone.utc)
    _stub_dedupe_rows(
        monkeypatch,
        [
            {
                "key": "nfl_cap_warn:NFL:per_league",
                "sent_at": now - timedelta(minutes=2),
                "expires_at": now + timedelta(minutes=58),
            },
            {
                "key": "nfl_cap_warn:NFL:sunday_window",
                "sent_at": now - timedelta(minutes=45),
                "expires_at": now + timedelta(minutes=15),
            },
        ],
    )

    # Pretend the latest cycle dispatched per-league but suppressed the
    # Sunday window because its dedupe row was already live.
    _cap._record_cycle(
        threshold=0.80,
        ttl_seconds=3600,
        over_threshold=["NFL:per_league", "NFL:sunday_window"],
        fired=["NFL:per_league"],
        suppressed=["NFL:sunday_window"],
    )

    res = asyncio.run(dashboard.get_nfl_exposure())

    assert res["per_league"]["recent_alert"]["fired_last_cycle"] is True
    assert res["per_league"]["recent_alert"]["suppressed_last_cycle"] is False
    assert res["sunday_window"]["recent_alert"]["fired_last_cycle"] is False
    assert res["sunday_window"]["recent_alert"]["suppressed_last_cycle"] is True

    summary = res["recent_warnings"]
    assert summary["ran_at"] is not None
    assert summary["fired_count"] == 1
    assert summary["suppressed_count"] == 1
    assert summary["threshold"] == 0.80
    assert summary["ttl_seconds"] == 3600
    assert summary["tracked_buckets"] == 2


def test_per_matchup_dedupe_row_attaches_to_matching_matchup(monkeypatch):
    """A per-matchup dedupe row keyed on ``BUF@MIA`` must attach to the
    BUF@MIA bucket only — the other open matchup must stay quiet."""
    from app import dashboard

    _wire_aggregates(monkeypatch)
    now = datetime.now(timezone.utc)
    _stub_dedupe_rows(
        monkeypatch,
        [
            {
                "key": "nfl_cap_warn:NFL:per_matchup:BUF@MIA",
                "sent_at": now - timedelta(minutes=12),
                "expires_at": now + timedelta(minutes=48),
            }
        ],
    )

    res = asyncio.run(dashboard.get_nfl_exposure())

    matchups = {m["matchup"]: m for m in res["per_matchup"]}
    assert matchups["BUF@MIA"]["recent_alert"] is not None
    assert matchups["BUF@MIA"]["recent_alert"]["last_sent_at"] is not None
    assert matchups["KC@DEN"]["recent_alert"] is None


def test_dedupe_fetch_failure_does_not_break_dashboard(monkeypatch):
    """If ``notification_dedupe`` is unreachable the panel must keep
    rendering — the operator can still see open exposure even when
    notifications are degraded."""
    from app import dashboard
    from app import db as _db

    _wire_aggregates(monkeypatch)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(_db, "fetch", _boom)

    res = asyncio.run(dashboard.get_nfl_exposure())

    # Buckets still rendered, recent_alert empty.
    assert res["per_league"]["open_usd"] == 400.0
    assert res["per_league"]["recent_alert"] is None
    assert res["recent_warnings"]["ran_at"] is None


def test_leagues_exposure_attaches_recent_alert_for_nfl(monkeypatch):
    """The multi-league panel (the one actually mounted on the
    dashboard) must surface the same per-bucket history so operators
    landing on the consolidated view get the same signal."""
    from app import dashboard

    _wire_aggregates(monkeypatch)
    now = datetime.now(timezone.utc)
    _stub_dedupe_rows(
        monkeypatch,
        [
            {
                "key": "nfl_cap_warn:NFL:per_league",
                "sent_at": now - timedelta(minutes=3),
                "expires_at": now + timedelta(minutes=57),
            }
        ],
    )

    res = asyncio.run(dashboard.get_leagues_exposure())

    nfl_entry = next(e for e in res["leagues"] if e["league"] == "NFL")
    assert nfl_entry["per_league"]["recent_alert"] is not None
    assert nfl_entry["per_league"]["recent_alert"]["last_sent_at"] is not None
    # Sunday-window has no dedupe row in this snapshot.
    assert nfl_entry["sunday_window"]["recent_alert"] is None
    assert "recent_warnings" in res


def test_check_cycle_records_last_cycle_for_dashboard(monkeypatch):
    """The dispatcher must hand the dashboard enough state to render
    'fired this cycle, suppressed this cycle' without re-querying the
    same dedupe rows."""
    from app.notifications import cap_warnings as _cap
    from app import dashboard as _dash
    from app import db as _db

    # Snapshot: per-league bucket is over the warn threshold, others
    # quiet — so the cycle should fire a single dispatch.
    snapshot = {
        "league": "NFL",
        "bankroll_usd": 10_000.0,
        "warn_threshold": 0.80,
        "per_league": {
            "open_usd": 425.0, "cap_pct": 0.05, "cap_usd": 500.0,
            "headroom_usd": 75.0, "utilization": 0.85, "warn": True,
        },
        "sunday_window": {
            "open_usd": 100.0, "cap_pct": 0.03, "cap_usd": 300.0,
            "headroom_usd": 200.0, "utilization": 0.33, "warn": False,
        },
        "per_matchup": [],
        "raw": {},
    }

    async def _fake_exposure() -> dict[str, Any]:
        return snapshot

    monkeypatch.setattr(_dash, "get_nfl_exposure", _fake_exposure)

    # Stub the dedupe insert so we don't need a real DB.
    async def _fake_fetchrow(query, *args):
        assert "notification_dedupe" in query
        return {"ok": 1}

    monkeypatch.setattr(_db, "fetchrow", _fake_fetchrow)

    # And no-op the actual email dispatch so we don't try to talk to
    # Postmark from a unit test.
    from app.notifications import alerts as _alerts

    monkeypatch.setattr(
        _alerts,
        "_dispatch",
        lambda *a, **kw: {"tag": kw.get("tag"), "dispatched": True},
    )

    asyncio.run(_cap.check_nfl_cap_warnings())

    snap = _cap.last_cycle_snapshot()
    assert snap is not None
    assert snap["fired"] == ["NFL:per_league"]
    assert snap["suppressed"] == []
    assert snap["over_threshold"] == ["NFL:per_league"]
    assert snap["threshold"] == 0.80
    assert snap["ran_at"] is not None
