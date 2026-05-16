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
        # Use up to 300 s of jitter (capped at the feed cadence) so that
        # 30+ feeds spread their first poll over 5 minutes rather than
        # 60 s — previously this caused healthz to time out during startup
        # because the event loop was saturated with concurrent outbound
        # HTTP calls immediately after lifespan completion.
        # WATCHDOG_STARTUP_GRACE_SEC is 600 s so all feeds will still
        # fire their first poll before the grace window closes.
        try:
            await asyncio.wait_for(
                self._stop.wait(),
                timeout=random.uniform(0.0, min(300.0, feed.cadence_seconds)),
            )
            return  # stopped before we started
        except asyncio.TimeoutError:
            pass

        if not feed.enabled:
            log.info("feed %s is disabled (%s) — not polling", feed.name, feed.skipped_reason or "no reason")
            return

        while not self._stop.is_set():
            # Honour any provider-issued backoff window before spending a poll.
            status = self._registry.status_of(feed.name)
            if status and status.is_rate_limited():
                # rate_limited_until is wall-clock epoch seconds.
                import time as _time
                wait_s = max(1.0, (status.rate_limited_until or 0.0) - _time.time())
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
                    return  # stop() arrived during backoff
                except asyncio.TimeoutError:
                    pass  # backoff complete, fall through and poll

            started = await self._registry.record_start(feed.name)
            try:
                written = await feed.poll()
                await self._registry.record_success(feed.name, started, int(written or 0))
                log.debug("feed %s ok (%d rows)", feed.name, written or 0)
            except asyncio.CancelledError:
                raise
            except RateLimited as rl:
                await self._registry.record_rate_limited(feed.name, rl.retry_after)
                await self._registry.record_failure(feed.name, started, rl)
            except Exception as e:  # noqa: BLE001 — fault isolation is the whole point
                await self._registry.record_failure(feed.name, started, e)
                # Many production exceptions (httpx ConnectError, ReadTimeout,
                # asyncpg ConnectionDoesNotExistError) stringify to "" which
                # produced the useless "feed X failed: " logs that hid the
                # real root cause for weeks. Always include the exception
                # class name; fall back to repr when str(e) is empty so
                # on-call sees *something* actionable.
                msg = str(e) or repr(e)
                log.warning(
                    "feed %s failed: %s: %s", feed.name, type(e).__name__, msg,
                )

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=feed.cadence_seconds)
            except asyncio.TimeoutError:
                continue


class RateLimited(Exception):
    """Raised by a poll function when the upstream returned 429.

    The scheduler catches this, records a backoff window in FeedStatus,
    and skips polling until the deadline passes.
    """
    def __init__(self, provider: str, retry_after: float = 60.0) -> None:
        super().__init__(f"{provider} rate-limited; retry in {retry_after:.0f}s")
        self.retry_after = retry_after


scheduler = FeedScheduler()
