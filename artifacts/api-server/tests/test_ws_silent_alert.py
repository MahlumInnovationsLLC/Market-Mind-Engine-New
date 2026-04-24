"""Tests for the WS-subscriber-silent alert path.

Verifies that ``health._probe_ws_subscribers`` fans out a Postmark alert
through the existing alert pipeline when a subscriber stays >=90% full
or overflowed for N consecutive snapshots, and that the dedupe table
suppresses follow-up alerts on the same (hub, label) within the TTL.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db, health as health_mod
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

    def _fake_dispatch(subject: str, text_body: str, *, tag: str, dry_run: bool):
        fake.dispatched.append({"subject": subject, "body": text_body, "tag": tag})
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)
    # Reset per-test consecutive counters on the health module.
    health_mod._ws_bad_snapshot_counts.clear()
    return fake


def _set_summary(monkeypatch: pytest.MonkeyPatch, subs: list[dict]) -> None:
    by_hub: dict[str, dict[str, int]] = {}
    for s in subs:
        h = by_hub.setdefault(
            s["hub"], {"count": 0, "qsize_total": 0,
                       "dropped_total": 0, "overflowed": 0}
        )
        h["count"] += 1
        h["qsize_total"] += int(s["qsize"])
        h["dropped_total"] += int(s["dropped"])
        if s["overflowed"]:
            h["overflowed"] += 1
    summary = {
        "total_subscribers": len(subs),
        "by_hub": by_hub,
        "subscribers": subs,
    }
    from app import ws_backpressure as _wb
    monkeypatch.setattr(_wb, "registry_summary", lambda: summary)


def _stuck_sub(*, sid: int, hub: str, label: str, qsize: int = 240,
               maxsize: int = 256, overflowed: bool = False) -> dict:
    return {
        "id": sid,
        "hub": hub,
        "label": label,
        "policy": "drop_oldest",
        "maxsize": maxsize,
        "qsize": qsize,
        "dropped": 0,
        "overflowed": overflowed,
        "age_seconds": 120.0,
    }


def test_alert_fires_after_threshold_consecutive_snapshots(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_SILENT_ALERT_THRESHOLD", 3)
    sub = _stuck_sub(sid=11, hub="pipeline_event_hub", label="positions")
    _set_summary(monkeypatch, [sub])

    # First two snapshots: counter increments, no alert.
    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    assert fake_pipe.dispatched == []

    # Third snapshot crosses the threshold -> one alert.
    health_mod._probe_ws_subscribers()
    assert len(fake_pipe.dispatched) == 1
    msg = fake_pipe.dispatched[0]
    assert msg["tag"] == "ws-subscriber-silent"
    assert "pipeline_event_hub" in msg["subject"]
    assert "positions" in msg["subject"]
    assert "pipeline_event_hub" in msg["body"]
    assert "positions" in msg["body"]


def test_alert_dedupes_within_ttl(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_SILENT_ALERT_THRESHOLD", 2)
    sub = _stuck_sub(sid=22, hub="kalshi_bus", label="ws-stream")
    _set_summary(monkeypatch, [sub])

    # Cross the threshold once.
    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    assert len(fake_pipe.dispatched) == 1

    # Subscriber reconnects with a new id but same (hub, label) — the
    # counter restarts and would re-trigger but the dedupe key suppresses.
    health_mod._ws_bad_snapshot_counts.clear()
    sub2 = _stuck_sub(sid=23, hub="kalshi_bus", label="ws-stream")
    _set_summary(monkeypatch, [sub2])
    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    assert len(fake_pipe.dispatched) == 1


def test_recovery_resets_counter(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_SILENT_ALERT_THRESHOLD", 3)
    bad = _stuck_sub(sid=33, hub="settlement_event_hub", label="feed")
    _set_summary(monkeypatch, [bad])
    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    assert fake_pipe.dispatched == []

    # Subscriber drains.
    healthy = _stuck_sub(
        sid=33, hub="settlement_event_hub", label="feed", qsize=10
    )
    _set_summary(monkeypatch, [healthy])
    health_mod._probe_ws_subscribers()
    assert 33 not in health_mod._ws_bad_snapshot_counts

    # Becomes bad again — needs N more bad snapshots, not 1.
    _set_summary(monkeypatch, [bad])
    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    assert fake_pipe.dispatched == []
    health_mod._probe_ws_subscribers()
    assert len(fake_pipe.dispatched) == 1


def test_overflowed_subscriber_triggers_alert(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_SILENT_ALERT_THRESHOLD", 1)
    sub = _stuck_sub(
        sid=44, hub="llm_log_hub", label="ops-tail",
        qsize=0, maxsize=256, overflowed=True,
    )
    _set_summary(monkeypatch, [sub])
    health_mod._probe_ws_subscribers()
    assert len(fake_pipe.dispatched) == 1
    assert "overflowed" in fake_pipe.dispatched[0]["subject"]


def test_healthy_subscriber_never_alerts(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_SILENT_ALERT_THRESHOLD", 2)
    sub = _stuck_sub(
        sid=55, hub="pipeline_event_hub", label="positions", qsize=10,
    )
    _set_summary(monkeypatch, [sub])
    for _ in range(5):
        health_mod._probe_ws_subscribers()
    assert fake_pipe.dispatched == []


def test_snapshot_exposes_stuck_subscribers_for_banner(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /api/health/snapshot card must surface the same "stuck"
    list that drives the email alert so the operator dashboard banner
    can render it without re-implementing the threshold logic."""
    monkeypatch.setattr(health_mod, "WS_SILENT_ALERT_THRESHOLD", 2)
    sub = _stuck_sub(sid=66, hub="pipeline_event_hub", label="positions")
    _set_summary(monkeypatch, [sub])

    # Below threshold — banner should see no stuck subs yet.
    card = health_mod._probe_ws_subscribers()
    extras = card["extras"]
    assert extras["silent_alert_threshold"] == 2
    assert extras["stuck_subscribers"] == []

    # Crossing the threshold surfaces the offending sub on the snapshot.
    card = health_mod._probe_ws_subscribers()
    extras = card["extras"]
    stuck = extras["stuck_subscribers"]
    assert len(stuck) == 1
    assert stuck[0]["hub"] == "pipeline_event_hub"
    assert stuck[0]["label"] == "positions"
    assert stuck[0]["consecutive_snapshots"] >= 2


def test_snapshot_clears_stuck_when_subscriber_recovers(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_SILENT_ALERT_THRESHOLD", 2)
    bad = _stuck_sub(sid=77, hub="kalshi_bus", label="ws-stream")
    _set_summary(monkeypatch, [bad])
    health_mod._probe_ws_subscribers()
    card = health_mod._probe_ws_subscribers()
    assert len(card["extras"]["stuck_subscribers"]) == 1

    healthy = _stuck_sub(
        sid=77, hub="kalshi_bus", label="ws-stream", qsize=10,
    )
    _set_summary(monkeypatch, [healthy])
    card = health_mod._probe_ws_subscribers()
    assert card["extras"]["stuck_subscribers"] == []
