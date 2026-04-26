"""Tests for the elo-updater health card + silence/error alert.

Covers ``health._probe_elo_updater`` and the Postmark alert it fans
out via ``notifications.alerts.elo_updater_unhealthy`` when the
periodic team-Elo updater stalls or starts erroring (per task #524).

Three things matter for the on-call signal to be useful:

  1. The card flips to ``degraded`` when the loop is stopped, no
     cycle has completed within ``cycle_seconds * 3``, or the most
     recent cycle returned per-game errors.
  2. Exactly one Postmark alert fires per incident, after a
     consecutive-bad-snapshot threshold so a single missed cycle
     does not page anyone.
  3. The dedupe table suppresses follow-up alerts on the same incident
     within the TTL window, and a recovery resets the counter so a
     future stall has to climb the threshold again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import db as _db
from app import health as health_mod
from app.elo import scheduler as elo_scheduler
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
    health_mod._elo_updater_bad_snapshot_counts.clear()
    # Make sure the disabled env var from a prior test doesn't bleed in.
    monkeypatch.delenv("AXIOM_ELO_UPDATER_DISABLED", raising=False)
    return fake


def _set_status(monkeypatch: pytest.MonkeyPatch, status: dict[str, Any]) -> None:
    monkeypatch.setattr(elo_scheduler.scheduler, "status", lambda: status)


def _alerts_only(fake: _FakeDedupe) -> list[dict[str, Any]]:
    return [d for d in fake.dispatched if d["tag"] == "elo-updater-unhealthy"]


def _iso_ago(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Card status classification
# ---------------------------------------------------------------------------


def test_probe_marks_degraded_when_loop_not_running(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 99)
    _set_status(monkeypatch, {
        "running": False,
        "cycle_seconds": 300.0,
        "last_cycle": None,
    })
    card = health_mod._probe_elo_updater()
    assert card["status"] == "degraded"
    assert "not running" in card["detail"]
    assert card["extras"]["alert_threshold_snapshots"] == 99
    assert card["extras"]["consecutive_bad_snapshots"] == 1
    assert card["extras"]["reason"] == "stalled"


def test_probe_marks_degraded_when_last_cycle_too_stale(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 99)
    _set_status(monkeypatch, {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(2_000),  # 33 min ago, threshold 900s
            "errors": [],
            "games_examined": 1,
            "games_updated": 1,
        },
    })
    card = health_mod._probe_elo_updater()
    assert card["status"] == "degraded"
    assert "stalled" in card["detail"] or "stale" in card["detail"]
    # Threshold = max(cycle_seconds * 3, 60) = 900s
    assert card["extras"]["threshold_seconds"] == pytest.approx(900.0)
    assert card["extras"]["age_seconds"] is not None
    assert card["extras"]["age_seconds"] > 900
    assert card["extras"]["reason"] == "stalled"


def test_probe_marks_degraded_when_last_cycle_returned_errors(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 99)
    _set_status(monkeypatch, {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(60),  # well within threshold
            "errors": ["g1: KeyError(home_team)", "g2: ValueError(score)"],
            "games_examined": 2,
            "games_updated": 0,
        },
    })
    card = health_mod._probe_elo_updater()
    assert card["status"] == "degraded"
    assert "error" in card["detail"].lower()
    assert card["extras"]["reason"] == "errors"


def test_probe_marks_unconfigured_when_disabled_via_env(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AXIOM_ELO_UPDATER_DISABLED", "1")
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 1)
    _set_status(monkeypatch, {
        "running": False,
        "cycle_seconds": 300.0,
        "last_cycle": None,
    })
    card = health_mod._probe_elo_updater()
    assert card["status"] == "unconfigured"
    assert "disabled" in card["detail"]
    # Must not increment the bad-snapshot counter — otherwise an
    # operator who flips the env back off later would see a spurious
    # page on the first un-disabled snapshot.
    assert health_mod._elo_updater_bad_snapshot_counts.get("elo_updater", 0) == 0


def test_probe_marks_ok_when_fresh_and_no_errors(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 99)
    _set_status(monkeypatch, {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(30),
            "errors": [],
            "games_examined": 5,
            "games_updated": 3,
        },
    })
    card = health_mod._probe_elo_updater()
    assert card["status"] == "ok"
    assert card["extras"]["consecutive_bad_snapshots"] == 0
    assert card["extras"]["alert_triggered_this_snapshot"] is False


def test_probe_marks_degraded_when_no_cycle_completed_yet(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 99)
    _set_status(monkeypatch, {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": None,
    })
    card = health_mod._probe_elo_updater()
    assert card["status"] == "degraded"
    assert "no cycle" in card["detail"]
    assert card["extras"]["reason"] == "stalled"


# ---------------------------------------------------------------------------
# Alert dispatch + dedupe
# ---------------------------------------------------------------------------


def test_alert_fires_after_threshold_consecutive_stalled_snapshots(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 3)
    _set_status(monkeypatch, {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(2_000),
            "errors": [],
            "games_examined": 1,
            "games_updated": 0,
        },
    })

    # Two bad snapshots: counter increments, no alert.
    health_mod._probe_elo_updater()
    health_mod._probe_elo_updater()
    assert _alerts_only(fake_pipe) == []

    # Third crosses the threshold -> exactly one alert.
    card = health_mod._probe_elo_updater()
    msgs = _alerts_only(fake_pipe)
    assert len(msgs) == 1
    assert msgs[0]["tag"] == "elo-updater-unhealthy"
    assert "ELO" in msgs[0]["subject"]
    assert "stalled" in msgs[0]["body"] or "stalled" in msgs[0]["subject"] \
        or "last cycle" in msgs[0]["subject"]
    assert card["extras"]["alert_triggered_this_snapshot"] is True


def test_alert_fires_after_threshold_consecutive_error_snapshots(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 2)
    _set_status(monkeypatch, {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(60),
            "errors": ["g1: boom", "g2: kaboom"],
            "games_examined": 2,
            "games_updated": 0,
        },
    })

    health_mod._probe_elo_updater()
    assert _alerts_only(fake_pipe) == []
    health_mod._probe_elo_updater()
    msgs = _alerts_only(fake_pipe)
    assert len(msgs) == 1
    assert "boom" in msgs[0]["body"]


def test_alert_dedupes_within_ttl(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 1)
    _set_status(monkeypatch, {
        "running": False,
        "cycle_seconds": 300.0,
        "last_cycle": None,
    })

    health_mod._probe_elo_updater()
    assert len(_alerts_only(fake_pipe)) == 1

    # Counter would re-trigger on the very next snapshot, but the
    # dedupe row is still live so no second alert fans out.
    health_mod._elo_updater_bad_snapshot_counts.clear()
    health_mod._probe_elo_updater()
    assert len(_alerts_only(fake_pipe)) == 1


def test_recovery_resets_counter_and_requires_full_climb_to_realert(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 3)
    bad = {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(2_000),
            "errors": [],
            "games_examined": 1,
            "games_updated": 0,
        },
    }
    healthy = {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(30),
            "errors": [],
            "games_examined": 5,
            "games_updated": 3,
        },
    }

    _set_status(monkeypatch, bad)
    health_mod._probe_elo_updater()
    health_mod._probe_elo_updater()
    assert _alerts_only(fake_pipe) == []

    # Loop recovers — the counter must drop back to zero.
    _set_status(monkeypatch, healthy)
    health_mod._probe_elo_updater()
    assert health_mod._elo_updater_bad_snapshot_counts.get("elo_updater", 0) == 0

    # Becomes bad again: needs N more bad snapshots, not 1.
    _set_status(monkeypatch, bad)
    health_mod._probe_elo_updater()
    health_mod._probe_elo_updater()
    assert _alerts_only(fake_pipe) == []
    health_mod._probe_elo_updater()
    assert len(_alerts_only(fake_pipe)) == 1


def test_stalled_and_errors_are_paged_independently(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled-then-erroring updater describes two distinct failure
    modes; each gets its own page even though the dedupe TTL would
    suppress a re-page within the same reason."""
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 1)

    # First incident: stalled.
    _set_status(monkeypatch, {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(2_000),
            "errors": [],
            "games_examined": 1,
            "games_updated": 0,
        },
    })
    health_mod._probe_elo_updater()
    assert len(_alerts_only(fake_pipe)) == 1

    # Second incident on the same snapshot stream: errors. Different
    # reason, so the dedupe key differs and a fresh page goes out.
    health_mod._elo_updater_bad_snapshot_counts.clear()
    _set_status(monkeypatch, {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(60),
            "errors": ["g1: boom"],
            "games_examined": 2,
            "games_updated": 0,
        },
    })
    health_mod._probe_elo_updater()
    msgs = _alerts_only(fake_pipe)
    assert len(msgs) == 2


def test_healthy_updater_never_alerts(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 1)
    _set_status(monkeypatch, {
        "running": True,
        "cycle_seconds": 300.0,
        "last_cycle": {
            "completed_at": _iso_ago(30),
            "errors": [],
            "games_examined": 5,
            "games_updated": 3,
        },
    })
    for _ in range(5):
        health_mod._probe_elo_updater()
    assert fake_pipe.dispatched == []


def test_disabled_updater_never_alerts(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AXIOM_ELO_UPDATER_DISABLED", "1")
    monkeypatch.setattr(health_mod, "ELO_UPDATER_ALERT_THRESHOLD", 1)
    _set_status(monkeypatch, {
        "running": False,
        "cycle_seconds": 300.0,
        "last_cycle": None,
    })
    for _ in range(5):
        health_mod._probe_elo_updater()
    assert fake_pipe.dispatched == []


# ---------------------------------------------------------------------------
# Alert helper body
# ---------------------------------------------------------------------------


def test_alert_helper_body_includes_diagnostics() -> None:
    out = _alerts.elo_updater_unhealthy(
        reason="stalled",
        last_completed_at="2026-04-24T00:00:00+00:00",
        age_seconds=2_400.0,
        cycle_seconds=300.0,
        threshold_seconds=900.0,
        running=True,
        last_errors=[],
        games_examined=0,
        games_updated=0,
        consecutive_snapshots=3,
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "elo-updater-unhealthy"
    body = out["body_preview"]
    assert "2400" in body  # age_seconds
    assert "900" in body  # threshold_seconds
    assert "stalled" in body


def test_alert_helper_body_includes_error_samples() -> None:
    out = _alerts.elo_updater_unhealthy(
        reason="errors",
        last_completed_at="2026-04-24T00:05:00+00:00",
        age_seconds=60.0,
        cycle_seconds=300.0,
        threshold_seconds=900.0,
        running=True,
        last_errors=["g1: boom", "g2: kaboom", "g3: pow"],
        games_examined=3,
        games_updated=0,
        consecutive_snapshots=3,
        dry_run=True,
    )
    body = out["body_preview"]
    assert "errors" in body
    assert "g1: boom" in body
    assert "g2: kaboom" in body
