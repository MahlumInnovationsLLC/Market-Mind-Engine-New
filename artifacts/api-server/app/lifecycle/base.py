"""Base class for native lifecycle loops.

Subclasses provide :meth:`tick` (one iteration) and the base handles:

* cancellation on :meth:`stop`,
* restart-on-uncaught with capped exponential backoff so a chronic
  failure does not turn into a tight crash loop,
* a ``last_tick_at`` clock the registry surfaces via
  ``/api/health/loops``.

Native loops are wired into :mod:`app.lifecycle` by calling
``loop.register()`` — that hands the lifecycle methods to the registry
as the ``starter`` / ``stopper`` / ``metrics_provider`` triple.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

log = logging.getLogger("axiom.lifecycle.loop")


# Module-level so tests can monkeypatch them — production code never
# overrides these directly.
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0


class BackgroundLoop:
    """Run :meth:`tick` forever, restarting on uncaught exceptions.

    Subclasses MUST implement :meth:`tick`.  The default :meth:`run`
    invokes ``tick`` in an outer try-loop: a clean return from a tick
    is treated as "do another one now" (use ``await asyncio.sleep`` in
    ``tick`` to pace).  ``asyncio.CancelledError`` propagates so the
    asyncio task ends cleanly on :meth:`stop`.
    """

    #: Public name surfaced in healthz.  Required.
    name: str = ""

    #: Cap on auto-restart attempts before the loop gives up and goes
    #: ``failed``.  ``None`` means unlimited (default — a per-loop
    #: crash should never silently disable the loop forever, but the
    #: backoff keeps the restart rate sane).
    max_restarts: int | None = None

    def __init__(self) -> None:
        if not self.name:
            raise RuntimeError(f"{type(self).__name__}.name must be set")
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_tick_at: float | None = None
        self._restart_count: int = 0
        self._last_error: str | None = None

    # -- subclass hook ---------------------------------------------------

    async def tick(self) -> None:
        """One unit of work.  Subclasses override."""
        raise NotImplementedError

    # -- bookkeeping subclasses may call --------------------------------

    def _mark_tick(self) -> None:
        """Subclasses call this after each successful unit of work."""
        self._last_tick_at = time.time()

    # -- driver ----------------------------------------------------------

    async def _run(self) -> None:
        backoff = INITIAL_BACKOFF_SECONDS
        while not self._stop_event.is_set():
            try:
                await self.tick()
                # A successful tick resets the backoff so a transient
                # blip doesn't poison future iterations.
                backoff = INITIAL_BACKOFF_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                self._restart_count += 1
                log.warning(
                    "lifecycle[%s] tick raised (#%d): %s; backoff=%.1fs",
                    self.name, self._restart_count, self._last_error, backoff,
                )
                if (
                    self.max_restarts is not None
                    and self._restart_count >= self.max_restarts
                ):
                    log.error(
                        "lifecycle[%s] hit max_restarts=%d — giving up",
                        self.name, self.max_restarts,
                    )
                    # Surface exhaustion on the registry handle so
                    # ``/api/health/loops`` flips this loop to
                    # ``failed`` instead of leaving it stuck at
                    # ``running`` (the registry's start_one() already
                    # returned True long before the loop crashed).
                    try:
                        from . import registry as _registry
                        handle = _registry.get(self.name)
                        if handle is not None:
                            handle.state = "failed"
                            handle.last_error = self._last_error
                    except Exception:  # pragma: no cover — defensive
                        pass
                    return
                # Sleep with a touch of jitter so a coordinated upstream
                # outage doesn't synchronize every loop's retry.
                jitter = random.uniform(0.0, backoff * 0.25)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=backoff + jitter,
                    )
                    return  # stop() was called while we were sleeping
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0, MAX_BACKOFF_SECONDS)

    # -- public lifecycle ------------------------------------------------

    async def start(self) -> None:
        """Spawn the loop task.  Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()  # reset on every start
        self._task = asyncio.create_task(self._run(), name=f"loop:{self.name}")

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Cancel the loop task and wait for it.  Idempotent."""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(self._await_task()), timeout=timeout
            )
        except asyncio.TimeoutError:
            log.warning("lifecycle[%s] stop timed out after %.1fs", self.name, timeout)
        finally:
            self._task = None

    async def _await_task(self) -> None:
        if self._task is None:
            return
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            return

    # -- metrics for the registry ----------------------------------------

    def metrics(self) -> dict[str, Any]:
        return {
            "last_tick_at": self._last_tick_at,
            "restart_count": self._restart_count,
            "last_error": self._last_error,
        }

    # -- registry helper -------------------------------------------------

    def register(self) -> None:
        """Hand ``self`` to the process-wide registry."""
        # Imported lazily to avoid the package __init__ -> base cycle
        # at module-load time.
        from . import register as _register
        _register(
            self.name,
            self.start,
            self.stop,
            metrics_provider=self.metrics,
            replace=True,
        )
