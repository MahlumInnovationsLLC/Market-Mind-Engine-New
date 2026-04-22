"""Tests for structured JSON logging + request_id correlation."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import REQUEST_ID_HEADER, RequestIdMiddleware
from app.observability import (
    bind_request_id,
    configure_logging,
    get_request_id,
    new_request_id,
)


@pytest.fixture
def json_log_buffer() -> io.StringIO:
    """Replace the root handler with one that writes to an in-memory buffer."""
    configure_logging()

    buf = io.StringIO()
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []

    handler = logging.StreamHandler(buf)
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            lambda _l, _m, ev: {**ev, "request_id": ev.get("request_id", get_request_id())},
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ],
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield buf
    finally:
        root.handlers = saved


def _parse_lines(buf: io.StringIO) -> list[dict]:
    out: list[dict] = []
    for line in buf.getvalue().splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


def test_new_request_id_is_hex32() -> None:
    rid = new_request_id()
    assert re.fullmatch(r"[0-9a-f]{32}", rid), rid


def test_stdlib_logger_emits_json_with_request_id(json_log_buffer: io.StringIO) -> None:
    bind_request_id("abc123")
    logging.getLogger("axiom.test").info("hello world")

    lines = _parse_lines(json_log_buffer)
    assert lines, "expected at least one JSON log line"
    rec = lines[-1]
    assert rec["event"] == "hello world"
    assert rec["request_id"] == "abc123"
    assert rec["level"] == "info"
    assert "timestamp" in rec


def test_middleware_propagates_inbound_request_id() -> None:
    configure_logging()
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/echo")
    async def _echo() -> dict[str, str | None]:
        return {"request_id": get_request_id()}

    client = TestClient(app)
    res = client.get("/echo", headers={REQUEST_ID_HEADER: "client-supplied-id"})
    assert res.status_code == 200
    assert res.json() == {"request_id": "client-supplied-id"}
    assert res.headers[REQUEST_ID_HEADER] == "client-supplied-id"


def test_middleware_mints_request_id_when_missing() -> None:
    configure_logging()
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/whoami")
    async def _whoami() -> dict[str, str | None]:
        return {"request_id": get_request_id()}

    client = TestClient(app)
    res = client.get("/whoami")
    assert res.status_code == 200
    rid = res.json()["request_id"]
    assert rid and re.fullmatch(r"[0-9a-f]{32}", rid)
    assert res.headers[REQUEST_ID_HEADER] == rid


def test_request_id_inherits_into_background_task(json_log_buffer: io.StringIO) -> None:
    """asyncio.create_task copies the current Context, so workers inherit the id."""

    async def _scenario() -> str | None:
        bind_request_id("trace-xyz")
        captured: dict[str, str | None] = {}

        async def _worker() -> None:
            await asyncio.sleep(0)
            logging.getLogger("axiom.worker").info("work_done")
            captured["seen"] = get_request_id()

        await asyncio.create_task(_worker())
        return captured.get("seen")

    seen = asyncio.run(_scenario())
    assert seen == "trace-xyz"

    lines = _parse_lines(json_log_buffer)
    worker_lines = [r for r in lines if r.get("event") == "work_done"]
    assert worker_lines, "worker log line not captured"
    assert worker_lines[-1]["request_id"] == "trace-xyz"
