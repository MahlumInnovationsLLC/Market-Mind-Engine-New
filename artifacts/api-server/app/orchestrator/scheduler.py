"""Per-vertical asyncio scheduler.

One long-running task per vertical wakes every `cycle_interval_seconds`,
runs `run_cycle`, and on exception applies exponential backoff.  Failures
NEVER kill the loop — the task only exits when `stop()` is called.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import get_settings
from .. import verticals as vertical_registry
from .cycle import run_cycle


log = logging.getLogger(__name__)


class _CycleScheduler:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._stop = asyncio.Event()
        self._last_cycle_summary: dict[str, dict[str, Any]] = {}

    def status(self) -> dict[str, Any]:
        s = get_settings()
        return {
            "enabled": s.orchestrator_enabled,
            "trading_mode": s.trading_mode,
            "cycle_interval_seconds": s.cycle_interval_seconds,
            "verticals": list(self._tasks.keys()),
            "registered_verticals": [
                spec.to_dict() for spec in vertical_registry.all_specs()
            ],
            "last_cycle": self._last_cycle_summary,
            "running": any(not t.done() for t in self._tasks.values()),
        }

    async def start(self) -> None:
        s = get_settings()
        if not s.orchestrator_enabled:
            log.info("orchestrator scheduler disabled by config")
            return
        self._stop.clear()
        skipped: list[tuple[str, str]] = []
        for vertical in s.orchestrator_verticals:
            spec = vertical_registry.get_spec(vertical)
            # Unknown names should already have been rejected by
            # config.py validation, but stay defensive in case the
            # registry is mutated at runtime.
            if spec is None:
                skipped.append((vertical, "not registered"))
                continue
            if not spec.enabled:
                skipped.append((vertical, "disabled in registry"))
                continue
            if vertical in self._tasks and not self._tasks[vertical].done():
                continue
            self._tasks[vertical] = asyncio.create_task(
                self._loop(vertical), name=f"orchestrator-{vertical}"
            )
        log.info(
            "orchestrator scheduler started: verticals=%s interval=%ss",
            list(self._tasks.keys()),
            s.cycle_interval_seconds,
        )
        for name, reason in skipped:
            log.warning("orchestrator vertical %s skipped: %s", name, reason)

    async def stop(self) -> None:
        self._stop.set()
        for vertical, t in list(self._tasks.items()):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.warning("scheduler task %s exited with: %s", vertical, e)
        self._tasks.clear()
        log.info("orchestrator scheduler stopped")

    async def _loop(self, vertical: str) -> None:
        # READY gate: park until migrations have applied, models have
        # loaded, required secrets are present, and every enabled feed
        # has produced at least one poll outcome. Without this the
        # scheduler would race the boot sequence and could fire a
        # trading cycle against half-loaded predictors.
        from .. import ready_gate

        ready_wait = asyncio.create_task(ready_gate.wait())
        stop_wait = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            {ready_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if self._stop.is_set():
            return
        log.info(
            "orchestrator vertical %s: ready gate cleared, starting cycle loop",
            vertical,
        )
        backoff = 0.0
        while not self._stop.is_set():
            try:
                cr = await run_cycle(vertical)
                self._last_cycle_summary[vertical] = {
                    "cycle_id": cr.cycle_id,
                    "started_at": cr.started_at,
                    "completed_at": cr.completed_at,
                    "duration_seconds": cr.duration_seconds,
                    "picks_evaluated": cr.picks_evaluated,
                    "trades_placed": cr.trades_placed,
                    "fresh_debates": cr.fresh_debates,
                    "errors": cr.errors,
                }
                backoff = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Failures never kill the loop — back off then retry.
                log.exception("cycle %s failed: %s", vertical, e)
                backoff = min(60.0, max(5.0, backoff * 2 if backoff else 5.0))
            settings = get_settings()
            sleep_s = settings.cycle_interval_seconds + backoff
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass
            else:
                return  # stop event fired


scheduler = _CycleScheduler()
