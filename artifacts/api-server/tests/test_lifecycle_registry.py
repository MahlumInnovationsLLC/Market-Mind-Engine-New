"""Tests for `app.lifecycle` — the per-loop background-task registry.

These exercise the *framework* in isolation:

* register / start / stop / restart bookkeeping is right.
* one crashing loop does not kill the others (bulkhead).
* a `BackgroundLoop` subclass that raises is restarted with backoff
  and stops cleanly on cancel.

No live DB, no live event hub, no FastAPI app — the goal is to prove
the lifecycle machinery itself is sound; per-loop integration is the
job of each loop's own test file.
"""

from __future__ import annotations

import asyncio

import pytest

from app import lifecycle as lc
from app.lifecycle import base as lc_base


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_registry():
    lc.registry.clear()
    yield
    lc.registry.clear()


# ---------------------------------------------------------------------------
# Adapted loops — `register(name, start, stop)` with arbitrary callables
# ---------------------------------------------------------------------------


def test_register_then_status_lists_the_loop():
    async def _start(): pass
    lc.register("alpha", _start)
    s = lc.status()
    assert "alpha" in s
    assert s["alpha"]["state"] == "registered"
    assert s["alpha"]["restart_count"] == 0


def test_start_one_marks_running_and_records_started_at():
    calls = []
    async def _start(): calls.append("started")
    lc.register("beta", _start)
    ok = _run(lc.registry.start_one("beta"))
    assert ok is True
    assert calls == ["started"]
    h = lc.registry.get("beta")
    assert h is not None
    assert h.state == "running"
    assert h.started_at is not None
    assert h.last_error is None


def test_start_one_swallows_exception_and_flags_failed():
    async def _bad():
        raise RuntimeError("boom")
    lc.register("gamma", _bad)
    ok = _run(lc.registry.start_one("gamma"))
    assert ok is False
    h = lc.registry.get("gamma")
    assert h.state == "failed"
    assert "RuntimeError" in (h.last_error or "")
    assert "boom" in (h.last_error or "")


def test_start_all_bulkheads_one_crashing_loop():
    """Critical: one bad loop must not block the others."""
    started: list[str] = []
    async def _good_a(): started.append("a")
    async def _bad():     raise RuntimeError("nope")
    async def _good_c(): started.append("c")
    lc.register("a", _good_a)
    lc.register("crash", _bad)
    lc.register("c", _good_c)
    out = _run(lc.start_all())
    assert out == {"a": True, "crash": False, "c": True}
    assert started == ["a", "c"]


def test_sync_starter_is_also_supported():
    """Some legacy modules (e.g. dead_letter_sweeper) have sync `start()`."""
    state = {"started": False}
    def _start_sync():
        state["started"] = True
    lc.register("sync_loop", _start_sync)
    ok = _run(lc.registry.start_one("sync_loop"))
    assert ok is True
    assert state["started"] is True


def test_stop_one_runs_stopper_and_marks_stopped():
    stops: list[str] = []
    async def _start(): pass
    async def _stop(): stops.append("done")
    lc.register("delta", _start, _stop)
    _run(lc.registry.start_one("delta"))
    _run(lc.registry.stop_one("delta"))
    assert stops == ["done"]
    h = lc.registry.get("delta")
    assert h.state == "stopped"
    assert h.stopped_at is not None


def test_stop_one_timeout_marks_failed_without_hanging():
    async def _start(): pass
    async def _slow_stop():
        await asyncio.sleep(60)
    lc.register("hang", _start, _slow_stop)
    _run(lc.registry.start_one("hang"))
    import time
    t0 = time.monotonic()
    ok = _run(lc.registry.stop_one("hang", timeout=0.2))
    elapsed = time.monotonic() - t0
    assert ok is False
    assert elapsed < 1.0, f"stop_one did not respect timeout (took {elapsed:.2f}s)"
    h = lc.registry.get("hang")
    assert h.state == "failed"
    assert "timed out" in (h.last_error or "")


def test_restart_one_increments_restart_count():
    started = []
    async def _start(): started.append("s")
    async def _stop():  started.append("x")
    lc.register("eps", _start, _stop)
    _run(lc.registry.start_one("eps"))
    assert lc.registry.get("eps").restart_count == 0
    _run(lc.registry.restart_one("eps"))
    assert lc.registry.get("eps").restart_count == 1
    assert started == ["s", "x", "s"]


def test_restart_one_on_unknown_raises_key_error():
    with pytest.raises(KeyError):
        _run(lc.registry.restart_one("does_not_exist"))


def test_duplicate_register_warns_and_keeps_original():
    async def _a(): pass
    async def _b(): pass
    h1 = lc.register("dup", _a)
    h2 = lc.register("dup", _b)
    assert h1 is h2  # same handle, second registration ignored


def test_duplicate_register_with_replace_swaps_in_new_handle():
    async def _a(): pass
    async def _b(): pass
    h1 = lc.register("dup2", _a)
    h2 = lc.register("dup2", _b, replace=True)
    assert h2 is not h1
    assert lc.registry.get("dup2") is h2


# ---------------------------------------------------------------------------
# BackgroundLoop — native loops with restart-on-uncaught + backoff
# ---------------------------------------------------------------------------


class _CountingLoop(lc.BackgroundLoop):
    name = "counting"
    def __init__(self):
        super().__init__()
        self.ticks = 0
    async def tick(self):
        self.ticks += 1
        self._mark_tick()
        await asyncio.sleep(0.01)


def test_background_loop_starts_runs_and_stops_cleanly():
    async def _go():
        loop = _CountingLoop()
        await loop.start()
        await asyncio.sleep(0.05)
        await loop.stop(timeout=1.0)
        return loop
    loop = _run(_go())
    assert loop.ticks >= 2
    assert loop._last_tick_at is not None
    assert loop._last_error is None
    assert loop._restart_count == 0


class _AlwaysCrashLoop(lc.BackgroundLoop):
    name = "always_crash"
    max_restarts = 3
    async def tick(self):
        raise RuntimeError("kaboom")


def test_background_loop_restarts_then_gives_up_at_max():
    """A loop that crashes every tick must give up at max_restarts —
    proves the backoff loop doesn't busy-loop forever AND that the
    restart cap is honoured."""
    # Shrink the backoff so the test stays under a second.
    orig_initial = lc_base.INITIAL_BACKOFF_SECONDS
    orig_max = lc_base.MAX_BACKOFF_SECONDS
    lc_base.INITIAL_BACKOFF_SECONDS = 0.01
    lc_base.MAX_BACKOFF_SECONDS = 0.02
    try:
        async def _go():
            loop = _AlwaysCrashLoop()
            await loop.start()
            # Wait long enough for max_restarts hits + the final return.
            await asyncio.sleep(0.3)
            await loop.stop(timeout=1.0)
            return loop
        loop = _run(_go())
        assert loop._restart_count >= 3
        assert "RuntimeError" in (loop._last_error or "")
    finally:
        lc_base.INITIAL_BACKOFF_SECONDS = orig_initial
        lc_base.MAX_BACKOFF_SECONDS = orig_max


class _CancellableLoop(lc.BackgroundLoop):
    name = "cancellable"
    async def tick(self):
        # Long sleep that should be interrupted by .stop()
        await asyncio.sleep(60)


def test_background_loop_stop_cancels_within_timeout():
    async def _go():
        loop = _CancellableLoop()
        await loop.start()
        await asyncio.sleep(0.02)
        import time
        t0 = time.monotonic()
        await loop.stop(timeout=1.0)
        return time.monotonic() - t0
    elapsed = _run(_go())
    assert elapsed < 0.5, f"stop took too long ({elapsed:.2f}s)"


def test_background_loop_self_registers_into_registry():
    loop = _CountingLoop()
    loop.register()
    assert "counting" in lc.registry.names()
    h = lc.registry.get("counting")
    assert h.metrics_provider is not None
    metrics = h.metrics_provider()
    assert "last_tick_at" in metrics
    assert "restart_count" in metrics


def test_background_loop_to_dict_merges_metrics_into_status():
    loop = _CountingLoop()
    loop.register()
    snap = lc.status()["counting"]
    # State-derived keys always win over metrics-provider keys, but
    # the live tick/restart fields show up.
    assert "last_tick_at" in snap
    assert snap["state"] == "registered"


def test_native_metrics_override_registry_runtime_fields():
    """Regression: ``to_dict`` previously suppressed metrics-provider
    keys for `restart_count` / `last_error` because the registry's
    own (start-time) values shadowed them.  Native loops own the
    runtime view — make sure tick crashes are visible in healthz."""
    orig_initial = lc_base.INITIAL_BACKOFF_SECONDS
    lc_base.INITIAL_BACKOFF_SECONDS = 0.01
    try:
        async def _go():
            loop = _AlwaysCrashLoop()
            loop.register()
            await lc.registry.start_one("always_crash")
            await asyncio.sleep(0.25)
            await lc.registry.stop_one("always_crash", timeout=1.0)
            return loop
        loop = _run(_go())
        snap = lc.status()["always_crash"]
        # Runtime counters from the loop body must surface, not the
        # registry's start-time zeros.
        assert snap["restart_count"] >= 1, snap
        assert "RuntimeError" in (snap["last_error"] or ""), snap
        assert snap["restart_count"] == loop._restart_count
    finally:
        lc_base.INITIAL_BACKOFF_SECONDS = orig_initial


def test_background_loop_flips_handle_to_failed_on_max_restarts():
    """Regression: when a native loop exhausts ``max_restarts`` and
    exits, the registry handle must transition to ``failed`` so
    ``/api/health/loops`` reflects reality (the registry's
    ``start_one`` already returned True long before the crash)."""
    orig_initial = lc_base.INITIAL_BACKOFF_SECONDS
    orig_max = lc_base.MAX_BACKOFF_SECONDS
    lc_base.INITIAL_BACKOFF_SECONDS = 0.01
    lc_base.MAX_BACKOFF_SECONDS = 0.02
    try:
        async def _go():
            loop = _AlwaysCrashLoop()
            loop.register()
            assert lc.registry.get("always_crash").state == "registered"
            await lc.registry.start_one("always_crash")
            # Right after start_one returns, the loop is "running" —
            # the crashes happen asynchronously.
            assert lc.registry.get("always_crash").state == "running"
            await asyncio.sleep(0.3)
        _run(_go())
        h = lc.registry.get("always_crash")
        assert h.state == "failed", h.state
        assert "RuntimeError" in (h.last_error or "")
    finally:
        lc_base.INITIAL_BACKOFF_SECONDS = orig_initial
        lc_base.MAX_BACKOFF_SECONDS = orig_max


# ---------------------------------------------------------------------------
# Chaos: one BackgroundLoop crashing must not stop the registry's other
# loops from continuing to tick.
# ---------------------------------------------------------------------------


def test_one_crashing_loop_does_not_stop_the_others():
    """Cross-loop bulkhead: a healthy loop must keep ticking even
    while a sibling crashes on every tick.  Mirrors the production
    invariant the per-loop registry exists to enforce."""
    # Shrink the backoff so the test runs fast.
    orig_initial = lc_base.INITIAL_BACKOFF_SECONDS
    lc_base.INITIAL_BACKOFF_SECONDS = 0.01
    try:
        async def _go():
            good = _CountingLoop()
            bad = _AlwaysCrashLoop()
            good.register()
            bad.register()
            await lc.start_all()
            await asyncio.sleep(0.2)
            await lc.stop_all(per_loop_timeout=1.0)
            return good, bad
        good, bad = _run(_go())
        assert good.ticks >= 3, f"healthy loop only ticked {good.ticks} times"
        assert bad._restart_count >= 1
    finally:
        lc_base.INITIAL_BACKOFF_SECONDS = orig_initial
