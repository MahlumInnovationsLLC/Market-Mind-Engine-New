"""L2 supervision watchdog for the Axiom-Predict API server.

Spec calls for a 3-layer supervision model:
    L1 — Replit workflow (process supervisor of last resort)
    L2 — this Python watchdog (port 8099)
    L3 — internal probes inside the API server

This watchdog:
  * Spawns the API server as a child subprocess (when invoked with
    `--supervise -- <cmd> [args...]`) OR runs in observe-only mode against
    an already-running API server (when `--supervise` is omitted).
  * Polls the target health URL every `--interval` seconds.
  * After `--threshold` consecutive failed probes, restarts the supervised
    child process (no-op in observe mode; logs an alert instead).
  * Rate-limits restart storms: at most `--max-restarts` restarts inside
    `--restart-window` seconds. Once the budget is exhausted the watchdog
    enters cooldown until the window slides forward.
  * Exposes a tiny HTTP status endpoint on `--port` (default 8099):
        GET  /                   -> JSON status snapshot
        GET  /healthz            -> 200 "ok" liveness for the watchdog itself
        POST /restart            -> request an immediate child restart (rate-limited)

Stdlib-only — no extra runtime dependencies.

Examples:
    # Supervise mode (recommended for L1 workflow):
    python scripts/watchdog.py --supervise -- \
        python -m uvicorn app.main:app --host 0.0.0.0 --port 8080

    # Observe-only mode (alongside an existing server):
    python scripts/watchdog.py --target http://127.0.0.1:8080/api/healthz
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


log = logging.getLogger("watchdog")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class WatchdogState:
    def __init__(
        self,
        *,
        target: str,
        threshold: int,
        interval: float,
        max_restarts: int,
        restart_window: float,
        backoff_initial: float,
        backoff_cap: float,
        supervise: bool,
        startup_grace_sec: float = 0.0,
    ) -> None:
        self.target = target
        self.threshold = threshold
        self.interval = interval
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.backoff_initial = backoff_initial
        self.backoff_cap = backoff_cap
        self.supervise = supervise
        # Suppress restart-trigger evaluation during the first
        # ``startup_grace_sec`` seconds after the child boots. Lets a
        # slow alembic / model-warmup phase complete without flapping.
        self.startup_grace_sec = max(0.0, startup_grace_sec)
        self.last_child_start_at: float | None = None

        self.lock = threading.Lock()
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.last_probe_at: float | None = None
        self.last_probe_ok: bool | None = None
        self.last_probe_error: str | None = None
        self.last_probe_status: int | None = None
        self.last_probe_latency_ms: float | None = None
        self.total_probes = 0
        self.total_failures = 0
        self.total_restarts = 0
        # Sliding window of recent restart timestamps (for rate-limiting).
        self.restart_timestamps: deque[float] = deque()
        self.last_restart_at: float | None = None
        self.next_restart_eligible_at: float = 0.0
        self.last_restart_reason: str | None = None
        self.cooldown_until: float | None = None
        self.started_at = time.time()
        self.child_pid: int | None = None
        self.child_exit_code: int | None = None

    # ---- restart accounting --------------------------------------------------

    def _trim_window(self, now: float) -> None:
        cutoff = now - self.restart_window
        while self.restart_timestamps and self.restart_timestamps[0] < cutoff:
            self.restart_timestamps.popleft()

    def record_restart(self, reason: str) -> None:
        with self.lock:
            now = time.time()
            self._trim_window(now)
            self.restart_timestamps.append(now)
            self.total_restarts += 1
            self.last_restart_at = now
            self.last_restart_reason = reason
            # Exponential backoff between restarts inside the window.
            attempts_in_window = len(self.restart_timestamps)
            backoff = min(
                self.backoff_cap,
                self.backoff_initial * (2 ** max(0, attempts_in_window - 1)),
            )
            self.next_restart_eligible_at = now + backoff
            self.consecutive_failures = 0

    def can_restart(self) -> tuple[bool, str | None]:
        with self.lock:
            now = time.time()
            self._trim_window(now)
            if len(self.restart_timestamps) >= self.max_restarts:
                self.cooldown_until = (
                    self.restart_timestamps[0] + self.restart_window
                )
                return False, (
                    f"restart budget exhausted: {len(self.restart_timestamps)} "
                    f"restarts in last {self.restart_window:.0f}s "
                    f"(max {self.max_restarts}); cooldown until "
                    f"{self.cooldown_until:.0f}"
                )
            if now < self.next_restart_eligible_at:
                return False, (
                    f"backoff: next restart eligible in "
                    f"{self.next_restart_eligible_at - now:.1f}s"
                )
            self.cooldown_until = None
            return True, None

    # ---- probe accounting ----------------------------------------------------

    def record_probe(
        self,
        ok: bool,
        *,
        status: int | None,
        error: str | None,
        latency_ms: float,
    ) -> None:
        with self.lock:
            now = time.time()
            self.last_probe_at = now
            self.last_probe_ok = ok
            self.last_probe_status = status
            self.last_probe_error = error
            self.last_probe_latency_ms = latency_ms
            self.total_probes += 1
            if ok:
                self.consecutive_failures = 0
                self.consecutive_successes += 1
            else:
                self.consecutive_failures += 1
                self.consecutive_successes = 0
                self.total_failures += 1

    # ---- snapshot ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            self._trim_window(now)
            return {
                "watchdog": "ok",
                "supervise": self.supervise,
                "target": self.target,
                "uptime_seconds": round(now - self.started_at, 1),
                "interval": self.interval,
                "threshold": self.threshold,
                "max_restarts_per_window": self.max_restarts,
                "restart_window_seconds": self.restart_window,
                "consecutive_failures": self.consecutive_failures,
                "consecutive_successes": self.consecutive_successes,
                "total_probes": self.total_probes,
                "total_failures": self.total_failures,
                "total_restarts": self.total_restarts,
                "restarts_in_window": len(self.restart_timestamps),
                "last_probe_at": self.last_probe_at,
                "last_probe_ok": self.last_probe_ok,
                "last_probe_status": self.last_probe_status,
                "last_probe_error": self.last_probe_error,
                "last_probe_latency_ms": self.last_probe_latency_ms,
                "last_restart_at": self.last_restart_at,
                "last_restart_reason": self.last_restart_reason,
                "next_restart_eligible_in": max(
                    0.0, self.next_restart_eligible_at - now
                ),
                "cooldown_until": self.cooldown_until,
                "child_pid": self.child_pid,
                "child_exit_code": self.child_exit_code,
            }


# ---------------------------------------------------------------------------
# Child supervisor
# ---------------------------------------------------------------------------

class ChildSupervisor:
    """Spawns and respawns the API server subprocess."""

    def __init__(self, argv: list[str], state: WatchdogState) -> None:
        if not argv:
            raise ValueError("supervised command must be non-empty")
        self.argv = argv
        self.state = state
        self.proc: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()
        self.shutting_down = False

    def start(self) -> None:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            log.info("[watchdog] starting child: %s", " ".join(self.argv))
            self.proc = subprocess.Popen(
                self.argv,
                stdout=None,
                stderr=None,
                start_new_session=True,
            )
            self.state.child_pid = self.proc.pid
            self.state.child_exit_code = None
            self.state.last_child_start_at = time.time()
            log.info("[watchdog] child started pid=%d", self.proc.pid)

    def stop(self, *, timeout: float = 10.0) -> None:
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            log.info("[watchdog] stopping child pid=%d (SIGTERM)", proc.pid)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning(
                "[watchdog] child pid=%d did not exit in %.1fs; SIGKILL",
                proc.pid, timeout,
            )
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                log.error("[watchdog] child pid=%d still running after SIGKILL", proc.pid)
        with self.lock:
            if self.proc is proc:
                self.state.child_exit_code = proc.returncode

    def restart(self, reason: str) -> bool:
        ok, why = self.state.can_restart()
        if not ok:
            log.warning("[watchdog] restart suppressed: %s", why)
            return False
        log.warning("[watchdog] RESTART triggered: %s", reason)
        self.stop()
        self.state.record_restart(reason)
        self.start()
        return True

    def reap(self) -> None:
        """If the child exited on its own, record it and respawn (subject to
        rate limiting). Called from the probe loop on each tick."""
        with self.lock:
            proc = self.proc
        if proc is None:
            return
        rc = proc.poll()
        if rc is None:
            return
        # Child has exited unexpectedly.
        with self.lock:
            self.state.child_exit_code = rc
        if self.shutting_down:
            return
        self.restart(f"child exited unexpectedly (rc={rc})")


# ---------------------------------------------------------------------------
# Probe loop
# ---------------------------------------------------------------------------

def probe_once(target: str, *, timeout: float) -> tuple[bool, int | None, str | None, float]:
    started = time.monotonic()
    try:
        req = urllib.request.Request(target, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            r.read(1024)  # drain a bit
            latency_ms = (time.monotonic() - started) * 1000.0
            ok = 200 <= status < 400
            return ok, status, None if ok else f"HTTP {status}", latency_ms
    except urllib.error.HTTPError as e:
        latency_ms = (time.monotonic() - started) * 1000.0
        return False, e.code, f"HTTP {e.code}", latency_ms
    except urllib.error.URLError as e:
        latency_ms = (time.monotonic() - started) * 1000.0
        return False, None, f"URLError: {e.reason}", latency_ms
    except Exception as e:
        latency_ms = (time.monotonic() - started) * 1000.0
        return False, None, f"{type(e).__name__}: {e}", latency_ms


def probe_loop(
    state: WatchdogState,
    supervisor: ChildSupervisor | None,
    stop_event: threading.Event,
    *,
    probe_timeout: float,
) -> None:
    log.info(
        "[watchdog] probe loop start target=%s interval=%.1fs threshold=%d",
        state.target, state.interval, state.threshold,
    )
    while not stop_event.is_set():
        if supervisor is not None:
            supervisor.reap()
        ok, status, err, latency = probe_once(state.target, timeout=probe_timeout)
        state.record_probe(ok, status=status, error=err, latency_ms=latency)
        if ok:
            log.debug(
                "[watchdog] probe ok status=%s latency=%.1fms", status, latency,
            )
        else:
            log.warning(
                "[watchdog] probe FAIL status=%s err=%s latency=%.1fms "
                "consecutive=%d/%d",
                status, err, latency,
                state.consecutive_failures, state.threshold,
            )
        # Startup grace: while the child is still inside its boot
        # window, suppress restart-trigger evaluation. Long startup
        # work (e.g. `alembic upgrade head` against an IO-starved DB)
        # would otherwise flap the supervisor every 15s and never let
        # the child finish booting.
        in_grace = (
            state.startup_grace_sec > 0
            and state.last_child_start_at is not None
            and (time.time() - state.last_child_start_at) < state.startup_grace_sec
        )
        if in_grace and state.consecutive_failures >= state.threshold:
            log.info(
                "[watchdog] probe failures suppressed: child in startup "
                "grace (%.0fs of %.0fs)",
                time.time() - (state.last_child_start_at or time.time()),
                state.startup_grace_sec,
            )
        if state.consecutive_failures >= state.threshold and not in_grace:
            reason = (
                f"{state.consecutive_failures} consecutive probe failures "
                f"(last_error={err})"
            )
            if supervisor is not None:
                supervisor.restart(reason)
            else:
                # Observe-only: just log loudly so an external supervisor
                # (workflow) sees the alert. We then reset the counter so
                # we don't spam alerts every tick until things recover.
                ok2, why = state.can_restart()
                if ok2:
                    log.error("[watchdog] ALERT (observe-only): %s", reason)
                    state.record_restart(f"observe-only alert: {reason}")
                else:
                    log.warning("[watchdog] alert suppressed: %s", why)
        stop_event.wait(state.interval)
    log.info("[watchdog] probe loop stopped")


# ---------------------------------------------------------------------------
# HTTP status endpoint (port 8099)
# ---------------------------------------------------------------------------

class _StatusHandler(BaseHTTPRequestHandler):
    state: WatchdogState  # injected by factory
    supervisor: ChildSupervisor | None

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        log.debug("status-http: " + fmt, *args)

    def _write_json(self, code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/status"):
            self._write_json(200, self.state.snapshot())
            return
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self._write_json(404, {"error": "not found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/restart":
            if self.supervisor is None:
                self._write_json(
                    400, {"error": "watchdog is in observe-only mode"}
                )
                return
            ok = self.supervisor.restart("manual restart via /restart")
            self._write_json(200 if ok else 429, {"restarted": ok})
            return
        self._write_json(404, {"error": "not found", "path": self.path})


def make_handler(
    state: WatchdogState, supervisor: ChildSupervisor | None
) -> type[_StatusHandler]:
    cls = type(
        "BoundStatusHandler",
        (_StatusHandler,),
        {"state": state, "supervisor": supervisor},
    )
    return cls


class _ReusableHTTPServer(HTTPServer):
    """HTTPServer subclass that sets ``SO_REUSEADDR`` so a freshly-restarted
    watchdog can immediately re-bind the status port even if the previous
    process's socket is still in ``TIME_WAIT``.  Without this, a rapid
    restart cycle (which is exactly what the workflow runner does) racily
    crashes with ``OSError: [Errno 98] Address already in use``."""

    allow_reuse_address = True


def serve_status(
    state: WatchdogState,
    supervisor: ChildSupervisor | None,
    *,
    host: str,
    port: int,
    stop_event: threading.Event,
) -> HTTPServer:
    handler = make_handler(state, supervisor)
    # Even with SO_REUSEADDR, the kernel can briefly hold the port if the
    # previous holder hasn't fully released it yet.  Retry the bind for a
    # few seconds before giving up — almost every collision we see in the
    # workflow runner clears within ~1s.
    bind_deadline = time.monotonic() + 5.0
    last_err: OSError | None = None
    httpd: HTTPServer | None = None
    while True:
        try:
            httpd = _ReusableHTTPServer((host, port), handler)
            break
        except OSError as e:
            last_err = e
            if time.monotonic() >= bind_deadline:
                raise
            log.warning(
                "[watchdog] status port %s:%d not yet free (%s) — retrying...",
                host, port, e,
            )
            time.sleep(0.25)
    if httpd is None:  # pragma: no cover — loop above either breaks or raises
        raise RuntimeError(f"failed to bind watchdog status port: {last_err}")
    thread = threading.Thread(
        target=httpd.serve_forever, name="watchdog-http", daemon=True
    )
    thread.start()
    log.info("[watchdog] status endpoint listening on http://%s:%d/", host, port)

    def _shutdown() -> None:
        stop_event.wait()
        log.info("[watchdog] shutting down status endpoint")
        httpd.shutdown()

    threading.Thread(target=_shutdown, name="watchdog-http-stopper", daemon=True).start()
    return httpd


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    # Split off the supervised command at the literal `--`. argparse handles
    # `--` natively but capturing the tail explicitly is clearer here.
    tail: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        tail = argv[i + 1 :]
        argv = argv[:i]

    p = argparse.ArgumentParser(
        prog="watchdog",
        description="L2 supervision watchdog for the Axiom-Predict API server.",
    )
    p.add_argument("--port", type=int, default=int(os.environ.get("WATCHDOG_PORT", "8099")))
    p.add_argument("--host", default=os.environ.get("WATCHDOG_HOST", "127.0.0.1"))
    p.add_argument(
        "--target",
        default=os.environ.get("WATCHDOG_TARGET")
        or f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/api/healthz",
        help="health URL to poll",
    )
    p.add_argument(
        "--interval", type=float,
        default=float(os.environ.get("WATCHDOG_INTERVAL_SEC", "5")),
    )
    p.add_argument(
        "--threshold", type=int,
        default=int(os.environ.get("WATCHDOG_FAILURE_THRESHOLD", "3")),
        help="consecutive failed probes before triggering a restart",
    )
    p.add_argument(
        "--probe-timeout", type=float,
        default=float(os.environ.get("WATCHDOG_PROBE_TIMEOUT", "30")),
    )
    p.add_argument(
        "--max-restarts", type=int,
        default=int(os.environ.get("WATCHDOG_MAX_RESTARTS", "5")),
        help="max restarts inside the sliding window before cooldown",
    )
    p.add_argument(
        "--restart-window", type=float,
        default=float(os.environ.get("WATCHDOG_RESTART_WINDOW_SEC", "600")),
    )
    p.add_argument(
        "--backoff-initial", type=float,
        default=float(os.environ.get("WATCHDOG_BACKOFF_INITIAL_SEC", "5")),
    )
    p.add_argument(
        "--backoff-cap", type=float,
        default=float(os.environ.get("WATCHDOG_BACKOFF_CAP_SEC", "120")),
    )
    p.add_argument(
        "--startup-grace-sec", type=float,
        default=float(os.environ.get("WATCHDOG_STARTUP_GRACE_SEC", "120")),
        help="seconds after each child start during which probe failures don't trigger restarts",
    )
    p.add_argument(
        "--supervise", action="store_true",
        help="spawn the supervised command (after `--`) and respawn on failure",
    )
    p.add_argument(
        "--log-level", default=os.environ.get("WATCHDOG_LOG_LEVEL", "INFO"),
    )
    args = p.parse_args(argv)
    return args, tail


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args, child_argv = _parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.supervise and not child_argv:
        log.error("--supervise requires a command after `--`")
        return 2

    state = WatchdogState(
        target=args.target,
        threshold=args.threshold,
        interval=args.interval,
        max_restarts=args.max_restarts,
        restart_window=args.restart_window,
        backoff_initial=args.backoff_initial,
        backoff_cap=args.backoff_cap,
        supervise=args.supervise,
        startup_grace_sec=args.startup_grace_sec,
    )

    stop_event = threading.Event()
    supervisor: ChildSupervisor | None = None
    if args.supervise:
        supervisor = ChildSupervisor(child_argv, state)
        supervisor.start()

    def _handle_signal(signum: int, _frame: Any) -> None:
        log.info("[watchdog] received signal %d; shutting down", signum)
        if supervisor is not None:
            supervisor.shutting_down = True
            supervisor.stop()
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    serve_status(
        state, supervisor,
        host=args.host, port=args.port, stop_event=stop_event,
    )

    try:
        probe_loop(state, supervisor, stop_event, probe_timeout=args.probe_timeout)
    finally:
        if supervisor is not None:
            supervisor.shutting_down = True
            supervisor.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
