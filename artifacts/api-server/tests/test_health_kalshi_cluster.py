"""Tests for the Kalshi cluster auth probe surfaced on /api/health."""
from __future__ import annotations

import time

from app import health as health_mod
from app.kalshi import client as kalshi_client


def _reset_probe_state() -> None:
    kalshi_client._cluster_probe_state.clear()
    kalshi_client._cluster_probe_state["checked"] = False


def test_unconfigured_card_when_probe_has_not_run() -> None:
    _reset_probe_state()
    card = health_mod._probe_kalshi_cluster()
    assert card["key"] == "kalshi_cluster"
    assert card["status"] == "unconfigured"
    # Even before the probe runs we surface the configured cadence so
    # the dashboard can compute a "stale" threshold consistently.
    assert (
        card["extras"]["probe_interval_seconds"]
        == kalshi_client.KALSHI_CLUSTER_PROBE_INTERVAL_SECONDS
    )


def test_card_extras_carry_probe_interval_and_checked_at() -> None:
    """The dashboard renders 'last checked Xm ago' from these fields."""
    _reset_probe_state()
    now = time.time()
    kalshi_client._record_cluster_probe_result(
        {
            "ok": True,
            "configured_env": "prod",
            "key_belongs_to": "prod",
            "detail": "authenticated against cluster 'prod'",
        }
    )
    card = health_mod._probe_kalshi_cluster()
    assert card["status"] == "ok"
    extras = card["extras"]
    # The cached probe result already stamps these — re-surfacing them
    # in extras is what lets the UI render a relative timestamp.
    assert extras["checked"] is True
    assert isinstance(extras["checked_at_epoch"], float)
    assert abs(extras["checked_at_epoch"] - now) < 5
    assert isinstance(extras["checked_at"], str)
    # Stale-detection threshold (3x interval) is computed in the UI
    # from this field — it must always be present.
    assert (
        extras["probe_interval_seconds"]
        == kalshi_client.KALSHI_CLUSTER_PROBE_INTERVAL_SECONDS
    )


def test_failed_probe_still_includes_probe_interval() -> None:
    _reset_probe_state()
    kalshi_client._record_cluster_probe_result(
        {
            "ok": False,
            "configured_env": "prod",
            "key_belongs_to": "demo",
            "error": (
                "Kalshi credentials look valid but were rejected by "
                "cluster 'prod' — the key authenticates against 'demo'."
            ),
        }
    )
    card = health_mod._probe_kalshi_cluster()
    assert card["status"] == "down"
    assert (
        card["extras"]["probe_interval_seconds"]
        == kalshi_client.KALSHI_CLUSTER_PROBE_INTERVAL_SECONDS
    )
    # checked_at fields survive on failure too — operators need to
    # tell "wrong cluster, just checked" apart from "wrong cluster,
    # last checked an hour ago".
    assert card["extras"]["checked_at"]
    assert card["extras"]["checked_at_epoch"]
