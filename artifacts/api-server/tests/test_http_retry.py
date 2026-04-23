"""Unit tests for app.http_retry — the shared retry primitive."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import http_retry


@pytest.fixture(autouse=True)
def _reset_counters():
    http_retry.reset_counters()
    yield
    http_retry.reset_counters()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _resp(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, content=b"{}")


async def _no_sleep(_):
    return None


def test_happy_path_returns_response_and_records_success():
    calls = 0

    async def do():
        nonlocal calls
        calls += 1
        return _resp(200)

    r = _run(http_retry.retry_request(do, client_name="happy", sleep=_no_sleep))
    assert r.status_code == 200
    assert calls == 1
    snap = http_retry.snapshot_counters()
    assert snap["happy"]["total_attempts"] == 1
    assert snap["happy"]["total_retries"] == 0
    assert snap["happy"]["last_success_at"] is not None


def test_transient_503_then_200_retries_once():
    seq = [503, 200]
    calls = 0

    async def do():
        nonlocal calls
        calls += 1
        return _resp(seq[calls - 1])

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    r = _run(
        http_retry.retry_request(
            do, client_name="t503", max_attempts=3, sleep=fake_sleep,
        )
    )
    assert r.status_code == 200
    assert calls == 2
    assert len(sleeps) == 1
    snap = http_retry.snapshot_counters()
    assert snap["t503"]["total_attempts"] == 2
    assert snap["t503"]["total_retries"] == 1


def test_exhausted_retries_returns_last_retryable_response():
    calls = 0

    async def do():
        nonlocal calls
        calls += 1
        return _resp(503)

    r = _run(
        http_retry.retry_request(
            do, client_name="exh", max_attempts=3, sleep=_no_sleep,
        )
    )
    assert r.status_code == 503
    assert calls == 3
    snap = http_retry.snapshot_counters()
    assert snap["exh"]["total_attempts"] == 3
    assert snap["exh"]["total_retries"] == 2
    assert snap["exh"]["total_failures"] >= 3


def test_retry_after_header_is_honoured():
    seq = [(503, {"Retry-After": "2"}), (200, {})]
    calls = 0

    async def do():
        nonlocal calls
        calls += 1
        s, h = seq[calls - 1]
        return _resp(s, h)

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    r = _run(
        http_retry.retry_request(
            do, client_name="ra", max_attempts=3, sleep=fake_sleep,
        )
    )
    assert r.status_code == 200
    assert sleeps == [2.0]


def test_non_retryable_4xx_returns_immediately():
    calls = 0

    async def do():
        nonlocal calls
        calls += 1
        return _resp(404)

    r = _run(http_retry.retry_request(do, client_name="nf", max_attempts=3))
    assert r.status_code == 404
    assert calls == 1
    snap = http_retry.snapshot_counters()
    assert snap["nf"]["total_attempts"] == 1
    assert snap["nf"]["total_retries"] == 0
    assert snap["nf"]["total_failures"] == 1


def test_transport_error_retries_and_eventually_raises():
    calls = 0

    async def do():
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom")

    with pytest.raises(httpx.ConnectError):
        _run(
            http_retry.retry_request(
                do, client_name="boom", max_attempts=3, sleep=_no_sleep,
            )
        )
    snap = http_retry.snapshot_counters()
    assert snap["boom"]["total_attempts"] == 3
    assert snap["boom"]["total_retries"] == 2
    assert snap["boom"]["last_failure_error"].startswith("ConnectError")


def test_transport_error_then_recover():
    calls = 0

    async def do():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadError("temporary blip")
        return _resp(200)

    r = _run(
        http_retry.retry_request(
            do, client_name="recover", max_attempts=3, sleep=_no_sleep,
        )
    )
    assert r.status_code == 200
    assert calls == 2
