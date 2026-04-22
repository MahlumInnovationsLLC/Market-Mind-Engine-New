"""Unit tests for the READY-gate primitive (`app.ready_gate`)."""
from __future__ import annotations

import asyncio

import pytest

from app import ready_gate


@pytest.fixture(autouse=True)
def _reset_gate():
    ready_gate.begin()
    yield
    ready_gate._event = None
    ready_gate._checks = {}
    ready_gate._started_at = 0.0
    ready_gate._completed_at = None


def test_status_starts_not_ready():
    snap = ready_gate.status()
    assert snap["ready"] is False
    assert snap["checks"] == {}
    assert snap["started_at"] is not None
    assert snap["completed_at"] is None


def test_mark_check_records_result():
    ready_gate.mark_check("migrations", ok=True, detail="head=0001")
    ready_gate.mark_check("models", ok=False, detail="boom")
    snap = ready_gate.status()
    assert snap["checks"]["migrations"]["ok"] is True
    assert snap["checks"]["migrations"]["detail"] == "head=0001"
    assert snap["checks"]["models"]["ok"] is False
    assert snap["ready"] is False


def test_set_ready_flips_event_and_unparks_waiter():
    async def go() -> bool:
        waiter = asyncio.create_task(ready_gate.wait())
        await asyncio.sleep(0)  # let waiter park
        assert not waiter.done()
        ready_gate.set_ready()
        await asyncio.wait_for(waiter, timeout=1.0)
        return ready_gate.is_ready()

    assert asyncio.run(go()) is True


def test_begin_resets_state():
    ready_gate.mark_check("x", ok=True)
    ready_gate.set_ready()
    assert ready_gate.is_ready() is True
    ready_gate.begin()
    assert ready_gate.is_ready() is False
    assert ready_gate.status()["checks"] == {}
