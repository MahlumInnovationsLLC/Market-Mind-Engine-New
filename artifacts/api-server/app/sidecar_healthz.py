"""Loop-independent liveness sidecar.

Spawns a tiny stdlib ``http.server`` on a dedicated port in a daemon
thread.  The server returns ``200 OK`` on every request and shares no
state with the FastAPI app, so it answers even when the asyncio event
loop is fully blocked by CPU work, a stuck await, or a long-running
synchronous call sneaked into a background task.

Production wires the L2 watchdog (`scripts/watchdog.py`) at this
sidecar so a brief loop stall no longer escalates to a SIGKILL +
process restart (the dominant cause of user-visible outages on the
uptime monitor — every restart cost the platform a 15-30 s cold-boot
window during which all requests, not just the blocked ones, failed).

Liveness through this sidecar means "the process is alive"; the
asyncpg pool, model registry, and feed health are still gated by the
existing ``/api/health/ready`` readiness endpoint served by FastAPI.
"""

from __future__ import annotations

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("axiom.sidecar_healthz")


class _LivenessHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "2")
        self.end_headers()
        try:
            self.wfile.write(b"ok")
        except BrokenPipeError:
            pass

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib signature
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Silence the default stderr access log — would otherwise flood
        # the watchdog probe cadence (every ~5 s) into prod logs.
        return


_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def start_sidecar_healthz(port: int | None = None) -> int | None:
    """Start the sidecar HTTP server on a daemon thread.

    Idempotent — repeated calls are a no-op.  Returns the bound port,
    or ``None`` if the server could not be started (e.g. port already
    in use after a hot reload).  The port defaults to
    ``HEALTHZ_SIDECAR_PORT`` env, falling back to ``8081``.
    """
    global _server, _thread
    with _lock:
        if _server is not None:
            return _server.server_port
        chosen = port if port is not None else int(
            os.environ.get("HEALTHZ_SIDECAR_PORT", "8081")
        )
        try:
            server = ThreadingHTTPServer(("0.0.0.0", chosen), _LivenessHandler)
        except OSError as e:
            log.warning(
                "sidecar healthz could not bind 0.0.0.0:%s (%s) — "
                "watchdog will continue probing the main loop",
                chosen, e,
            )
            return None
        thread = threading.Thread(
            target=server.serve_forever,
            name="sidecar-healthz",
            daemon=True,
        )
        thread.start()
        _server = server
        _thread = thread
        log.info("sidecar healthz listening on 0.0.0.0:%s", chosen)
        return chosen


def stop_sidecar_healthz() -> None:
    """Stop the sidecar (test/teardown helper)."""
    global _server, _thread
    with _lock:
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _server = None
        _thread = None
