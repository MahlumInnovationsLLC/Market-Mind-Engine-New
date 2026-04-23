"""Tests for the hosted-log-store shipper."""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx
import pytest

from app import log_shipper
from app.log_shipper import (
    LogShipperHandler,
    ShipperConfig,
    _build_betterstack_payload,
    _build_loki_payload,
    build_default_formatter,
    install_from_env,
)
from app.observability import bind_request_id, configure_logging


@pytest.fixture(autouse=True)
def _reset_status() -> None:
    log_shipper._STATUS.__init__()  # type: ignore[misc]
    yield
    log_shipper._STATUS.__init__()  # type: ignore[misc]


@pytest.fixture(autouse=True)
def _detach_shippers():
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, LogShipperHandler):
            root.removeHandler(h)
            h.close()


class _RecordingTransport(httpx.BaseTransport):
    """Captures every POST so tests can assert on URL + payload."""

    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.event = threading.Event()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        self.event.set()
        return httpx.Response(self.status_code, json={"ok": True})


def _make_handler(provider: str = "betterstack", status_code: int = 202, **kw):
    transport = _RecordingTransport(status_code=status_code)
    client = httpx.Client(transport=transport)
    config = ShipperConfig(
        url="https://logs.example/v1/ingest",
        provider=provider,
        token="secret-token",
        batch_size=kw.get("batch_size", 5),
        flush_ms=kw.get("flush_ms", 100),
        queue_max=kw.get("queue_max", 1000),
        labels=kw.get("labels", {"service": "axiom-api"}),
    )
    handler = LogShipperHandler(config, build_default_formatter(), client=client)
    return handler, transport


def _await_send(transport: _RecordingTransport, timeout: float = 3.0) -> None:
    assert transport.event.wait(timeout=timeout), "shipper never POSTed"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_disabled_when_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AXIOM_LOG_SHIPPER_URL", raising=False)
    assert ShipperConfig.from_env() is None


def test_config_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_URL", "https://logs.example/in")
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_TOKEN", "tk")
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_PROVIDER", "loki")
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_BATCH_SIZE", "7")
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_FLUSH_MS", "500")
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_LABELS", "service=axiom-api,env=prod")
    cfg = ShipperConfig.from_env()
    assert cfg is not None
    assert cfg.url == "https://logs.example/in"
    assert cfg.token == "tk"
    assert cfg.provider == "loki"
    assert cfg.batch_size == 7
    assert cfg.flush_ms == 500
    assert cfg.labels == {"service": "axiom-api", "env": "prod"}


def test_config_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_URL", "https://logs.example/in")
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_PROVIDER", "splunk")
    with pytest.raises(RuntimeError):
        ShipperConfig.from_env()


# ---------------------------------------------------------------------------
# Wire formats
# ---------------------------------------------------------------------------


def test_betterstack_payload_includes_request_id_and_bearer() -> None:
    cfg = ShipperConfig(
        url="x",
        token="abc",
        labels={"service": "axiom-api"},
    )
    line = json.dumps(
        {
            "event": "trade_submitted",
            "timestamp": "2026-04-23T12:00:00.000000Z",
            "level": "info",
            "request_id": "rid-1",
        }
    )
    body, headers = _build_betterstack_payload(cfg, [line])
    assert headers["Authorization"] == "Bearer abc"
    items = json.loads(body)
    assert len(items) == 1
    rec = items[0]
    assert rec["request_id"] == "rid-1"
    assert rec["service"] == "axiom-api"
    assert rec["dt"] == "2026-04-23T12:00:00.000000Z"
    assert rec["message"] == "trade_submitted"


def test_loki_payload_groups_by_request_id() -> None:
    cfg = ShipperConfig(url="x", provider="loki", labels={"service": "axiom-api"})
    lines = [
        json.dumps({"event": "a", "request_id": "r1", "timestamp": "2026-04-23T12:00:00Z"}),
        json.dumps({"event": "b", "request_id": "r1", "timestamp": "2026-04-23T12:00:01Z"}),
        json.dumps({"event": "c", "request_id": "r2", "timestamp": "2026-04-23T12:00:02Z"}),
    ]
    body, _ = _build_loki_payload(cfg, lines)
    payload = json.loads(body)
    streams = payload["streams"]
    assert len(streams) == 2
    by_rid = {s["stream"]["request_id"]: s for s in streams}
    assert set(by_rid.keys()) == {"r1", "r2"}
    assert len(by_rid["r1"]["values"]) == 2
    assert by_rid["r1"]["stream"]["service"] == "axiom-api"


# ---------------------------------------------------------------------------
# End-to-end: handler -> worker thread -> HTTP POST
# ---------------------------------------------------------------------------


def test_handler_posts_batched_records_with_request_id() -> None:
    handler, transport = _make_handler(batch_size=2, flush_ms=50)
    logger = logging.getLogger("axiom.shipper.test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        bind_request_id("trace-aaa")
        logger.info("first")
        logger.info("second")
        _await_send(transport)
        # Worker may flush a batch of one when scheduled near boundary —
        # accept either 1 or 2 batches but verify both lines arrive.
        time.sleep(0.2)
        all_items: list[dict] = []
        for body in transport.bodies:
            all_items.extend(json.loads(body))
        events = [r["event"] for r in all_items]
        assert "first" in events and "second" in events
        for r in all_items:
            assert r["request_id"] == "trace-aaa"
            assert r["service"] == "axiom-api"
        # All requests carry the bearer token + correct URL.
        for req in transport.requests:
            assert req.url.path == "/v1/ingest"
            assert req.headers["Authorization"] == "Bearer secret-token"
        assert log_shipper._STATUS.sent >= 2
        assert log_shipper._STATUS.batches_sent >= 1
    finally:
        logger.removeHandler(handler)


def test_handler_drops_oldest_when_queue_full() -> None:
    transport = _RecordingTransport(status_code=202)
    client = httpx.Client(transport=transport)
    config = ShipperConfig(
        url="https://logs.example/in",
        provider="betterstack",
        batch_size=1000,
        flush_ms=60_000,
        queue_max=2,
    )
    handler = LogShipperHandler(config, build_default_formatter(), client=client)
    # Park the worker thread so the queue actually fills — otherwise
    # the worker drains items as fast as we enqueue them and we never
    # observe the drop path.
    handler._stop_event.set()
    handler._worker.join(timeout=1.0)
    logger = logging.getLogger("axiom.shipper.drop")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for i in range(50):
            logger.info("evt %d", i)
        # queue_max=2, so 48 records must have been dropped.
        assert log_shipper._STATUS.dropped_queue_full >= 40
        assert handler._queue.qsize() == 2
    finally:
        logger.removeHandler(handler)
        client.close()


def test_idle_worker_does_not_busy_spin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long-idle worker must park on the queue, not tight-loop on a 0s timeout.

    Regression test for the worker-loop bug where `deadline` was set
    once and `timeout = max(0, deadline - now)` collapsed to zero
    forever once the first window elapsed with no records.
    """
    handler, transport = _make_handler(batch_size=10, flush_ms=20)
    # Snapshot how many `queue.get` calls the worker makes while idle.
    real_get = handler._queue.get
    counter = {"n": 0}

    def _counting_get(*a, **kw):  # type: ignore[no-untyped-def]
        counter["n"] += 1
        return real_get(*a, **kw)

    monkeypatch.setattr(handler._queue, "get", _counting_get)
    try:
        # Sleep well past the flush window with no log records pushed.
        time.sleep(0.6)
        # Idle path uses a 1s blocking get; a busy-spinning worker
        # would have called `get` thousands of times by now.
        assert counter["n"] < 50, (
            f"worker appears to busy-spin while idle ({counter['n']} get() calls in 0.6s)"
        )
        # And no spurious POSTs while idle.
        assert not transport.requests
    finally:
        pass


def test_handler_records_failed_batch_on_4xx() -> None:
    handler, transport = _make_handler(status_code=401, batch_size=1, flush_ms=20)
    logger = logging.getLogger("axiom.shipper.fail")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("nope")
        _await_send(transport)
        time.sleep(0.2)
        assert log_shipper._STATUS.failed_batches >= 1
        assert log_shipper._STATUS.last_error is not None
        assert log_shipper._STATUS.sent == 0
    finally:
        logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Installation entry point
# ---------------------------------------------------------------------------


def test_install_from_env_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_URL", "https://logs.example/in")
    monkeypatch.setenv("AXIOM_LOG_SHIPPER_TOKEN", "tk")
    h1 = install_from_env(build_default_formatter())
    h2 = install_from_env(build_default_formatter())
    assert h1 is not None and h2 is h1
    root = logging.getLogger()
    shippers = [h for h in root.handlers if isinstance(h, LogShipperHandler)]
    assert len(shippers) == 1


def test_install_from_env_noop_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AXIOM_LOG_SHIPPER_URL", raising=False)
    assert install_from_env(build_default_formatter()) is None
    status = log_shipper.get_shipper_status()
    assert status["enabled"] is False


def test_configure_logging_respects_disabled_shipper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AXIOM_LOG_SHIPPER_URL", raising=False)
    # configure_logging is idempotent; the second call still must not
    # add a shipper handler when env is unset.
    configure_logging()
    root = logging.getLogger()
    assert not any(isinstance(h, LogShipperHandler) for h in root.handlers)
