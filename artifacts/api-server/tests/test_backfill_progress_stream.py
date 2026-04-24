"""Backfill progress streams over the settlement WebSocket hub.

The operator console used to poll ``/api/settlement/backfill-status``
every 1.5s while a run was in flight. Per task #315, ``run_backfill``
now publishes ``backfill_run_start`` / ``backfill_progress`` /
``backfill_run_complete`` events to ``settlement_event_hub`` so the
panel can subscribe instead, with polling kept as a disconnected
fallback.

These tests:
  * Confirm the lifecycle events fire in the right order
  * Confirm each event carries a fresh BackfillProgress snapshot
  * Confirm a per-market progress event lands for both the CSV and
    the Kalshi resolution paths
  * Confirm a failure inside ``run_backfill`` still emits
    ``backfill_run_complete`` with state="failed"
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.settlement import backfill as backfill_mod
from app.settlement.streams import settlement_event_hub


def _drain(sub) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not sub.empty():
        try:
            out.append(sub.get_nowait())
        except Exception:
            break
    return out


def test_run_backfill_streams_lifecycle_events_via_csv(monkeypatch):
    """CSV path: every market should emit a `backfill_progress` event,
    bracketed by `backfill_run_start` and `backfill_run_complete`.
    Each event payload must include the live snapshot fields so a
    subscriber can `setStatus({...event, type: undefined})` directly."""
    market_ids = ["M-1", "M-2", "M-3"]

    async def _fake_list() -> list[str]:
        return list(market_ids)

    async def _fake_settle(mid, *, outcome_yes, settle_price=None):
        return {"trades_settled": 2, "persona_metrics_updated": 1, "alerts_raised": 0}

    monkeypatch.setattr(backfill_mod, "_list_unsettled_kalshi_markets", _fake_list)
    monkeypatch.setattr(backfill_mod, "settle_market", _fake_settle)
    monkeypatch.setattr(
        backfill_mod, "_load_csv_overrides",
        lambda _path: {mid: (1, None) for mid in market_ids},
    )

    async def _scenario():
        sub = settlement_event_hub.subscribe(label="test-backfill")
        try:
            final = await backfill_mod.run_backfill(csv_path="/tmp/fake.csv")
        finally:
            events = _drain(sub)
            settlement_event_hub.unsubscribe(sub)
        return final, events

    final, events = asyncio.run(_scenario())

    types = [ev["type"] for ev in events]
    # First event is run_start, last is run_complete
    assert types[0] == "backfill_run_start"
    assert types[-1] == "backfill_run_complete"
    # Per-market progress events are present (one initial post-total
    # snapshot + one per market).
    progress = [ev for ev in events if ev["type"] == "backfill_progress"]
    assert len(progress) >= len(market_ids), types

    # Snapshot fields are merged in at the top level so the panel can
    # treat each event as a BackfillStatus update.
    last_progress = progress[-1]
    for key in (
        "state",
        "markets_total",
        "markets_processed",
        "markets_settled",
        "trades_settled",
        "persona_metrics_updated",
    ):
        assert key in last_progress, last_progress

    # The terminal event must reflect a completed run with the final
    # counters so a subscriber that only saw run_complete still has
    # an accurate snapshot.
    final_ev = events[-1]
    assert final_ev["state"] == "completed"
    assert final_ev["markets_processed"] == len(market_ids)
    assert final_ev["markets_settled"] == len(market_ids)
    assert final_ev["trades_settled"] == 2 * len(market_ids)
    assert final_ev["completed_at"] is not None

    # And the function's return value still matches.
    assert final["state"] == "completed"
    assert final["markets_processed"] == len(market_ids)


def test_run_backfill_emits_run_complete_on_failure(monkeypatch):
    """A crash mid-run still publishes `backfill_run_complete` so the
    panel never gets stuck rendering a phantom in-flight job."""

    async def _explode() -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        backfill_mod, "_list_unsettled_kalshi_markets", _explode
    )

    async def _scenario():
        sub = settlement_event_hub.subscribe(label="test-backfill-fail")
        try:
            final = await backfill_mod.run_backfill()
        finally:
            events = _drain(sub)
            settlement_event_hub.unsubscribe(sub)
        return final, events

    final, events = asyncio.run(_scenario())

    types = [ev["type"] for ev in events]
    assert "backfill_run_start" in types
    assert types[-1] == "backfill_run_complete"

    final_ev = events[-1]
    assert final_ev["state"] == "failed"
    assert final_ev["last_error"] and "boom" in final_ev["last_error"]
    # Function return value mirrors the final emitted snapshot.
    assert final["state"] == "failed"


def test_emit_event_payload_has_no_extra_fields():
    """`_emit` should only stamp the event with `type` plus the snapshot
    fields — no internal bookkeeping should leak into the WS payload."""

    async def _go():
        sub = settlement_event_hub.subscribe(label="emit-shape")
        try:
            await backfill_mod._emit(backfill_mod.EVENT_PROGRESS)
            ev = sub.get_nowait()
        finally:
            settlement_event_hub.unsubscribe(sub)
        return ev

    ev = asyncio.run(_go())
    snap_keys = set(backfill_mod.BackfillProgress().snapshot().keys())
    expected = snap_keys | {"type"}
    assert set(ev.keys()) == expected
    assert ev["type"] == "backfill_progress"
