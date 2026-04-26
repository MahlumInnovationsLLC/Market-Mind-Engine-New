"""Regression tests for the L2 watchdog status-port re-bind fix.

The workflow runner restarts the api-server (and therefore the watchdog
that wraps it) frequently.  Before the fix, the first restart attempt
would race against the previous process's still-bound socket and crash
with ``OSError: [Errno 98] Address already in use`` — which the workflow
runner treated as a failed start until the supervisor retried.

These tests pin two invariants:
  1. The HTTPServer subclass used by ``serve_status`` opts in to
     ``SO_REUSEADDR`` (``allow_reuse_address = True``).
  2. ``serve_status`` retries the bind for several seconds when the
     port is briefly held by another process, instead of giving up
     on the first OSError.
"""
from __future__ import annotations

import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest


_WATCHDOG_PATH = Path(__file__).resolve().parents[1] / "scripts" / "watchdog.py"
_spec = importlib.util.spec_from_file_location("axiom_watchdog_portfix", _WATCHDOG_PATH)
assert _spec and _spec.loader
watchdog = importlib.util.module_from_spec(_spec)
sys.modules["axiom_watchdog_portfix"] = watchdog
_spec.loader.exec_module(watchdog)  # type: ignore[attr-defined]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_state() -> Any:
    return watchdog.WatchdogState(
        target="http://localhost:1/healthz",
        threshold=3,
        interval=1.0,
        max_restarts=3,
        restart_window=60.0,
        backoff_initial=1.0,
        backoff_cap=30.0,
        supervise=False,
    )


def test_reusable_http_server_sets_allow_reuse_address() -> None:
    """The subclass must enable SO_REUSEADDR — that's the entire point."""
    assert watchdog._ReusableHTTPServer.allow_reuse_address is True


def test_serve_status_binds_when_port_is_free() -> None:
    """Sanity: the happy path still works — serve_status should bind and
    return an HTTPServer that we can shut down cleanly."""
    state = _make_state()
    stop_event = threading.Event()
    port = _free_port()
    httpd = watchdog.serve_status(
        state, None, host="127.0.0.1", port=port, stop_event=stop_event,
    )
    try:
        assert httpd is not None
        assert isinstance(httpd, watchdog._ReusableHTTPServer)
    finally:
        stop_event.set()
        httpd.shutdown()
        httpd.server_close()


def test_serve_status_retries_when_port_briefly_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """When another socket holds the port at the moment we try to bind,
    serve_status should retry inside its bind-deadline window rather than
    raising on the first OSError."""
    state = _make_state()
    stop_event = threading.Event()
    port = _free_port()

    # Hold the port briefly with a non-reusable socket so our first bind
    # attempt fails with EADDRINUSE.  Release it after ~0.5s — well inside
    # the bind-deadline retry window in serve_status.
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)

    def _release_after_delay() -> None:
        time.sleep(0.5)
        blocker.close()

    threading.Thread(target=_release_after_delay, daemon=True).start()

    httpd = watchdog.serve_status(
        state, None, host="127.0.0.1", port=port, stop_event=stop_event,
    )
    try:
        assert httpd is not None
    finally:
        stop_event.set()
        httpd.shutdown()
        httpd.server_close()
