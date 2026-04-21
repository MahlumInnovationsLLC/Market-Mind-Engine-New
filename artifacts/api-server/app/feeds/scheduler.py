"""Async scheduler that ticks every registered Feed on its cadence.

One asyncio task per feed.  All tasks are fault-isolated: an exception
in one feed never affects any other.  Disabled feeds are skipped (they
still appear in the status snapshot so the operator sees *why* they
aren't running).
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from .registry import Feed, FeedRegistry, registry as _default_registry


log = logging.getLogger("axiom.feeds.scheduler")


class FeedScheduler:
    def __init__(self, reg: FeedRegistry | None = None) -> None:
        self._registry = reg or _default_registry
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        for feed in self._registry.feeds():
            self._tasks.append(asyncio.create_task(self._run_feed(feed), name=f"feed:{feed.name}"))
        log.info("feed scheduler started with %d feed(s)", len(self._tasks))

    async def stop(self) -> None:
        if not self._tasks:
            return
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("feed scheduler stopped")

    async def _run_feed(self, feed: Feed) -> None:
        # Stagger start so we don't burst every API at once on cold boot.
        try:
            await asyncio.wait_for(
                self._stop.wait(),
                timeout=random.uniform(0.0, min(5.0, feed.cadence_seconds)),
            )
            return  # stopped before we started
        except asyncio.TimeoutError:
            pass

        if not feed.enabled:
            log.info("feed %s is disabled (%s) — not polling", feed.name, feed.skipped_reason or "no reason")
            return

        while not self._stop.is_set():
            started = await self._registry.record_start(feed.name)
            try:
                written = await feed.poll()
                await self._registry.record_success(feed.name, started, int(written or 0))
                log.debug("feed %s ok (%d rows)", feed.name, written or 0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — fault isolation is the whole point
                await self._registry.record_failure(feed.name, started, e)
                log.warning("feed %s failed: %s", feed.name, e)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=feed.cadence_seconds)
            except asyncio.TimeoutError:
                continue


scheduler = FeedScheduler()
