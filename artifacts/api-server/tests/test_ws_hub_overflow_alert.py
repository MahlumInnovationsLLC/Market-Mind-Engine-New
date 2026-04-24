"""Tests for the per-hub WS-overflow alert path.

Verifies that ``health._probe_ws_subscribers`` fans a Postmark alert
keyed on the *hub* (not per-subscriber) when the hub has overflowed
subscribers or sustained >=90% queue depth for N consecutive snapshots.
This complements ``test_ws_silent_alert.py`` which covers the
per-subscription-id signal: the per-hub signal survives subscriber
reconnect, which is the failure mode for misconfigured ceilings on
bursty hubs (kalshi/polymarket orderbooks).
"""
from __future__ import annotations

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
    health_mod._ws_bad_snapshot_counts.clear()
    health_mod._ws_hub_bad_snapshot_counts.clear()
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


def _set_queue_config(
    monkeypatch: pytest.MonkeyPatch, *, hubs: dict[str, int]
) -> None:
    """Stub ``queue_config_snapshot`` so the alert payload has a
    deterministic resolved-maxsize / env-var pair to assert against.
    """
    cfg = {
        "default": 256,
        "global_env": "AXIOM_WS_QUEUE_MAXSIZE",
        "global_env_set": False,
        "by_hub": {
            h: {
                "maxsize": ms,
                "env": f"AXIOM_WS_QUEUE_MAXSIZE_{h.upper().replace('-', '_')}",
                "env_set": False,
            }
            for h, ms in hubs.items()
        },
    }
    from app import ws_backpressure as _wb
    monkeypatch.setattr(_wb, "queue_config_snapshot", lambda: cfg)


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
        "age_seconds": 12.0,
    }


def test_hub_alert_fires_after_threshold_consecutive_snapshots(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_HUB_OVERFLOW_ALERT_THRESHOLD", 3)
    _set_queue_config(monkeypatch, hubs={"kalshi": 256})
    sub = _stuck_sub(sid=1, hub="kalshi", label="orderbook-A")
    _set_summary(monkeypatch, [sub])

    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    hub_alerts = [d for d in fake_pipe.dispatched if d["tag"] == "ws-hub-overflowing"]
    assert hub_alerts == []

    health_mod._probe_ws_subscribers()
    hub_alerts = [d for d in fake_pipe.dispatched if d["tag"] == "ws-hub-overflowing"]
    assert len(hub_alerts) == 1
    msg = hub_alerts[0]
    assert "kalshi" in msg["subject"]
    # Resolved maxsize and override env name must be in the body so the
    # on-call knows what to bump.
    assert "256" in msg["body"]
    assert "AXIOM_WS_QUEUE_MAXSIZE_KALSHI" in msg["body"]


def test_hub_alert_survives_subscriber_reconnect(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-hub counter must not reset when subscriber ids churn.

    Misconfigured ceilings cause overflow + immediate reconnect; the
    per-id counter (used by ws_subscriber_silent) can never accumulate
    in that case, but the per-hub counter should.
    """
    monkeypatch.setattr(health_mod, "WS_HUB_OVERFLOW_ALERT_THRESHOLD", 3)
    monkeypatch.setattr(health_mod, "WS_SILENT_ALERT_THRESHOLD", 999)
    _set_queue_config(monkeypatch, hubs={"polymarket": 128})

    for sid in (10, 11, 12):
        sub = _stuck_sub(
            sid=sid, hub="polymarket", label="orderbook",
            qsize=0, maxsize=128, overflowed=True,
        )
        _set_summary(monkeypatch, [sub])
        health_mod._probe_ws_subscribers()

    hub_alerts = [d for d in fake_pipe.dispatched if d["tag"] == "ws-hub-overflowing"]
    assert len(hub_alerts) == 1
    assert "polymarket" in hub_alerts[0]["subject"]
    assert "128" in hub_alerts[0]["body"]


def test_hub_alert_dedupes_within_ttl(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_HUB_OVERFLOW_ALERT_THRESHOLD", 2)
    _set_queue_config(monkeypatch, hubs={"pipeline": 256})
    sub = _stuck_sub(sid=21, hub="pipeline", label="positions")
    _set_summary(monkeypatch, [sub])

    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    hub_alerts = [d for d in fake_pipe.dispatched if d["tag"] == "ws-hub-overflowing"]
    assert len(hub_alerts) == 1

    # Hub temporarily clears (counter resets) then re-overflows — the
    # in-process counter rebuilds but the persistent dedupe table
    # suppresses the second page within the TTL window.
    healthy = _stuck_sub(sid=22, hub="pipeline", label="positions", qsize=10)
    _set_summary(monkeypatch, [healthy])
    health_mod._probe_ws_subscribers()
    _set_summary(monkeypatch, [sub])
    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    hub_alerts = [d for d in fake_pipe.dispatched if d["tag"] == "ws-hub-overflowing"]
    assert len(hub_alerts) == 1


def test_hub_alert_recovery_resets_counter(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_HUB_OVERFLOW_ALERT_THRESHOLD", 3)
    _set_queue_config(monkeypatch, hubs={"settlement": 256})
    bad = _stuck_sub(sid=31, hub="settlement", label="feed")
    _set_summary(monkeypatch, [bad])
    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    assert "settlement" in health_mod._ws_hub_bad_snapshot_counts

    healthy = _stuck_sub(sid=31, hub="settlement", label="feed", qsize=10)
    _set_summary(monkeypatch, [healthy])
    health_mod._probe_ws_subscribers()
    assert "settlement" not in health_mod._ws_hub_bad_snapshot_counts


def test_hub_alert_skipped_when_all_subscribers_healthy(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_HUB_OVERFLOW_ALERT_THRESHOLD", 1)
    _set_queue_config(monkeypatch, hubs={"llm-logs": 256})
    sub = _stuck_sub(sid=41, hub="llm-logs", label="ops-tail", qsize=10)
    _set_summary(monkeypatch, [sub])
    for _ in range(5):
        health_mod._probe_ws_subscribers()
    hub_alerts = [d for d in fake_pipe.dispatched if d["tag"] == "ws-hub-overflowing"]
    assert hub_alerts == []


def test_hub_alert_separate_hubs_tracked_independently(
    fake_pipe: _FakeDedupe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_mod, "WS_HUB_OVERFLOW_ALERT_THRESHOLD", 2)
    _set_queue_config(monkeypatch, hubs={"kalshi": 256, "polymarket": 256})
    bad_kalshi = _stuck_sub(sid=51, hub="kalshi", label="orderbook")
    healthy_poly = _stuck_sub(sid=52, hub="polymarket", label="orderbook", qsize=5)

    _set_summary(monkeypatch, [bad_kalshi, healthy_poly])
    health_mod._probe_ws_subscribers()
    health_mod._probe_ws_subscribers()
    hub_alerts = [d for d in fake_pipe.dispatched if d["tag"] == "ws-hub-overflowing"]
    assert len(hub_alerts) == 1
    assert "kalshi" in hub_alerts[0]["subject"]
    assert "polymarket" not in hub_alerts[0]["subject"]
