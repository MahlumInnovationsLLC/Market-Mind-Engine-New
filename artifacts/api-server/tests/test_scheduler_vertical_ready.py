"""Vertical-scoped readiness for pipeline-exempt verticals.

``_CycleScheduler._vertical_ready`` lets a pipeline-exempt vertical
(e.g. weather) unpark on its OWN discovery feed instead of the global
READY gate, which parks the whole orchestrator if any realtime feed
misses its first poll.  Core boot checks (migrations/secrets/models)
still gate it, identical to the global gate's fail-closed behaviour.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from app import ready_gate
from app.feeds import registry as feed_registry

scheduler_mod = importlib.import_module("app.orchestrator.scheduler")


@pytest.fixture(autouse=True)
def _reset_gate():
    ready_gate.begin()
    yield
    ready_gate._checks = {}
    ready_gate._event = None


def _core_ok():
    ready_gate.mark_check("migrations", ok=True)
    ready_gate.mark_check("secrets", ok=True)
    ready_gate.mark_check("models", ok=True)


def _feed(name: str, polled: bool) -> dict:
    return {
        "name": name,
        "enabled": True,
        "last_ok_at": 1.0 if polled else None,
        "last_error_at": None,
    }


def test_ready_when_core_ok_and_discovery_feed_polled(monkeypatch):
    _core_ok()
    monkeypatch.setattr(
        feed_registry,
        "snapshot",
        lambda: [_feed("kalshi_weather_markets", True), _feed("the_odds_api", False)],
    )

    async def _run():
        sched = scheduler_mod._CycleScheduler()
        return await sched._vertical_ready("weather")

    assert asyncio.run(_run()) is True


def test_failed_first_poll_still_counts_as_polled(monkeypatch):
    # A discovery feed that polled and ERRORED is still a completed first
    # poll — the vertical should unpark (mirrors the global gate).
    _core_ok()
    snap = [{"name": "kalshi_weather_markets", "enabled": True,
             "last_ok_at": None, "last_error_at": 2.0}]
    monkeypatch.setattr(feed_registry, "snapshot", lambda: snap)

    async def _run():
        sched = scheduler_mod._CycleScheduler()
        return await sched._vertical_ready("weather")

    assert asyncio.run(_run()) is True


def test_parked_while_core_check_failed(monkeypatch):
    ready_gate.mark_check("migrations", ok=True)
    ready_gate.mark_check("secrets", ok=True)
    ready_gate.mark_check("models", ok=False, detail="boom")
    monkeypatch.setattr(
        feed_registry, "snapshot",
        lambda: [_feed("kalshi_weather_markets", True)],
    )

    async def _run():
        sched = scheduler_mod._CycleScheduler()
        task = asyncio.create_task(sched._vertical_ready("weather"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
        sched._stop.set()
        return await task

    assert asyncio.run(_run()) is False


def test_returns_false_on_stop(monkeypatch):
    # Core never completes => never ready; stop must unblock as False.
    monkeypatch.setattr(feed_registry, "snapshot", lambda: [])

    async def _run():
        sched = scheduler_mod._CycleScheduler()
        task = asyncio.create_task(sched._vertical_ready("weather"))
        await asyncio.sleep(0.05)
        sched._stop.set()
        return await task

    assert asyncio.run(_run()) is False


def test_proceeds_when_no_relevant_discovery_feed(monkeypatch):
    # No weather feed registered at all => nothing to wait on; proceed
    # once the core checks pass rather than parking forever.
    _core_ok()
    monkeypatch.setattr(
        feed_registry, "snapshot",
        lambda: [_feed("the_odds_api", False), _feed("kalshi_sports_markets", True)],
    )

    async def _run():
        sched = scheduler_mod._CycleScheduler()
        return await sched._vertical_ready("weather")

    assert asyncio.run(_run()) is True
