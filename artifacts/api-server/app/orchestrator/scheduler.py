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


# Substring hints used by the vertical-scoped readiness check to find a
# vertical's own discovery feed(s) in the feed registry.  Pipeline-exempt
# verticals (see ``Settings.pipeline_verticals``) unpark on their own
# discovery feed polling instead of waiting for the global READY gate,
# which requires EVERY realtime feed to have polled.
_DISCOVERY_FEED_HINTS: dict[str, tuple[str, ...]] = {
    "weather": ("weather",),
    "crypto": ("crypto",),
    "sports": ("sports", "odds"),
    "commodities": ("commodit",),
    "entertainment": ("entertainment",),
}


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
                self._supervised_loop(vertical),
                name=f"orchestrator-{vertical}",
            )
        log.info(
            "orchestrator scheduler started: verticals=%s interval=%ss",
            list(self._tasks.keys()),
            s.cycle_interval_seconds,
        )
        for name, reason in skipped:
            log.warning("orchestrator vertical %s skipped: %s", name, reason)

    async def _supervised_loop(self, vertical: str) -> None:
        """Self-healing wrapper around :meth:`_loop`.

        ``_loop`` is supposed to never exit on its own — but a stray
        ``CancelledError`` from a sibling task or an exception thrown
        outside the inner ``try`` (e.g. the READY-gate setup) used to
        silently kill the vertical's task, flipping the orchestrator
        health card to ``down`` until the API was restarted.  This
        supervisor restarts the loop after a short back-off so the
        operator dashboard never gets pinned at ORCHESTRATOR DOWN.
        """
        backoff = 5.0
        while not self._stop.is_set():
            try:
                await self._loop(vertical)
                if self._stop.is_set():
                    return
                log.warning(
                    "orchestrator vertical %s loop returned unexpectedly — "
                    "restarting in %ss",
                    vertical, backoff,
                )
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 — supervisor catches all
                log.exception(
                    "orchestrator vertical %s loop crashed — restarting in %ss",
                    vertical, backoff,
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                continue
            else:
                return

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

    async def _vertical_ready(self, vertical: str) -> bool:
        """Vertical-scoped readiness for pipeline-exempt verticals.

        Instead of waiting on the global READY gate -- which parks the
        WHOLE orchestrator if ANY realtime feed misses its first poll --
        wait only for this vertical's own prerequisites:

          * the core boot checks the global gate also requires
            (``migrations``, ``secrets``, ``models``) have completed and
            passed -- we never trade against a half-loaded process; and
          * at least one of the vertical's own discovery feeds has
            produced a first poll outcome (success OR failure).

        Returns True once ready, or False if the scheduler is stopping.
        A core check that has *completed but FAILED* keeps us parked
        (identical to the global gate's behaviour); a feed that never
        polls within ``AXIOM_READY_FEEDS_TIMEOUT_SECONDS`` is logged once
        and then allowed through so a single flaky discovery feed can't
        permanently park the vertical.
        """
        import os

        from .. import ready_gate
        from ..feeds import registry as feed_registry

        hints = _DISCOVERY_FEED_HINTS.get(vertical, (vertical,))
        feed_timeout = float(
            os.environ.get("AXIOM_READY_FEEDS_TIMEOUT_SECONDS") or "120"
        )
        feed_deadline = asyncio.get_event_loop().time() + feed_timeout
        _core = ("migrations", "secrets", "models")
        warned_core = False
        warned_feed = False
        while not self._stop.is_set():
            checks = ready_gate.status()["checks"]
            core = {n: checks.get(n) for n in _core}
            # A completed-but-failed core check => stay parked.
            if any(c is not None and not c["ok"] for c in core.values()):
                if not warned_core:
                    bad = [n for n, c in core.items() if c is not None and not c["ok"]]
                    log.warning(
                        "orchestrator vertical %s parked: core readiness "
                        "check(s) failed: %s",
                        vertical, bad,
                    )
                    warned_core = True
            elif all(c is not None and c["ok"] for c in core.values()):
                snap = feed_registry.snapshot()
                relevant = [
                    f for f in snap
                    if f.get("enabled")
                    and any(h in (f.get("name") or "") for h in hints)
                ]
                feed_ok = any(
                    (f.get("last_ok_at") or f.get("last_error_at"))
                    for f in relevant
                )
                if feed_ok or not relevant:
                    return True
                if asyncio.get_event_loop().time() >= feed_deadline:
                    if not warned_feed:
                        log.warning(
                            "orchestrator vertical %s: discovery feed(s) %s "
                            "not polled within %.0fs — proceeding anyway",
                            vertical,
                            [f.get("name") for f in relevant],
                            feed_timeout,
                        )
                        warned_feed = True
                    return True
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            else:
                return False
        return False

    async def _loop(self, vertical: str) -> None:
        # READY gate: park until migrations have applied, models have
        # loaded, required secrets are present, and every enabled feed
        # has produced at least one poll outcome. Without this the
        # scheduler would race the boot sequence and could fire a
        # trading cycle against half-loaded predictors.
        from .. import ready_gate

        # Pipeline-exempt verticals (e.g. weather) unpark on a
        # vertical-scoped readiness check instead of the global READY
        # gate, so an unrelated realtime feed missing its first poll can
        # never park them.  Everything else waits on the global gate.
        if vertical in get_settings().pipeline_verticals:
            if not await self._vertical_ready(vertical):
                return  # stop event fired
            log.info(
                "orchestrator vertical %s: vertical-scoped readiness cleared "
                "(pipeline-exempt), starting cycle loop",
                vertical,
            )
        else:
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
