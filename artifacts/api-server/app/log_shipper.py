"""Forward structured JSON log lines to a hosted log search backend.

Background
----------
``observability.configure_logging`` already renders every stdlib log
record as a single JSON object on stdout, with ``request_id`` (and other
contextvars) attached.  That makes ``grep`` against the local stream
work, but production logs need to live in a searchable backend so an
operator can paste a ``request_id`` into a saved query and get the full
trace top-to-bottom.

This module installs a second ``logging.Handler`` on the root logger
that:

* Renders each record through the same structlog ``ProcessorFormatter``
  used for stdout, so the wire format is identical to what already
  works locally.
* Pushes the rendered JSON line onto an in-process bounded
  ``queue.Queue``.
* A single daemon worker thread drains the queue, batches lines, and
  POSTs them to the configured intake URL via ``httpx`` with a short
  timeout + a couple of retries.
* On a *full* queue the handler drops the oldest line (and bumps a
  ``dropped`` counter) rather than blocking the application thread.
  Logging must never become a back-pressure source for trading.

Two wire formats are supported out of the box:

* ``betterstack`` (default) — POSTs a JSON array of log objects with
  ``Authorization: Bearer <token>``.  This is what Better Stack /
  Logtail accept and is also what most "generic JSON intake" services
  expect, so it's the lowest-friction default.
* ``loki`` — POSTs a Grafana Loki ``/loki/api/v1/push`` payload.

Configuration is entirely env-driven so the code path is identical in
dev, CI, and prod.  No env vars set => no shipper handler installed,
no background thread, no network calls.

Env vars
--------
``AXIOM_LOG_SHIPPER_URL``         intake URL (required to enable)
``AXIOM_LOG_SHIPPER_TOKEN``       bearer token (optional)
``AXIOM_LOG_SHIPPER_PROVIDER``    ``betterstack`` (default) or ``loki``
``AXIOM_LOG_SHIPPER_BATCH_SIZE``  default 100
``AXIOM_LOG_SHIPPER_FLUSH_MS``    default 2000
``AXIOM_LOG_SHIPPER_QUEUE_MAX``   default 10000
``AXIOM_LOG_SHIPPER_LABELS``      ``k=v,k=v`` merged into every record
``AXIOM_LOG_SHIPPER_LEVEL``       per-shipper minimum level (default INFO)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
import structlog


# ---------------------------------------------------------------------------
# Status — visible in tests + can be surfaced on the operator dashboard.
# ---------------------------------------------------------------------------


@dataclass
class ShipperStatus:
    enabled: bool = False
    provider: str = ""
    url: str = ""
    sent: int = 0
    batches_sent: int = 0
    dropped_queue_full: int = 0
    failed_batches: int = 0
    last_error: str | None = None
    last_send_ts: float | None = None
    last_error_ts: float | None = None


_STATUS = ShipperStatus()


def get_shipper_status() -> dict[str, Any]:
    """Return a JSON-safe snapshot of the shipper's runtime status."""
    return {
        "enabled": _STATUS.enabled,
        "provider": _STATUS.provider,
        "url": _STATUS.url,
        "sent": _STATUS.sent,
        "batches_sent": _STATUS.batches_sent,
        "dropped_queue_full": _STATUS.dropped_queue_full,
        "failed_batches": _STATUS.failed_batches,
        "last_error": _STATUS.last_error,
        "last_send_ts": _STATUS.last_send_ts,
        "last_error_ts": _STATUS.last_error_ts,
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ShipperConfig:
    url: str
    provider: str = "betterstack"
    token: str | None = None
    batch_size: int = 100
    flush_ms: int = 2000
    queue_max: int = 10000
    labels: dict[str, str] = field(default_factory=dict)
    level: int = logging.INFO

    @classmethod
    def from_env(cls) -> "ShipperConfig | None":
        url = (os.environ.get("AXIOM_LOG_SHIPPER_URL") or "").strip()
        if not url:
            return None
        provider = (
            os.environ.get("AXIOM_LOG_SHIPPER_PROVIDER") or "betterstack"
        ).strip().lower()
        if provider not in ("betterstack", "loki"):
            raise RuntimeError(
                "AXIOM_LOG_SHIPPER_PROVIDER must be 'betterstack' or 'loki' "
                f"(got {provider!r})"
            )
        token = (os.environ.get("AXIOM_LOG_SHIPPER_TOKEN") or "").strip() or None
        try:
            batch_size = max(1, int(os.environ.get("AXIOM_LOG_SHIPPER_BATCH_SIZE") or "100"))
            flush_ms = max(100, int(os.environ.get("AXIOM_LOG_SHIPPER_FLUSH_MS") or "2000"))
            queue_max = max(100, int(os.environ.get("AXIOM_LOG_SHIPPER_QUEUE_MAX") or "10000"))
        except ValueError as e:
            raise RuntimeError(f"AXIOM_LOG_SHIPPER_* must be integers: {e}") from e

        labels: dict[str, str] = {}
        raw_labels = (os.environ.get("AXIOM_LOG_SHIPPER_LABELS") or "").strip()
        if raw_labels:
            for chunk in raw_labels.split(","):
                if "=" not in chunk:
                    continue
                k, v = chunk.split("=", 1)
                k, v = k.strip(), v.strip()
                if k:
                    labels[k] = v

        level_name = (os.environ.get("AXIOM_LOG_SHIPPER_LEVEL") or "INFO").strip().upper()
        level = logging._nameToLevel.get(level_name, logging.INFO)

        return cls(
            url=url,
            provider=provider,
            token=token,
            batch_size=batch_size,
            flush_ms=flush_ms,
            queue_max=queue_max,
            labels=labels,
            level=level,
        )


# ---------------------------------------------------------------------------
# Handler + worker
# ---------------------------------------------------------------------------


_SENTINEL: object = object()


class LogShipperHandler(logging.Handler):
    """A logging.Handler that hands rendered JSON lines to a worker thread.

    Constructed with a structlog-backed ``ProcessorFormatter`` so the
    wire format mirrors stdout exactly.  The ``emit`` path never
    blocks: when the queue is full the *oldest* item is dropped so the
    most recent activity is what reaches the backend during a hot
    spell.
    """

    def __init__(
        self,
        config: ShipperConfig,
        formatter: logging.Formatter,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(level=config.level)
        self.setFormatter(formatter)
        self._config = config
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=config.queue_max)
        self._client = client or httpx.Client(timeout=5.0)
        self._owns_client = client is None
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run,
            name="log-shipper",
            daemon=True,
        )
        self._worker.start()

    # -- emit -----------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            # Drop the oldest, then enqueue.  Keep the most recent
            # activity rather than the oldest stale lines.
            try:
                self._queue.get_nowait()
                _STATUS.dropped_queue_full += 1
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(line)
            except queue.Full:
                _STATUS.dropped_queue_full += 1

    # -- worker loop ----------------------------------------------------

    def _run(self) -> None:
        flush_seconds = self._config.flush_ms / 1000.0
        batch: list[str] = []
        # `deadline` is the wall-clock at which the *current* batch must
        # be flushed.  When the batch is empty there is nothing to flush,
        # so we park indefinitely on the queue (`get()` with no timeout)
        # and start a fresh deadline only when the first record of the
        # next batch lands.  This avoids a tight-loop CPU spin on idle.
        deadline: float | None = None

        while not self._stop_event.is_set():
            if deadline is None:
                # No pending batch — block until something arrives or we
                # are asked to shut down.  A 1s ceiling keeps the
                # stop_event responsive without busy-looping.
                try:
                    item = self._queue.get(timeout=1.0)
                except queue.Empty:
                    continue
            else:
                timeout = max(0.0, deadline - time.monotonic())
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = None

            if item is _SENTINEL:
                if batch:
                    self._send_batch(batch)
                    batch = []
                return

            if item is not None:
                if not batch:
                    # First record of a new batch — start the flush window.
                    deadline = time.monotonic() + flush_seconds
                batch.append(item)

            now = time.monotonic()
            if batch and (
                len(batch) >= self._config.batch_size
                or (deadline is not None and now >= deadline)
            ):
                self._send_batch(batch)
                batch = []
                deadline = None

    def _send_batch(self, batch: list[str]) -> None:
        try:
            payload, headers = _build_payload(self._config, batch)
        except Exception as e:  # pragma: no cover — defensive
            _STATUS.failed_batches += 1
            _STATUS.last_error = f"payload_build:{type(e).__name__}:{e}"
            _STATUS.last_error_ts = time.time()
            return

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._client.post(
                    self._config.url,
                    content=payload,
                    headers=headers,
                )
                if 200 <= resp.status_code < 300:
                    _STATUS.batches_sent += 1
                    _STATUS.sent += len(batch)
                    _STATUS.last_send_ts = time.time()
                    return
                # 4xx: don't retry — almost certainly a config / auth bug.
                if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                    last_exc = RuntimeError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                    break
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
            except Exception as e:  # network / timeout
                last_exc = e
            time.sleep(0.25 * (2 ** attempt))

        _STATUS.failed_batches += 1
        if last_exc is not None:
            _STATUS.last_error = f"{type(last_exc).__name__}: {last_exc}"
            _STATUS.last_error_ts = time.time()

    # -- shutdown -------------------------------------------------------

    def close(self) -> None:
        try:
            self._stop_event.set()
            try:
                self._queue.put_nowait(_SENTINEL)
            except queue.Full:
                pass
            self._worker.join(timeout=5.0)
            if self._owns_client:
                self._client.close()
        finally:
            super().close()


# ---------------------------------------------------------------------------
# Wire formats
# ---------------------------------------------------------------------------


def _build_payload(config: ShipperConfig, batch: Iterable[str]) -> tuple[bytes, dict[str, str]]:
    if config.provider == "loki":
        return _build_loki_payload(config, batch)
    return _build_betterstack_payload(config, batch)


def _decode_line(line: str) -> dict[str, Any]:
    try:
        obj = json.loads(line)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {"event": line}


def _build_betterstack_payload(
    config: ShipperConfig, batch: Iterable[str]
) -> tuple[bytes, dict[str, str]]:
    items: list[dict[str, Any]] = []
    for line in batch:
        rec = _decode_line(line)
        if config.labels:
            for k, v in config.labels.items():
                rec.setdefault(k, v)
        # Better Stack expects ``dt`` for the timestamp + ``message`` for
        # the human-readable line; preserve our existing ``timestamp`` /
        # ``event`` fields too so other backends still work.
        if "timestamp" in rec and "dt" not in rec:
            rec["dt"] = rec["timestamp"]
        if "event" in rec and "message" not in rec:
            rec["message"] = rec["event"]
        items.append(rec)
    headers = {"Content-Type": "application/json"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    return json.dumps(items, default=str).encode("utf-8"), headers


def _build_loki_payload(
    config: ShipperConfig, batch: Iterable[str]
) -> tuple[bytes, dict[str, str]]:
    # Group by request_id so traces become a single Loki stream and the
    # saved query `{request_id="<id>"}` returns the full top-to-bottom
    # log of one request.
    streams: dict[tuple[tuple[str, str], ...], list[list[str]]] = {}
    for line in batch:
        rec = _decode_line(line)
        request_id = str(rec.get("request_id") or "none")
        labels = {**config.labels, "request_id": request_id}
        # Loki insists labels are stable strings; cast.
        labels = {str(k): str(v) for k, v in labels.items()}
        key = tuple(sorted(labels.items()))
        ts_ns = _record_ts_ns(rec)
        streams.setdefault(key, []).append([str(ts_ns), line])
    body = {
        "streams": [
            {"stream": dict(key), "values": values}
            for key, values in streams.items()
        ]
    }
    headers = {"Content-Type": "application/json"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    return json.dumps(body).encode("utf-8"), headers


def _record_ts_ns(rec: dict[str, Any]) -> int:
    """Best-effort nanosecond timestamp from a structlog record."""
    raw = rec.get("timestamp")
    if isinstance(raw, str):
        try:
            from datetime import datetime

            # `TimeStamper(fmt="iso", utc=True)` emits e.g.
            # "2026-04-23T18:00:00.123456Z"
            cleaned = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            return int(dt.timestamp() * 1_000_000_000)
        except Exception:
            pass
    return time.time_ns()


# ---------------------------------------------------------------------------
# Installation entry point — called from observability.configure_logging.
# ---------------------------------------------------------------------------


def install_from_env(formatter: logging.Formatter) -> LogShipperHandler | None:
    """Build + attach the shipper handler when env config is present.

    Returns the installed handler (so tests can flush it) or ``None``
    when the shipper is disabled.  Idempotent: a previously-installed
    handler is detected by its ``shipper_marker`` attribute and left
    alone.
    """
    root = logging.getLogger()
    for existing in root.handlers:
        if getattr(existing, "shipper_marker", False):
            return existing  # type: ignore[return-value]

    try:
        config = ShipperConfig.from_env()
    except Exception as e:
        logging.getLogger(__name__).error("log shipper config invalid: %s", e)
        return None
    if config is None:
        return None

    handler = LogShipperHandler(config, formatter)
    handler.shipper_marker = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    _STATUS.enabled = True
    _STATUS.provider = config.provider
    _STATUS.url = config.url
    logging.getLogger(__name__).info(
        "log_shipper_installed",
        extra={
            "provider": config.provider,
            "url": config.url,
            "batch_size": config.batch_size,
            "flush_ms": config.flush_ms,
        },
    )
    return handler


def build_default_formatter() -> logging.Formatter:
    """Return the JSON formatter used by both stdout and the shipper.

    Centralised here so the shipper can be installed without depending
    on whatever local fixtures the tests build for ``configure_logging``.
    """
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    return structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
