"""Background-loop lifecycle registry.

A thin uniform layer over the ~25 long-running background tasks the API
spawns at lifespan boot.  Two things motivated this:

1.  Observability.  Each loop used to be started by an ad-hoc
    ``try: await foo.start() except Exception as e: log.warning(...)``
    block in :mod:`app.main`.  When a loop silently stopped (cancelled,
    crashed, or never actually started) there was no single place to ask
    "what's the state of every background loop?".  The registry tracks
    every register/start/stop attempt and surfaces the result via
    :func:`status` (which :mod:`app.health` wires into the
    ``/api/health/loops`` payload).

2.  Bulkheading.  ``start_all`` walks the registry sequentially and
    *swallows* per-loop start exceptions, so one broken loop can never
    prevent the rest from starting.  ``stop_all`` does the same on the
    way down so a hung ``stop`` does not block process shutdown beyond
    ``per_loop_timeout`` seconds.

Two flavours of loop coexist:

* **Adapted** modules — call :func:`register` with the module's existing
  ``start`` (and optional ``stop``) callables.  The registry tracks
  start/stop outcomes and exposes ``last_error`` / ``state`` /
  ``restart_count`` but leaves the internal asyncio.Task ownership to
  the module.  This is the migration path for the ~25 pre-existing
  loops without a behaviour change.

* **Native** loops — subclass :class:`BackgroundLoop` (see
  :mod:`app.lifecycle.base`) and get cancellation, per-loop restart on
  uncaught exception with exponential backoff, and a ``last_tick_at``
  timestamp updated by the loop body.  New loops should prefer this
  flavour; ``settlement_recent_tap`` is the first migrant.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

log = logging.getLogger("axiom.lifecycle")


Starter = Callable[[], "None | Awaitable[None]"]
Stopper = Callable[[], "None | Awaitable[None]"]


# ---------------------------------------------------------------------------
# Per-loop state
# ---------------------------------------------------------------------------


_STATES = ("registered", "starting", "running", "stopped", "failed")


@dataclass
class LoopHandle:
    """In-memory state for a single registered loop."""

    name: str
    starter: Starter
    stopper: Stopper | None = None
    # Optional callable returning a dict of live metrics (last_tick_at,
    # restart_count, etc).  Native :class:`BackgroundLoop` instances
    # expose this so the registry can surface real ticks; adapted
    # modules typically leave it ``None`` and only report start state.
    metrics_provider: Callable[[], dict] | None = None

    state: str = "registered"
    started_at: float | None = None
    stopped_at: float | None = None
    last_error: str | None = None
    restart_count: int = 0

    def to_dict(self) -> dict:
        # Registry-owned fields — these always win over a metrics
        # provider so the source of truth for "did start/stop succeed?"
        # stays in the registry.
        out: dict = {
            "name": self.name,
            "state": self.state,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_error": self.last_error,
            "restart_count": self.restart_count,
        }
        if self.metrics_provider is not None:
            try:
                extras = self.metrics_provider() or {}
            except Exception as e:  # pragma: no cover — defensive
                out["metrics_error"] = f"{type(e).__name__}: {e}"
                return out
            # Runtime fields that belong to the loop body, not the
            # registry — let native loops surface their live values.
            # (For an adapted loop with no metrics_provider, the
            # registry-owned values above are still authoritative.)
            for runtime_key in ("last_tick_at", "restart_count", "last_error"):
                if runtime_key in extras and extras[runtime_key] is not None:
                    out[runtime_key] = extras[runtime_key]
            # Pass through any other metrics a native loop exposes.
            for k, v in extras.items():
                if k not in out:
                    out[k] = v
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class LoopRegistry:
    """Process-wide registry of background loops.

    Not thread-safe — lifespan boot is single-threaded and ``start_all``
    is awaited from the FastAPI lifespan context.  All state is plain
    asyncio.
    """

    def __init__(self) -> None:
        self._loops: dict[str, LoopHandle] = {}

    # -- registration ----------------------------------------------------

    def register(
        self,
        name: str,
        starter: Starter,
        stopper: Stopper | None = None,
        *,
        metrics_provider: Callable[[], dict] | None = None,
        replace: bool = False,
    ) -> LoopHandle:
        """Register a loop.

        ``starter`` and ``stopper`` may be sync or async callables; the
        registry awaits them as appropriate.  When ``replace`` is False
        (default) a duplicate registration logs a warning and returns
        the existing handle — the test suite reloads modules a lot and
        we don't want double-registration to throw.
        """
        existing = self._loops.get(name)
        if existing is not None and not replace:
            log.warning("lifecycle.register: %r already registered", name)
            return existing
        handle = LoopHandle(
            name=name,
            starter=starter,
            stopper=stopper,
            metrics_provider=metrics_provider,
        )
        self._loops[name] = handle
        return handle

    def unregister(self, name: str) -> None:
        self._loops.pop(name, None)

    def clear(self) -> None:
        """Test-only — drop every registration."""
        self._loops.clear()

    # -- inspection ------------------------------------------------------

    def get(self, name: str) -> LoopHandle | None:
        return self._loops.get(name)

    def names(self) -> list[str]:
        return list(self._loops.keys())

    def status(self) -> dict[str, dict]:
        """Snapshot of every registered loop."""
        return {name: h.to_dict() for name, h in self._loops.items()}

    # -- lifecycle -------------------------------------------------------

    async def start_one(self, name: str) -> bool:
        """Start a single registered loop.  Returns True on success."""
        handle = self._loops.get(name)
        if handle is None:
            raise KeyError(f"unknown loop: {name!r}")
        handle.state = "starting"
        try:
            result = handle.starter()
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            handle.state = "failed"
            handle.last_error = f"{type(e).__name__}: {e}"
            log.warning("lifecycle.start[%s] failed: %s", name, handle.last_error)
            return False
        handle.state = "running"
        handle.started_at = time.time()
        handle.last_error = None
        return True

    async def stop_one(self, name: str, *, timeout: float = 10.0) -> bool:
        """Stop a single registered loop.  Returns True on clean stop."""
        handle = self._loops.get(name)
        if handle is None:
            raise KeyError(f"unknown loop: {name!r}")
        if handle.stopper is None:
            handle.state = "stopped"
            handle.stopped_at = time.time()
            return True
        try:
            result = handle.stopper()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=timeout)
        except asyncio.TimeoutError:
            handle.state = "failed"
            handle.last_error = f"stop timed out after {timeout:.1f}s"
            log.warning("lifecycle.stop[%s] timed out", name)
            return False
        except Exception as e:
            handle.state = "failed"
            handle.last_error = f"stop: {type(e).__name__}: {e}"
            log.warning("lifecycle.stop[%s] failed: %s", name, handle.last_error)
            return False
        handle.state = "stopped"
        handle.stopped_at = time.time()
        return True

    async def restart_one(self, name: str, *, stop_timeout: float = 10.0) -> bool:
        """Stop+start a single loop.  Increments ``restart_count`` on
        success.  Used by tests and by the operator
        ``/api/health/loops/{name}/restart`` endpoint."""
        handle = self._loops.get(name)
        if handle is None:
            raise KeyError(f"unknown loop: {name!r}")
        await self.stop_one(name, timeout=stop_timeout)
        ok = await self.start_one(name)
        if ok:
            handle.restart_count += 1
        return ok

    async def start_all(self, *, only: Iterable[str] | None = None) -> dict[str, bool]:
        """Start every registered loop.  Bulkhead: per-loop exceptions
        are swallowed so a single bad loop can never block the rest of
        boot.  Returns ``{name: success}``.
        """
        targets = list(only) if only is not None else self.names()
        out: dict[str, bool] = {}
        for name in targets:
            out[name] = await self.start_one(name)
        return out

    async def stop_all(self, *, per_loop_timeout: float = 10.0) -> dict[str, bool]:
        """Stop every registered loop in reverse-registration order."""
        out: dict[str, bool] = {}
        for name in reversed(self.names()):
            out[name] = await self.stop_one(name, timeout=per_loop_timeout)
        return out


# Process-wide singleton.  Tests can `from app.lifecycle import registry`
# and call `registry.clear()` before each case.
registry = LoopRegistry()


def register(
    name: str,
    starter: Starter,
    stopper: Stopper | None = None,
    *,
    metrics_provider: Callable[[], dict] | None = None,
    replace: bool = False,
) -> LoopHandle:
    """Module-level convenience wrapper around :meth:`LoopRegistry.register`."""
    return registry.register(
        name, starter, stopper,
        metrics_provider=metrics_provider, replace=replace,
    )


async def start_all(*, only: Iterable[str] | None = None) -> dict[str, bool]:
    return await registry.start_all(only=only)


async def stop_all(*, per_loop_timeout: float = 10.0) -> dict[str, bool]:
    return await registry.stop_all(per_loop_timeout=per_loop_timeout)


def status() -> dict[str, dict]:
    return registry.status()


__all__ = [
    "BackgroundLoop",
    "LoopHandle",
    "LoopRegistry",
    "register",
    "registry",
    "start_all",
    "status",
    "stop_all",
]


# Imported at the bottom so `app.lifecycle.base` can import the
# registry above.
from .base import BackgroundLoop  # noqa: E402
