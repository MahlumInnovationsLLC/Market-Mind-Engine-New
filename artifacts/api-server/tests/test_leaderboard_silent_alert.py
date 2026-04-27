"""Tests for the Sportradar tournament-leaderboard silence alert
(task #653).

Covers ``health._evaluate_leaderboard_silence_alert`` and the Postmark
+ ``model_health_alerts`` fan-out it triggers via
``notifications.alerts.leaderboard_silent`` when the cross-sport
leaderboard refresher writes 0 rows for >10 minutes while live PGA /
F1 events are in progress.

Three things matter for the on-call signal to be useful:

  1. Exactly one alert (one Postmark email + one ``model_health_alerts``
     row) fires per incident, even across many health snapshots.
  2. The alert auto-resolves the moment the next non-zero refresh (or
     the events drop off) clears the streak — operators don't need to
     touch the dashboard for silence-then-recovery transitions.
  3. After auto-resolving, a *new* incident has to climb the threshold
     again before re-paging.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app import health as health_mod
from app.feeds.registry import Feed, FeedRegistry
from app.feeds import sportradar as sportradar_mod
from app.notifications import alerts as _alerts


_FEED = sportradar_mod.TOURNAMENT_LEADERBOARDS_FEED_NAME


class _FakeDedupe:
    def __init__(self) -> None:
        self.rows: dict[str, datetime] = {}
        self.dispatched: list[dict[str, Any]] = []
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "notification_dedupe" in query
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        if key not in self.rows or self.rows[key] <= now:
            self.rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.executes.append((query, args))
        return "INSERT 0 1"


def _set_meta(reg: feed_registry.FeedRegistry, **fields: Any) -> None:
    reg.set_metadata(_FEED, **fields)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _isoformat(secs_ago: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=secs_ago)
    ).isoformat(timespec="seconds")


@pytest.fixture
def fake_pipe(monkeypatch: pytest.MonkeyPatch) -> _FakeDedupe:
    fake = _FakeDedupe()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(_db, "execute", fake.execute)

    def _fake_dispatch(subject: str, text_body: str, *, tag: str, dry_run: bool,
                       html_body: str | None = None) -> dict[str, Any]:
        fake.dispatched.append({"subject": subject, "body": text_body, "tag": tag})
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)
    health_mod._reset_leaderboard_silence_state()

    # Use an isolated registry per test so leftover state never leaks.
    fresh = FeedRegistry()
    fresh.register(Feed(
        name=_FEED,
        cadence_seconds=120.0,
        poll=lambda: None,  # type: ignore[arg-type]
    ))
    monkeypatch.setattr(health_mod, "feed_registry", fresh)
    return fake


def _alerts_only(fake: _FakeDedupe) -> list[dict[str, Any]]:
    return [d for d in fake.dispatched if d["tag"] == "leaderboard-silent"]


def _inserts(fake: _FakeDedupe) -> list[tuple[str, tuple[Any, ...]]]:
    return [
        (q, a) for (q, a) in fake.executes
        if "INSERT INTO model_health_alerts" in q
    ]


def _resolves(fake: _FakeDedupe) -> list[tuple[str, tuple[Any, ...]]]:
    return [
        (q, a) for (q, a) in fake.executes
        if "UPDATE model_health_alerts" in q
        and "leaderboard_silent" in q
    ]


# ---------------------------------------------------------------------------
# Threshold + dedupe behaviour
# ---------------------------------------------------------------------------


def test_no_alert_when_streak_below_threshold(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5-minute streak (well under the 10-minute floor) must not page."""
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_THRESHOLD_SECONDS", 600)
    _set_meta(
        health_mod.feed_registry,
        events_live=1,
        zero_rows_since=_isoformat(5 * 60),
        last_refresh_count=0,
        last_refresh_at=_now_iso(),
    )
    out = health_mod._evaluate_leaderboard_silence_alert()
    assert out is None
    assert _alerts_only(fake_pipe) == []
    assert _inserts(fake_pipe) == []


def test_no_alert_when_no_events_live_even_if_streak_old(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old `zero_rows_since` with no live events is the off-season —
    must not page."""
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_THRESHOLD_SECONDS", 600)
    _set_meta(
        health_mod.feed_registry,
        events_live=0,
        zero_rows_since=_isoformat(45 * 60),
    )
    assert health_mod._evaluate_leaderboard_silence_alert() is None
    assert _alerts_only(fake_pipe) == []


def test_alert_fires_once_when_streak_crosses_threshold(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threshold = 10m; a 15m streak with a live event must page once
    and only once across many snapshots."""
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_THRESHOLD_SECONDS", 600)
    streak_start = _isoformat(15 * 60)
    _set_meta(
        health_mod.feed_registry,
        events_live=2,
        zero_rows_since=streak_start,
        last_refresh_count=0,
        last_refresh_at=_now_iso(),
    )

    triggered = health_mod._evaluate_leaderboard_silence_alert()
    assert triggered is not None
    assert triggered["events_live"] == 2
    assert triggered["zero_rows_since"] == streak_start

    # Subsequent snapshots while still stale must not re-page.
    for _ in range(5):
        assert health_mod._evaluate_leaderboard_silence_alert() is None

    msgs = _alerts_only(fake_pipe)
    assert len(msgs) == 1
    assert msgs[0]["tag"] == "leaderboard-silent"
    assert "LEADERBOARD" in msgs[0]["subject"]
    assert "silent" in msgs[0]["subject"]
    assert "live event" in msgs[0]["subject"]

    inserts = _inserts(fake_pipe)
    assert len(inserts) == 1
    insert_query, insert_args = inserts[0]
    assert "leaderboard_silent" in insert_query
    # message arg includes the event count + streak start
    assert "live event" in insert_args[2]
    assert streak_start in insert_args[2]


def test_alert_includes_diagnostic_fields_in_body(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Postmark body must carry the streak start, age, threshold,
    cadence, and live-event count so on-call doesn't have to open
    the dashboard to triage."""
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_THRESHOLD_SECONDS", 600)
    _set_meta(
        health_mod.feed_registry,
        events_live=3,
        zero_rows_since=_isoformat(20 * 60),
        last_refresh_at=_now_iso(),
    )
    health_mod._evaluate_leaderboard_silence_alert()
    msgs = _alerts_only(fake_pipe)
    assert len(msgs) == 1
    body = msgs[0]["body"]
    assert "events_live" in body
    assert "zero_rows_since" in body
    assert "threshold_seconds" in body
    assert "cadence_seconds" in body
    assert "600" in body  # threshold_seconds
    assert "120" in body  # cadence_seconds


def test_dedupe_suppresses_postmark_when_within_ttl(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a previous incident's dedupe row is still live, the
    Postmark dispatch suppresses but the row insert still happens —
    the operator console must always reflect the current incident."""
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_THRESHOLD_SECONDS", 600)
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_ALERT_TTL_SECONDS", 3600)

    # Pre-populate dedupe with a row that's still live.
    fake_pipe.rows["leaderboard_silent:sportradar_tournament_leaderboards"] = (
        datetime.now(timezone.utc) + timedelta(seconds=3600)
    )

    _set_meta(
        health_mod.feed_registry,
        events_live=1,
        zero_rows_since=_isoformat(15 * 60),
        last_refresh_at=_now_iso(),
    )
    triggered = health_mod._evaluate_leaderboard_silence_alert()
    assert triggered is not None
    # No Postmark dispatch (dedupe took it), but the row insert still ran.
    assert _alerts_only(fake_pipe) == []
    assert len(_inserts(fake_pipe)) == 1


# ---------------------------------------------------------------------------
# Auto-resolve behaviour
# ---------------------------------------------------------------------------


def test_alert_auto_resolves_when_streak_clears(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once `zero_rows_since` returns to None, any open
    leaderboard_silent rows must be auto-acknowledged."""
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_THRESHOLD_SECONDS", 600)

    # Step 1: stale — fires alert.
    _set_meta(
        health_mod.feed_registry,
        events_live=1,
        zero_rows_since=_isoformat(15 * 60),
        last_refresh_at=_now_iso(),
    )
    assert health_mod._evaluate_leaderboard_silence_alert() is not None
    assert len(_inserts(fake_pipe)) == 1
    assert _resolves(fake_pipe) == []

    # Step 2: next refresh wrote rows — streak cleared.
    _set_meta(
        health_mod.feed_registry,
        events_live=1,
        zero_rows_since=None,
        last_refresh_count=12,
        last_refresh_at=_now_iso(),
    )
    assert health_mod._evaluate_leaderboard_silence_alert() is None
    resolves = _resolves(fake_pipe)
    assert len(resolves) == 1
    resolve_query, _ = resolves[0]
    assert "auto-resolved" in resolve_query
    assert "acknowledged = true" in resolve_query


def test_resolution_does_not_fire_when_no_prior_alert(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the streak never crossed the threshold, clearing it must
    NOT trigger an auto-resolve UPDATE — there's nothing to resolve."""
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_THRESHOLD_SECONDS", 600)
    _set_meta(
        health_mod.feed_registry,
        events_live=1,
        zero_rows_since=None,
        last_refresh_count=12,
    )
    assert health_mod._evaluate_leaderboard_silence_alert() is None
    assert _resolves(fake_pipe) == []


def test_new_incident_after_resolve_pages_again(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After auto-resolution, a brand-new streak crossing the
    threshold must page again (one email per *incident*, not one
    email forever)."""
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_THRESHOLD_SECONDS", 600)
    monkeypatch.setattr(health_mod, "LEADERBOARD_SILENCE_ALERT_TTL_SECONDS", 60)

    # First incident — fires.
    _set_meta(
        health_mod.feed_registry,
        events_live=1,
        zero_rows_since=_isoformat(15 * 60),
        last_refresh_at=_now_iso(),
    )
    health_mod._evaluate_leaderboard_silence_alert()
    assert len(_alerts_only(fake_pipe)) == 1

    # Streak clears.
    _set_meta(
        health_mod.feed_registry,
        events_live=1,
        zero_rows_since=None,
        last_refresh_count=8,
    )
    health_mod._evaluate_leaderboard_silence_alert()

    # Expire the dedupe row so the second page isn't suppressed.
    fake_pipe.rows.clear()

    # New incident — fires again.
    _set_meta(
        health_mod.feed_registry,
        events_live=1,
        zero_rows_since=_isoformat(20 * 60),
        last_refresh_at=_now_iso(),
    )
    health_mod._evaluate_leaderboard_silence_alert()
    assert len(_alerts_only(fake_pipe)) == 2


# ---------------------------------------------------------------------------
# Alert helper body
# ---------------------------------------------------------------------------


def test_alert_helper_body_includes_diagnostics() -> None:
    out = _alerts.leaderboard_silent(
        zero_rows_since="2026-04-27T01:00:00+00:00",
        age_seconds=900.0,
        threshold_seconds=600.0,
        events_live=2,
        cadence_seconds=120.0,
        last_refresh_at="2026-04-27T01:14:00+00:00",
        last_success_at="2026-04-27T00:55:00+00:00",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "leaderboard-silent"
    body = out["body_preview"]
    assert "2026-04-27T01:00:00+00:00" in body
    assert "900" in body  # age_seconds
    assert "600" in body  # threshold_seconds
    assert "120" in body  # cadence_seconds
    assert "events_live" in body
    subject = out["subject"]
    assert "LEADERBOARD" in subject
    assert "silent" in subject
    # Pluralization for events_live > 1
    assert "live events" in subject


def test_alert_helper_subject_singular_when_one_live_event() -> None:
    out = _alerts.leaderboard_silent(
        zero_rows_since="2026-04-27T01:00:00+00:00",
        age_seconds=720.0,
        threshold_seconds=600.0,
        events_live=1,
        cadence_seconds=120.0,
        last_refresh_at=None,
        last_success_at=None,
        dry_run=True,
    )
    assert "1 live event" in out["subject"]
    assert "1 live events" not in out["subject"]
