"""Tests for the `order_fills` dedupe-table retention sweep (task #154).

These verify:
  * `cleanup_once` issues the expected age-bounded DELETE and counts
    the rows the DB reports back.
  * Env vars override the retention window and the sweep cadence,
    with safety floors so a typo can't wipe replay protection.
  * `start`/`stop` spin a single background task that calls
    `cleanup_once` at least once and shuts down cleanly.
  * `AXIOM_ORDER_FILLS_RETENTION_ENABLED=0` short-circuits start.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from app import db as _db
from app import order_fills_retention as ofr


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    # Clear any leftover env from sibling tests so defaults are
    # exercised unless a test opts in.
    for k in (
        "AXIOM_ORDER_FILLS_RETENTION_DAYS",
        "AXIOM_ORDER_FILLS_CLEANUP_INTERVAL_SECONDS",
        "AXIOM_ORDER_FILLS_RETENTION_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)
    ofr._reset_state_for_tests()
    yield
    ofr._reset_state_for_tests()


def test_defaults_are_seven_days_and_hourly() -> None:
    assert ofr._retention_days() == 7
    assert ofr._interval_seconds() == 3600.0


def test_retention_days_floor_protects_against_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_ORDER_FILLS_RETENTION_DAYS", "0")
    assert ofr._retention_days() == 1
    monkeypatch.setenv("AXIOM_ORDER_FILLS_RETENTION_DAYS", "-3")
    assert ofr._retention_days() == 1


def test_retention_days_handles_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_ORDER_FILLS_RETENTION_DAYS", "not-a-number")
    assert ofr._retention_days() == ofr.DEFAULT_RETENTION_DAYS


def test_interval_seconds_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_ORDER_FILLS_CLEANUP_INTERVAL_SECONDS", "5")
    # The minimum sweep cadence is 60s so we can't accidentally hammer
    # the DB on a misconfigured deploy.
    assert ofr._interval_seconds() == 60.0


def test_parse_delete_status() -> None:
    assert ofr._parse_delete_status("DELETE 0") == 0
    assert ofr._parse_delete_status("DELETE 17") == 17
    assert ofr._parse_delete_status("") == 0
    assert ofr._parse_delete_status("UPDATE 3") == 0


def test_cleanup_once_issues_age_bounded_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_execute(query: str, *args: Any) -> str:
        captured["query"] = query
        captured["args"] = args
        return "DELETE 4"

    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setenv("AXIOM_ORDER_FILLS_RETENTION_DAYS", "3")

    deleted = asyncio.run(ofr.cleanup_once())

    assert deleted == 4
    assert "DELETE FROM order_fills" in captured["query"]
    assert "received_at <" in captured["query"]
    assert "INTERVAL '1 day'" in captured["query"]
    # Days come through as a positional parameter, not interpolated SQL.
    assert captured["args"] == (3,)
    s = ofr.state()
    assert s["last_deleted"] == 4
    assert s["total_deleted"] == 4
    assert s["last_error"] is None
    assert s["retention_days"] == 3


def test_cleanup_once_swallows_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(_db, "execute", boom)
    deleted = asyncio.run(ofr.cleanup_once())
    assert deleted == 0
    s = ofr.state()
    assert "connection reset" in (s["last_error"] or "")
    assert s["last_deleted"] == 0


def test_total_deleted_accumulates(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = iter(["DELETE 2", "DELETE 5", "DELETE 0"])

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        return next(counts)

    monkeypatch.setattr(_db, "execute", fake_execute)
    asyncio.run(ofr.cleanup_once())
    asyncio.run(ofr.cleanup_once())
    asyncio.run(ofr.cleanup_once())
    s = ofr.state()
    assert s["total_deleted"] == 7
    assert s["last_deleted"] == 0


def test_start_runs_loop_and_stop_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_execute(*_a: Any, **_kw: Any) -> str:
        calls.append(1)
        return "DELETE 0"

    monkeypatch.setattr(_db, "execute", fake_execute)
    # Set the floor cadence; we won't actually wait through it, we'll
    # cancel the task before the second tick.
    monkeypatch.setenv("AXIOM_ORDER_FILLS_CLEANUP_INTERVAL_SECONDS", "60")

    async def go() -> None:
        await ofr.start()
        # Yield control until the first cleanup runs.
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls, "expected cleanup_once to have run at least once"
        s = ofr.state()
        assert s["running"] is True
        await ofr.stop()
        assert ofr.state()["running"] is False

    asyncio.run(go())


def test_start_respects_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXIOM_ORDER_FILLS_RETENTION_ENABLED", "0")

    async def boom(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("DB must not be touched when disabled")

    monkeypatch.setattr(_db, "execute", boom)

    async def go() -> None:
        await ofr.start()
        assert ofr.state()["running"] is False
        # stop() on an unstarted module is a safe no-op.
        await ofr.stop()

    asyncio.run(go())
