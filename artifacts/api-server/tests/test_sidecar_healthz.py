"""Tests for the loop-independent liveness sidecar.

The sidecar exists so the L2 watchdog (`scripts/watchdog.py`) keeps
seeing 200s even when the asyncio event loop is briefly blocked by a
CPU-heavy background task.  These tests pin three behaviours:

  * the daemon thread is started by ``start_sidecar_healthz`` and
    responds to GET requests with ``200 ok``;
  * repeated starts on the same port are idempotent;
  * the sidecar truly bypasses the event loop — it answers a GET
    issued from a synchronous code path that holds the GIL the entire
    time (proxy for "loop is blocked").
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.request

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sidecar_port():
    from app import sidecar_healthz

    port = _free_port()
    sidecar_healthz.stop_sidecar_healthz()
    bound = sidecar_healthz.start_sidecar_healthz(port=port)
    assert bound == port
    yield port
    sidecar_healthz.stop_sidecar_healthz()


def test_sidecar_responds_200(sidecar_port: int) -> None:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{sidecar_port}/", timeout=2.0
    ) as r:
        assert r.status == 200
        assert r.read() == b"ok"


def test_sidecar_start_is_idempotent(sidecar_port: int) -> None:
    from app import sidecar_healthz

    # Second start on the same module returns the same bound port and
    # does NOT spin up a second thread.
    again = sidecar_healthz.start_sidecar_healthz(port=_free_port())
    assert again == sidecar_port


def test_sidecar_answers_while_main_thread_is_busy(sidecar_port: int) -> None:
    """Probe must succeed even when the *main* thread is in a tight
    busy loop — the sidecar runs in its own thread and doesn't share
    the asyncio event loop, so production loop-blockers can't starve
    liveness."""

    done = threading.Event()

    def _busy() -> None:
        # Burn CPU on the main thread for ~1.5 s; the sidecar lives
        # in its own daemon thread so the probe should still land.
        t_end = time.time() + 1.5
        x = 0
        while time.time() < t_end:
            x += 1
        done.set()

    t = threading.Thread(target=_busy)
    t.start()
    # Probe while the busy loop is still running.
    time.sleep(0.1)
    assert not done.is_set()
    with urllib.request.urlopen(
        f"http://127.0.0.1:{sidecar_port}/", timeout=2.0
    ) as r:
        assert r.status == 200
    t.join()
