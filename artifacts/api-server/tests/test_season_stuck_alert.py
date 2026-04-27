"""Tests for the Sportradar soccer "season id stuck" escalation alert.

Covers the time-based dispatcher in ``health._maybe_dispatch_season_stuck_alert``
and the Postmark alert it fans out via ``notifications.alerts.season_stuck``
when a soccer feed reports ``season_stuck=True`` continuously for more
than the configured threshold (default 60 minutes).

The signal must:

  1. Stay quiet while the feed is freshly stuck (under threshold) so a
     transient 404 doesn't page anyone.
  2. Fire exactly once per incident after the threshold lapses.
  3. Reset cleanly when the feed recovers, so a future stuck event has
     to wait the full threshold again.
  4. Surface the league, the cached/stuck season id, and the most
     recent ``season_last_refresh_outcome`` in the alert payload.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app import health as health_mod
from app.feeds.registry import Feed, FeedRegistry
from app.notifications import alerts as _alerts


class _FakeDedupe:
    def __init__(self) -> None:
        self.rows: dict[str, datetime] = {}
        self.dispatched: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "notification_dedupe" in query
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        if key not in self.rows or self.rows[key] <= now:
            self.rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None


@pytest.fixture
def fake_pipe(monkeypatch: pytest.MonkeyPatch) -> _FakeDedupe:
    fake = _FakeDedupe()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)

    def _fake_dispatch(
        subject: str,
        text_body: str,
        *,
        tag: str,
        dry_run: bool,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        fake.dispatched.append(
            {"subject": subject, "body": text_body, "tag": tag}
        )
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)
    health_mod._season_stuck_first_seen_at.clear()
    return fake


def _stuck_alerts(fake: _FakeDedupe) -> list[dict[str, Any]]:
    return [d for d in fake.dispatched if d["tag"] == "season-stuck"]


def _stuck_meta(**overrides: Any) -> dict[str, Any]:
    base = {
        "season_id": "sr:season:STUCK",
        "season_previous_id": "sr:season:STUCK",
        "season_stuck": True,
        "season_last_refresh_outcome": "same_id",
        "season_last_refresh_at": "2026-04-26T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _isolated_registry_with_stuck_feed(
    monkeypatch: pytest.MonkeyPatch, *, feed_name: str = "sportradar_epl_injuries",
    meta: dict[str, Any] | None = None,
) -> FeedRegistry:
    reg = FeedRegistry()

    async def _ok():
        return 0

    reg.register(
        Feed(
            name=feed_name,
            cadence_seconds=900.0,
            poll=_ok,
            enabled=True,
        )
    )
    st = reg._status[feed_name]  # type: ignore[attr-defined]
    st.poll_count = 1
    st.success_count = 1
    st.last_ok_at = 1_700_000_100.0
    st.last_finished_at = 1_700_000_100.0
    st.metadata = meta if meta is not None else _stuck_meta()
    monkeypatch.setattr(health_mod, "feed_registry", reg)
    return reg


# ---------------------------------------------------------------------------
# Dispatcher behaviour
# ---------------------------------------------------------------------------


def test_dispatcher_records_first_seen_but_does_not_alert_below_threshold(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health_mod, "SEASON_STUCK_ALERT_THRESHOLD_SECONDS", 3600
    )
    triggered = health_mod._maybe_dispatch_season_stuck_alert(
        feed_name="sportradar_epl_injuries", meta=_stuck_meta(), now=1_000_000.0
    )
    # Below threshold: no alert and no claim against the dedupe table.
    assert triggered is None
    assert _stuck_alerts(fake_pipe) == []
    # First-seen wall-clock recorded for next cycle.
    assert health_mod._season_stuck_first_seen_at[
        "sportradar_epl_injuries"
    ] == 1_000_000.0


def test_dispatcher_fires_once_after_threshold_elapses(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health_mod, "SEASON_STUCK_ALERT_THRESHOLD_SECONDS", 3600
    )
    name = "sportradar_epl_injuries"

    # Snapshot 1 — record first-seen, no alert.
    assert (
        health_mod._maybe_dispatch_season_stuck_alert(
            feed_name=name, meta=_stuck_meta(), now=1_000_000.0
        )
        is None
    )
    assert _stuck_alerts(fake_pipe) == []

    # Snapshot 2 — 30 minutes in, still under threshold, no alert.
    assert (
        health_mod._maybe_dispatch_season_stuck_alert(
            feed_name=name, meta=_stuck_meta(), now=1_000_000.0 + 1800
        )
        is None
    )
    assert _stuck_alerts(fake_pipe) == []

    # Snapshot 3 — 60 minutes in, threshold crossed -> exactly one alert.
    triggered = health_mod._maybe_dispatch_season_stuck_alert(
        feed_name=name, meta=_stuck_meta(), now=1_000_000.0 + 3600
    )
    assert triggered is not None
    msgs = _stuck_alerts(fake_pipe)
    assert len(msgs) == 1
    assert msgs[0]["tag"] == "season-stuck"
    assert "epl" in msgs[0]["subject"].lower()


def test_dispatcher_dedupes_within_ttl(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health_mod, "SEASON_STUCK_ALERT_THRESHOLD_SECONDS", 60
    )
    name = "sportradar_epl_injuries"

    # First crossing: alert fires.
    health_mod._maybe_dispatch_season_stuck_alert(
        feed_name=name, meta=_stuck_meta(), now=1_000_000.0
    )
    health_mod._maybe_dispatch_season_stuck_alert(
        feed_name=name, meta=_stuck_meta(), now=1_000_000.0 + 65
    )
    assert len(_stuck_alerts(fake_pipe)) == 1

    # Subsequent snapshots while still stuck must not re-page on the
    # same incident (dedupe row is live).
    for offset in (120, 1800, 7200):
        health_mod._maybe_dispatch_season_stuck_alert(
            feed_name=name, meta=_stuck_meta(), now=1_000_000.0 + offset
        )
    assert len(_stuck_alerts(fake_pipe)) == 1


def test_recovery_clears_first_seen_and_requires_full_climb_to_realert(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health_mod, "SEASON_STUCK_ALERT_THRESHOLD_SECONDS", 60
    )
    name = "sportradar_epl_injuries"

    # Record stuck.
    health_mod._maybe_dispatch_season_stuck_alert(
        feed_name=name, meta=_stuck_meta(), now=1_000_000.0
    )
    assert name in health_mod._season_stuck_first_seen_at

    # Recovery wipes the first-seen marker.
    healthy = {**_stuck_meta(), "season_stuck": False}
    assert (
        health_mod._maybe_dispatch_season_stuck_alert(
            feed_name=name, meta=healthy, now=1_000_000.0 + 30
        )
        is None
    )
    assert name not in health_mod._season_stuck_first_seen_at

    # Becomes stuck again — first-seen restarts from the new `now`,
    # so a snapshot 30s later (well past the original threshold start)
    # must still not page; only a full threshold's worth past the
    # *new* first-seen counts.
    health_mod._maybe_dispatch_season_stuck_alert(
        feed_name=name, meta=_stuck_meta(), now=1_000_000.0 + 1000
    )
    assert _stuck_alerts(fake_pipe) == []
    health_mod._maybe_dispatch_season_stuck_alert(
        feed_name=name, meta=_stuck_meta(), now=1_000_000.0 + 1000 + 30
    )
    assert _stuck_alerts(fake_pipe) == []
    health_mod._maybe_dispatch_season_stuck_alert(
        feed_name=name, meta=_stuck_meta(), now=1_000_000.0 + 1000 + 65
    )
    assert len(_stuck_alerts(fake_pipe)) == 1


def test_dispatcher_no_op_for_unstuck_feed(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health_mod, "SEASON_STUCK_ALERT_THRESHOLD_SECONDS", 60
    )
    healthy = {**_stuck_meta(), "season_stuck": False}
    for offset in range(0, 3600, 600):
        out = health_mod._maybe_dispatch_season_stuck_alert(
            feed_name="sportradar_epl_injuries",
            meta=healthy,
            now=1_000_000.0 + offset,
        )
        assert out is None
    assert fake_pipe.dispatched == []
    assert (
        "sportradar_epl_injuries"
        not in health_mod._season_stuck_first_seen_at
    )


# ---------------------------------------------------------------------------
# `_probe_feeds` integration — surface state on the card extras.
# ---------------------------------------------------------------------------


def test_probe_feeds_surfaces_stuck_alert_state(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health_mod, "SEASON_STUCK_ALERT_THRESHOLD_SECONDS", 3600
    )
    _isolated_registry_with_stuck_feed(monkeypatch)
    cards = health_mod._probe_feeds()
    assert len(cards) == 1
    extras = cards[0]["extras"]
    assert extras["season_stuck_since"] is not None
    assert extras["season_stuck_seconds"] is not None
    assert extras["season_stuck_alert_threshold_seconds"] == 3600
    # Below threshold on first snapshot — no alert yet.
    assert extras["season_stuck_alert_triggered_this_snapshot"] is False
    assert _stuck_alerts(fake_pipe) == []


def test_probe_feeds_clears_first_seen_on_recovery(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health_mod, "SEASON_STUCK_ALERT_THRESHOLD_SECONDS", 3600
    )
    reg = _isolated_registry_with_stuck_feed(monkeypatch)
    health_mod._probe_feeds()
    assert "sportradar_epl_injuries" in health_mod._season_stuck_first_seen_at

    # Flip metadata to unstuck and re-probe — first-seen must clear.
    st = reg._status["sportradar_epl_injuries"]  # type: ignore[attr-defined]
    st.metadata = {**st.metadata, "season_stuck": False}
    cards = health_mod._probe_feeds()
    extras = cards[0]["extras"]
    assert extras["season_stuck_since"] is None
    assert (
        "sportradar_epl_injuries"
        not in health_mod._season_stuck_first_seen_at
    )


# ---------------------------------------------------------------------------
# Alert helper body — must name the league, the stuck id, and the
# most recent refresh outcome.
# ---------------------------------------------------------------------------


def test_alert_helper_body_includes_league_id_and_outcome() -> None:
    out = _alerts.season_stuck(
        feed_name="sportradar_epl_injuries",
        league="epl",
        season_id="sr:season:STUCK",
        previous_season_id="sr:season:STUCK",
        refresh_outcome="same_id",
        last_refresh_at="2026-04-26T00:00:00+00:00",
        stuck_since="2026-04-25T22:00:00+00:00",
        stuck_seconds=3900.0,
        threshold_seconds=3600.0,
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "season-stuck"
    subject = out["subject"]
    assert "epl" in subject.lower()
    assert "same_id" in subject
    body = out["body_preview"]
    assert "sportradar_epl_injuries" in body
    assert "sr:season:STUCK" in body
    assert "same_id" in body
    assert "2026-04-26T00:00:00+00:00" in body
    assert "65" in body or "65.0" in body  # stuck_minutes ~ 65
    assert "60" in body or "60.0" in body  # threshold_minutes
