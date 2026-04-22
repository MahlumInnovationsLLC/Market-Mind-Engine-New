"""Periodic AutoResearch loop.

Same shape as :mod:`app.settlement.scheduler` — single asyncio task,
explicit ``start()``/``stop()``, exponential backoff on failure. Runs
``run_mining()`` once every ``AXIOM_AUTORESEARCH_INTERVAL_SECS`` (default
24h) so the candidate-feature ranking stays fresh against new outcomes.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .mining import MiningResult, run_mining


log = logging.getLogger("axiom.autoresearch.scheduler")


def _interval_seconds() -> float:
    # Default 24h (nightly). Tests override via env.
    return max(60.0, float(os.environ.get("AXIOM_AUTORESEARCH_INTERVAL_SECS") or "86400"))


def _enabled() -> bool:
    return (os.environ.get("AXIOM_AUTORESEARCH_DISABLED") or "").strip().lower() not in {
        "1", "true", "yes",
    }


class _AutoResearchScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._last: dict[str, Any] = {}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": _enabled(),
            "running": bool(self._task and not self._task.done()),
            "interval_seconds": _interval_seconds(),
            "last_run": self._last,
        }

    async def start(self) -> None:
        if not _enabled():
            log.info("autoresearch scheduler disabled by env")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="autoresearch-loop")
        log.info(
            "autoresearch scheduler started: interval=%.0fs", _interval_seconds()
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.warning("autoresearch task exit error: %s", e)
        self._task = None
        log.info("autoresearch scheduler stopped")

    async def run_now(self) -> MiningResult:
        """Trigger one mining pass synchronously (admin endpoint)."""
        result = await run_mining()
        self._last = {
            "started_at": result.started_at,
            "completed_at": result.completed_at,
            "duration_seconds": result.duration_seconds,
            "verticals_examined": result.verticals_examined,
            "candidates_evaluated": result.candidates_evaluated,
            "candidates_persisted": result.candidates_persisted,
            "insufficient_samples": result.insufficient_samples,
            "errors": list(result.errors),
        }
        return result

    async def _loop(self) -> None:
        backoff = 0.0
        while not self._stop.is_set():
            try:
                await self.run_now()
                backoff = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("autoresearch cycle failed: %s", e)
                backoff = min(3600.0, max(60.0, backoff * 2 if backoff else 60.0))
            sleep_s = _interval_seconds() + backoff
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass
            else:
                return


scheduler = _AutoResearchScheduler()
