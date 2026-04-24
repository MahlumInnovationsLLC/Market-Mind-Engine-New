"""Tests for the bankroll-sync silence detector.

Covers ``health._probe_bankroll_sync`` and the Postmark alert it fans
out via ``notifications.alerts.bankroll_sync_silent`` when the
continuous Kalshi balance sync goes quiet (per task #411).

Three things matter for the on-call signal to be useful:

  1. The card flips to ``down`` when the loop is stopped or the last
     tick is older than ``interval_seconds * 3`` so the snapshot
     ``summary.overall`` flips with it.
  2. Exactly one Postmark alert fires per incident, after a
     consecutive-bad-snapshot threshold so a single missed tick does
     not page anyone.
  3. The dedupe table suppresses follow-up alerts on the same incident
     within the TTL window, and a recovery resets the counter so a
     future stall has to climb the threshold again.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import bankroll_sync as bankroll_sync_mod
from app import db as _db
from app import health as health_mod
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

    def _fake_dispatch(subject: str, text_body: str, *, tag: str, dry_run: bool,
                       html_body: str | None = None) -> dict[str, Any]:
        fake.dispatched.append({"subject": subject, "body": text_body, "tag": tag})
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)
    health_mod._bankroll_sync_bad_snapshot_counts.clear()
    return fake


def _set_state(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    monkeypatch.setattr(bankroll_sync_mod, "state", lambda: state)


def _alerts_only(fake: _FakeDedupe) -> list[dict[str, Any]]:
    return [d for d in fake.dispatched if d["tag"] == "bankroll-sync-silent"]


# ---------------------------------------------------------------------------
# Card status classification
# ---------------------------------------------------------------------------


def test_probe_marks_down_when_loop_not_running(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "BANKROLL_SYNC_ALERT_THRESHOLD", 99)
    _set_state(monkeypatch, {
        "running": False,
        "started_at": None,
        "last_tick_at": None,
        "last_error": None,
        "last_balance_cents": None,
        "interval_seconds": 60.0,
    })
    card = health_mod._probe_bankroll_sync()
    assert card["status"] == "down"
    assert "not running" in card["detail"]
    assert card["extras"]["alert_threshold_snapshots"] == 99
    assert card["extras"]["consecutive_bad_snapshots"] == 1


def test_probe_marks_down_when_last_tick_too_stale(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "BANKROLL_SYNC_ALERT_THRESHOLD", 99)
    _set_state(monkeypatch, {
        "running": True,
        "started_at": time.time() - 10_000,
        "last_tick_at": time.time() - 600,  # 10 min ago, threshold is 180s
        "last_error": None,
        "last_balance_cents": 12_500,
        "interval_seconds": 60.0,
    })
    card = health_mod._probe_bankroll_sync()
    assert card["status"] == "down"
    assert "stale" in card["detail"]
    # Threshold = max(interval_seconds * 3, 60) = 180s
    assert card["extras"]["threshold_seconds"] == pytest.approx(180.0)
    assert card["extras"]["age_seconds"] is not None
    assert card["extras"]["age_seconds"] > 180


def test_probe_marks_degraded_when_recovering_from_error(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "BANKROLL_SYNC_ALERT_THRESHOLD", 99)
    _set_state(monkeypatch, {
        "running": True,
        "started_at": time.time() - 100,
        "last_tick_at": time.time() - 5,  # well within threshold
        "last_error": "HTTPError: 503",
        "last_balance_cents": 12_500,
        "interval_seconds": 60.0,
    })
    card = health_mod._probe_bankroll_sync()
    assert card["status"] == "degraded"
    assert "last_error" in card["detail"]


def test_probe_marks_ok_when_fresh_and_no_errors(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "BANKROLL_SYNC_ALERT_THRESHOLD", 99)
    _set_state(monkeypatch, {
        "running": True,
        "started_at": time.time() - 100,
        "last_tick_at": time.time() - 5,
        "last_error": None,
        "last_balance_cents": 12_500,
        "interval_seconds": 60.0,
    })
    card = health_mod._probe_bankroll_sync()
    assert card["status"] == "ok"
    assert card["extras"]["consecutive_bad_snapshots"] == 0
    assert card["extras"]["alert_triggered_this_snapshot"] is False


# ---------------------------------------------------------------------------
# Alert dispatch + dedupe
# ---------------------------------------------------------------------------


def test_alert_fires_after_threshold_consecutive_snapshots(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "BANKROLL_SYNC_ALERT_THRESHOLD", 3)
    _set_state(monkeypatch, {
        "running": True,
        "started_at": time.time() - 10_000,
        "last_tick_at": time.time() - 600,  # stale
        "last_error": None,
        "last_balance_cents": 12_500,
        "interval_seconds": 60.0,
    })

    # Two bad snapshots: counter increments, no alert.
    health_mod._probe_bankroll_sync()
    health_mod._probe_bankroll_sync()
    assert _alerts_only(fake_pipe) == []

    # Third crosses the threshold -> exactly one alert.
    card = health_mod._probe_bankroll_sync()
    msgs = _alerts_only(fake_pipe)
    assert len(msgs) == 1
    assert msgs[0]["tag"] == "bankroll-sync-silent"
    assert "BANKROLL" in msgs[0]["subject"]
    assert "stale" in msgs[0]["subject"] or "last tick" in msgs[0]["subject"]
    assert card["extras"]["alert_triggered_this_snapshot"] is True


def test_alert_dedupes_within_ttl(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "BANKROLL_SYNC_ALERT_THRESHOLD", 1)
    _set_state(monkeypatch, {
        "running": False,
        "started_at": None,
        "last_tick_at": None,
        "last_error": None,
        "last_balance_cents": None,
        "interval_seconds": 60.0,
    })

    health_mod._probe_bankroll_sync()
    assert len(_alerts_only(fake_pipe)) == 1

    # Counter would re-trigger on the very next snapshot, but the
    # dedupe row is still live so no second alert fans out.
    health_mod._bankroll_sync_bad_snapshot_counts.clear()
    health_mod._probe_bankroll_sync()
    assert len(_alerts_only(fake_pipe)) == 1


def test_recovery_resets_counter_and_requires_full_climb_to_realert(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "BANKROLL_SYNC_ALERT_THRESHOLD", 3)
    bad = {
        "running": True,
        "started_at": time.time() - 10_000,
        "last_tick_at": time.time() - 600,
        "last_error": None,
        "last_balance_cents": 12_500,
        "interval_seconds": 60.0,
    }
    healthy = {
        **bad,
        "last_tick_at": time.time() - 5,
    }

    _set_state(monkeypatch, bad)
    health_mod._probe_bankroll_sync()
    health_mod._probe_bankroll_sync()
    assert _alerts_only(fake_pipe) == []

    # Sync recovers — the counter must drop back to zero.
    _set_state(monkeypatch, healthy)
    health_mod._probe_bankroll_sync()
    assert health_mod._bankroll_sync_bad_snapshot_counts.get("bankroll_sync", 0) == 0

    # Becomes bad again: needs N more bad snapshots, not 1.
    _set_state(monkeypatch, bad)
    health_mod._probe_bankroll_sync()
    health_mod._probe_bankroll_sync()
    assert _alerts_only(fake_pipe) == []
    health_mod._probe_bankroll_sync()
    assert len(_alerts_only(fake_pipe)) == 1


def test_degraded_sync_does_not_page(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `degraded` snapshot (loop is up, recently ticked, but the
    last tick raised an error) is real but not silence — the task
    paging spec only fires on stopped or stale syncs. Verify even a
    sustained degraded streak never crosses the alert threshold."""
    monkeypatch.setattr(health_mod, "BANKROLL_SYNC_ALERT_THRESHOLD", 1)
    _set_state(monkeypatch, {
        "running": True,
        "started_at": time.time() - 100,
        "last_tick_at": time.time() - 5,  # fresh
        "last_error": "HTTPError: 503",  # but ticked-with-error
        "last_balance_cents": 12_500,
        "interval_seconds": 60.0,
    })
    for _ in range(5):
        card = health_mod._probe_bankroll_sync()
        assert card["status"] == "degraded"
    assert _alerts_only(fake_pipe) == []
    # Counter must stay zeroed so the alert fires immediately on a
    # later transition into `down`, not after one extra snapshot.
    assert health_mod._bankroll_sync_bad_snapshot_counts.get("bankroll_sync", 0) == 0


def test_healthy_sync_never_alerts(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "BANKROLL_SYNC_ALERT_THRESHOLD", 1)
    _set_state(monkeypatch, {
        "running": True,
        "started_at": time.time() - 100,
        "last_tick_at": time.time() - 5,
        "last_error": None,
        "last_balance_cents": 12_500,
        "interval_seconds": 60.0,
    })
    for _ in range(5):
        health_mod._probe_bankroll_sync()
    assert fake_pipe.dispatched == []


# ---------------------------------------------------------------------------
# Alert helper body
# ---------------------------------------------------------------------------


def test_alert_helper_body_includes_diagnostics() -> None:
    out = _alerts.bankroll_sync_silent(
        last_tick_at="2026-04-24T00:00:00+00:00",
        age_seconds=540.0,
        interval_seconds=60.0,
        threshold_seconds=180.0,
        running=True,
        last_error="HTTPError: 503",
        last_balance_cents=12_500,
        consecutive_snapshots=3,
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "bankroll-sync-silent"
    body = out["body_preview"]
    assert "540" in body  # age_seconds
    assert "180" in body  # threshold_seconds
    assert "HTTPError: 503" in body
    assert "12500" in body or "12,500" in body
