"""Bounded-backpressure tests for the in-process WS fan-out hubs.

Covers:
  * Per-subscriber queue is hard-bounded by ``maxsize``.
  * ``policy="drop_oldest"`` (default) keeps the producer hot and the
    consumer eventually consistent.
  * ``policy="close"`` pushes a sentinel that surfaces as
    ``SubscriberOverflow`` to the consumer.
  * Registry snapshot exposes per-connection queue depth + drop counts
    so /api/health/snapshot can render them.
  * Load test: a single stuck subscriber receiving ``50_000`` events
    cannot OOM the publisher — its queue stays pinned at ``maxsize``.
"""
from __future__ import annotations

import asyncio
import resource
import sys

import pytest

from app.ws_backpressure import (
    CLOSE,
    DROP_OLDEST,
    Subscription,
    SubscriberOverflow,
    registry_snapshot,
    registry_summary,
)
from app.orchestrator.streams import _PipelineEventHub
from app.settlement.streams import _Hub as _SettlementHub
from app.kalshi.bus import _Bus as _KalshiBus


def _run(coro):
    """Helper — every test gets a fresh event loop (matches the
    `asyncio.run()` pattern used elsewhere in this suite).
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Subscription primitives
# ---------------------------------------------------------------------------


def test_subscription_drop_oldest_caps_queue():
    async def go():
        sub = Subscription(hub="t", label="cap", maxsize=4, policy=DROP_OLDEST)
        for i in range(100):
            sub.offer({"i": i})
        # Hard-bounded — never grows past maxsize regardless of producer rate.
        assert sub.qsize() == 4
        seen = []
        while sub.qsize():
            seen.append((await sub.get())["i"])
        # Newest events win; older ones are evicted.
        assert seen == [96, 97, 98, 99]
        # Evictions are counted so /api/health/snapshot can show them.
        assert sub.dropped == 96
        assert sub.overflowed is False
    _run(go())


def test_subscription_close_on_overflow_signals_consumer():
    async def go():
        sub = Subscription(hub="t", label="close", maxsize=2, policy=CLOSE)
        sub.offer({"i": 0})
        sub.offer({"i": 1})
        # 3rd offer exceeds the cap and must mark the subscriber as
        # overflowed; the consumer wakes up with a SubscriberOverflow.
        sub.offer({"i": 2})
        assert sub.overflowed is True
        # The publisher evicted the oldest buffered event to make room
        # for the close sentinel — that's how we guarantee the
        # consumer wakes up instead of deadlocking on a full queue.
        assert (await sub.get())["i"] == 1
        with pytest.raises(SubscriberOverflow):
            await sub.get()
        # Subsequent offers are silent no-ops (subscriber is closed).
        sub.offer({"i": 999})
        assert sub.qsize() == 0
    _run(go())


def test_subscription_rejects_invalid_policy_and_size():
    with pytest.raises(ValueError):
        Subscription(hub="t", label="x", maxsize=4, policy="nonsense")
    with pytest.raises(ValueError):
        Subscription(hub="t", label="x", maxsize=0, policy=DROP_OLDEST)


# ---------------------------------------------------------------------------
# Hub integration
# ---------------------------------------------------------------------------


def test_pipeline_hub_supports_maxsize_and_policy():
    async def go():
        hub = _PipelineEventHub()
        sub = hub.subscribe(maxsize=8, policy=CLOSE, label="t-pipe")
        try:
            assert sub.policy == CLOSE
            assert sub.maxsize == 8
            assert sub.label == "t-pipe"
            assert sub.hub == "pipeline"
        finally:
            hub.unsubscribe(sub)
    _run(go())


def test_pipeline_hub_drops_for_stuck_subscriber():
    async def go():
        hub = _PipelineEventHub()
        slow = hub.subscribe(maxsize=4, label="slow")
        try:
            for i in range(50):
                await hub.publish({"type": "tick", "i": i})
            assert slow.qsize() == 4
            assert slow.dropped == 46
            seen = []
            while slow.qsize():
                seen.append((await slow.get())["i"])
            # Most recent 4 events survive.
            assert seen == [46, 47, 48, 49]
        finally:
            hub.unsubscribe(slow)
    _run(go())


def test_settlement_hub_close_policy_surfaces_overflow():
    async def go():
        hub = _SettlementHub()
        sub = hub.subscribe(maxsize=3, policy=CLOSE, label="t-settle")
        try:
            for i in range(10):
                await hub.publish({"type": "tick", "i": i})
            assert sub.overflowed is True
            # 0,1,2 fill the queue; the 4th publish evicts 0 to make
            # room for the close sentinel, leaving 1, 2, sentinel.
            for expected in (1, 2):
                ev = await sub.get()
                assert ev["i"] == expected
            with pytest.raises(SubscriberOverflow):
                await sub.get()
        finally:
            hub.unsubscribe(sub)
    _run(go())


def test_kalshi_bus_close_policy_surfaces_overflow():
    async def go():
        bus = _KalshiBus(max_queue=3)
        sub = await bus.subscribe(policy=CLOSE, label="t-kalshi")
        try:
            for i in range(10):
                await bus.publish({"type": "tick", "i": i})
            assert sub.overflowed is True
        finally:
            await bus.unsubscribe(sub)
    _run(go())


# ---------------------------------------------------------------------------
# Health snapshot wiring
# ---------------------------------------------------------------------------


def test_health_snapshot_card_shape():
    """The /api/health/snapshot collector ships a `ws_backpressure`
    card so operators can see queue depth without scraping logs.  Lock
    the card's shape here so the shape can't drift silently.
    """
    from app.health import _probe_ws_subscribers
    from app.orchestrator.streams import _PipelineEventHub

    async def go():
        hub = _PipelineEventHub()
        full = hub.subscribe(maxsize=4, label="health-card-full")
        try:
            for i in range(20):
                await hub.publish({"type": "x", "i": i})
            card = _probe_ws_subscribers()
            assert card["key"] == "ws_backpressure"
            assert card["category"] == "passive"
            # Subscriber pinned at maxsize -> degraded.
            assert card["status"] == "degraded"
            assert "subscribers" in card["extras"]
            assert "by_hub" in card["extras"]
            # Our just-created subscriber is in the registry.
            ours = [
                s for s in card["extras"]["subscribers"]
                if s["label"] == "health-card-full"
            ]
            assert ours, "expected our subscriber in the snapshot"
            entry = ours[0]
            for k in (
                "id", "hub", "label", "policy", "maxsize",
                "qsize", "dropped", "overflowed", "age_seconds",
            ):
                assert k in entry, f"missing field: {k}"
            assert entry["qsize"] == 4
            assert entry["dropped"] == 16
        finally:
            hub.unsubscribe(full)
    _run(go())


def test_registry_snapshot_reports_per_connection_depth():
    async def go():
        hub = _PipelineEventHub()
        a = hub.subscribe(maxsize=4, label="alpha-snap")
        b = hub.subscribe(maxsize=4, label="beta-snap")
        try:
            for i in range(10):
                await hub.publish({"type": "x", "i": i})
            snaps = registry_snapshot()
            ours = {
                s["label"]: s
                for s in snaps
                if s["hub"] == "pipeline"
                and s["label"] in ("alpha-snap", "beta-snap")
            }
            assert ours["alpha-snap"]["qsize"] == 4
            assert ours["alpha-snap"]["maxsize"] == 4
            assert ours["alpha-snap"]["dropped"] == 6
            assert ours["alpha-snap"]["overflowed"] is False
            assert ours["beta-snap"]["dropped"] == 6
            # Per-hub aggregation so the snapshot card can render
            # `{hub: {count, qsize_total, dropped_total}}` directly.
            summary = registry_summary()
            assert summary["by_hub"]["pipeline"]["count"] >= 2
            assert summary["by_hub"]["pipeline"]["dropped_total"] >= 12
        finally:
            hub.unsubscribe(a)
            hub.unsubscribe(b)
    _run(go())


# ---------------------------------------------------------------------------
# Load test — a single stuck subscriber cannot OOM the publisher.
# ---------------------------------------------------------------------------


def test_stuck_subscriber_does_not_oom_publisher():
    """Publish 50k chunky events to a subscriber that never drains.
    RSS growth must stay under 50 MiB and the queue must stay pinned
    at ``maxsize``.  Without the bound this would OOM or grow RSS by
    hundreds of MiB.
    """
    if sys.platform == "win32":  # pragma: no cover — RSS reporting differs
        pytest.skip("RSS sampling not portable on Windows")

    async def go():
        hub = _PipelineEventHub()
        stuck = hub.subscribe(maxsize=128, label="stuck-load")
        try:
            rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            payload = "x" * 4096  # ~4 KiB per event
            N = 50_000
            for i in range(N):
                await hub.publish({"type": "tick", "i": i, "blob": payload})
            rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # ru_maxrss is KiB on Linux, bytes on macOS — normalise to MiB.
            unit = 1024 if sys.platform == "darwin" else 1
            rss_growth_mib = (rss_after - rss_before) * unit / (1024 * 1024)
            assert stuck.qsize() == 128, "queue must stay pinned at maxsize"
            # Without the cap, RSS would balloon by ~200+ MiB
            # (50_000 × ~4 KiB).  The cap holds us to a few MiB at most.
            assert rss_growth_mib < 50, f"rss grew {rss_growth_mib:.1f} MiB"
            # The drop counter sanity-checks the policy fired the
            # expected number of times.
            assert stuck.dropped == N - 128
        finally:
            hub.unsubscribe(stuck)
    _run(go())
