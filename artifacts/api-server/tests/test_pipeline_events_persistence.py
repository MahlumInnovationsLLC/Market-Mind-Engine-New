"""Tests for the pipeline event DB-backed snapshot fallback (task #121).

When the in-process ring buffer is cold (e.g. immediately after an API
restart), the snapshot endpoint must hydrate from ``pipeline_events``
so operators see the last ~5 cycles instead of an empty page.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.orchestrator import streams as streams_mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def hub():
    return streams_mod._PipelineEventHub()


def test_publish_persists_events_with_cycle_id(monkeypatch, hub):
    inserts: list[tuple] = []

    async def fake_execute(query, *args):
        inserts.append((query, args))
        return "INSERT 0 1"

    monkeypatch.setattr(streams_mod.db, "execute", fake_execute)

    _run(hub.publish({"type": "cycle_start", "cycle_id": 42, "vertical": "sports"}))
    _run(hub.publish({"type": "cycle_complete", "cycle_id": 42}))

    assert len(inserts) == 2
    _, args = inserts[0]
    assert args[0] == 42  # cycle_id
    assert args[1] == "cycle_start"
    assert args[2] == 1   # _seq
    payload = json.loads(args[3])
    assert payload["vertical"] == "sports"
    assert payload["_seq"] == 1


def test_publish_skips_persist_for_cycleless_events(monkeypatch, hub):
    inserts: list[tuple] = []

    async def fake_execute(query, *args):
        inserts.append(args)
        return "INSERT 0 1"

    monkeypatch.setattr(streams_mod.db, "execute", fake_execute)

    _run(hub.publish({"type": "heartbeat"}))
    assert inserts == []


def test_persist_swallows_db_errors(monkeypatch, hub):
    async def boom(query, *args):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(streams_mod.db, "execute", boom)

    # Must not raise — cycle code wraps publish() but the contract is
    # explicit: persistence is best-effort.
    _run(hub.publish({"type": "cycle_start", "cycle_id": 7}))
    # In-memory ring still got it.
    assert hub.recent_events(max_cycles=1) == [
        {"type": "cycle_start", "cycle_id": 7, "_seq": 1}
    ]


def test_recent_events_fallback_uses_db_when_buffer_cold(monkeypatch, hub):
    # Simulate a fresh API restart: nothing in the ring, but the DB
    # holds events from the last two cycles.
    stored_rows = [
        {
            "id": 10, "seq": 5,
            "payload": json.dumps(
                {"type": "cycle_start", "cycle_id": 100, "_seq": 5}
            ),
        },
        {
            "id": 11, "seq": 6,
            "payload": json.dumps(
                {"type": "cycle_complete", "cycle_id": 100, "_seq": 6}
            ),
        },
        {
            "id": 12, "seq": 7,
            "payload": json.dumps(
                {"type": "cycle_start", "cycle_id": 101, "_seq": 7}
            ),
        },
    ]

    async def fake_fetch(query, *args):
        assert "pipeline_events" in query
        assert args == (5,)
        return stored_rows

    monkeypatch.setattr(streams_mod.db, "fetch", fake_fetch)

    events = _run(hub.recent_events_with_fallback(max_cycles=5))
    assert [e["_seq"] for e in events] == [5, 6, 7]
    assert [e["cycle_id"] for e in events] == [100, 100, 101]


def test_recent_events_fallback_prefers_in_memory(monkeypatch, hub):
    async def fake_execute(query, *args):
        return "INSERT 0 1"

    async def fake_fetch(query, *args):
        raise AssertionError("should not hit DB when buffer warm")

    monkeypatch.setattr(streams_mod.db, "execute", fake_execute)
    monkeypatch.setattr(streams_mod.db, "fetch", fake_fetch)

    _run(hub.publish({"type": "cycle_start", "cycle_id": 9}))
    events = _run(hub.recent_events_with_fallback(max_cycles=5))
    assert len(events) == 1
    assert events[0]["cycle_id"] == 9


def test_recent_events_fallback_swallows_db_errors(monkeypatch, hub):
    async def fake_fetch(query, *args):
        raise RuntimeError("table missing")

    monkeypatch.setattr(streams_mod.db, "fetch", fake_fetch)
    assert _run(hub.recent_events_with_fallback(max_cycles=5)) == []


def test_recent_events_fallback_handles_dict_payload(monkeypatch, hub):
    # asyncpg returns jsonb columns as already-decoded dicts when a
    # codec is registered; make sure we don't try to json.loads them.
    stored_rows = [
        {
            "id": 1, "seq": 1,
            "payload": {"type": "cycle_start", "cycle_id": 1, "_seq": 1},
        },
    ]

    async def fake_fetch(query, *args):
        return stored_rows

    monkeypatch.setattr(streams_mod.db, "fetch", fake_fetch)
    events = _run(hub.recent_events_with_fallback(max_cycles=1))
    assert events == [{"type": "cycle_start", "cycle_id": 1, "_seq": 1}]
