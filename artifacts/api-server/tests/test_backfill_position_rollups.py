"""Unit tests for ``scripts/backfill_position_rollups.py``.

The script reads ``order_lifecycle`` and writes rolled-up rows into
``positions``. Tests stub ``app.db.fetch`` / ``app.db.execute`` with an
in-memory state machine so the rollup math, idempotency, and over-fill
detection can be exercised without Postgres.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


# Load the script as a module without executing argparse/main.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_position_rollups.py"
_spec = importlib.util.spec_from_file_location("backfill_position_rollups", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["backfill_position_rollups"] = backfill
_spec.loader.exec_module(backfill)  # type: ignore[union-attr]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def fake_db(monkeypatch):
    """Replace app.db.fetch / app.db.execute with an in-memory store."""
    state: dict[str, Any] = {
        # Per-order chronological fill rows. The fixture serialises them
        # back into the (order_id, id) ordering the SQL produces.
        "fills_by_order": {},
        # Per-order requested counts (from the `submitted` lifecycle row).
        "requested": {},
        "existing_positions": set(),
        "inserts": [],
    }

    async def fake_fetch(query: str, *args):
        if "FROM positions" in query:
            return [{"order_id": oid} for oid in sorted(state["existing_positions"])]
        if "event_type = 'filled'" in query:
            rows: list[dict[str, Any]] = []
            row_id = 0
            for oid in sorted(state["fills_by_order"]):
                for f in state["fills_by_order"][oid]:
                    row_id += 1
                    rows.append({
                        "order_id": oid,
                        "id": row_id,
                        "contracts": f["contracts"],
                        "price_cents": f["price_cents"],
                        "side": f.get("side", "yes"),
                        "market_id": f.get("market_id", "MKT-1"),
                        "vertical": f.get("vertical", "sports"),
                    })
            return rows
        if "event_type = 'submitted'" in query:
            return [
                {"order_id": oid, "requested_count": cnt}
                for oid, cnt in sorted(state["requested"].items())
            ]
        raise AssertionError(f"unexpected fetch: {query[:80]}")

    async def fake_execute(query: str, *args):
        state["inserts"].append({
            "order_id": args[0], "market_id": args[1], "side": args[2],
            "vertical": args[3], "quantity": args[4], "avg_price": args[5],
            "status": args[6], "over_filled": args[7],
        })
        state["existing_positions"].add(args[0])
        return "INSERT 0 1"

    async def fake_close_pool():
        return None

    from app import db
    monkeypatch.setattr(db, "fetch", fake_fetch)
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(db, "close_pool", fake_close_pool)
    return state


def _seed(state, order_id, *, fills, requested=None, side="yes",
          market="MKT-1", vertical="sports"):
    """fills = list of (contracts, price_cents) in arrival order."""
    state["fills_by_order"][order_id] = [
        {"contracts": c, "price_cents": p,
         "side": side, "market_id": market, "vertical": vertical}
        for c, p in fills
    ]
    if requested is not None:
        state["requested"][order_id] = requested


def test_weighted_average_rollup_writes_capped_position(fake_db):
    # Three partials: 3@50, 4@60, 3@70 -> weighted avg = 60c, qty = 10
    _seed(fake_db, "ord-1", fills=[(3, 50), (4, 60), (3, 70)], requested=10)

    res = _run(backfill.run_backfill())

    assert res.scanned == 1
    assert res.written == 1
    assert res.over_filled == 0
    ins = fake_db["inserts"]
    assert len(ins) == 1
    assert ins[0]["order_id"] == "ord-1"
    assert ins[0]["quantity"] == 10
    # Stored as unit price (0..1) to match the live helper.
    assert ins[0]["avg_price"] == pytest.approx(0.60)
    assert ins[0]["over_filled"] is False
    # Mirror live rollup (handle_ws_fill always passes closed=False) so
    # backfilled rows match historical reads. Settlement closes them later.
    assert ins[0]["status"] == "open"


def test_skips_orders_already_rolled_up(fake_db):
    """Idempotency: an order_id that already has a positions row is left alone."""
    fake_db["existing_positions"].add("ord-existing")
    _seed(fake_db, "ord-existing", fills=[(5, 50)], requested=5)
    _seed(fake_db, "ord-new", fills=[(2, 40)], requested=2)

    res = _run(backfill.run_backfill())

    assert res.scanned == 2
    assert res.skipped_existing == 1
    assert res.written == 1
    inserted_ids = [i["order_id"] for i in fake_db["inserts"]]
    assert inserted_ids == ["ord-new"]


def test_overfill_uses_only_accepted_contracts_for_avg_price(fake_db):
    """Over-fill: weighted-average must come from ACCEPTED contracts only.

    Replays the live ``handle_ws_fill`` semantics for an order requested
    at 5 contracts that receives partials of 2@40, 2@50, 3@90. The third
    fill exceeds remaining capacity by 2; only 1 contract is accepted at
    90c, so the average is (2*40 + 2*50 + 1*90) / 5 = 54c — NOT the
    naive 7-contract average of (80+100+270)/7 = 64c that would slip in
    if we summed before capping.
    """
    _seed(fake_db, "ord-of", fills=[(2, 40), (2, 50), (3, 90)], requested=5)

    res = _run(backfill.run_backfill())

    assert res.over_filled == 1
    ins = fake_db["inserts"][0]
    assert ins["quantity"] == 5
    assert ins["over_filled"] is True
    # Accepted-only weighted average — discarded contracts don't skew it.
    assert ins["avg_price"] == pytest.approx(0.54)
    assert ins["status"] == "open"


def test_partial_fill_open_status(fake_db):
    """Filled less than requested -> status='open', quantity=total_filled."""
    _seed(fake_db, "ord-partial", fills=[(3, 55)], requested=10)

    _run(backfill.run_backfill())

    ins = fake_db["inserts"][0]
    assert ins["quantity"] == 3
    assert ins["status"] == "open"
    assert ins["avg_price"] == pytest.approx(0.55)
    assert ins["over_filled"] is False


def test_skips_orders_with_no_submitted_row(fake_db):
    """No `submitted` lifecycle row means no requested_count cap; skip safely."""
    _seed(fake_db, "ord-orphan", fills=[(4, 50)], requested=None)

    res = _run(backfill.run_backfill())

    assert res.scanned == 1
    assert res.skipped_no_request == 1
    assert res.written == 0
    assert fake_db["inserts"] == []


def test_dry_run_writes_nothing(fake_db):
    _seed(fake_db, "ord-1", fills=[(2, 40)], requested=2)
    _seed(fake_db, "ord-2", fills=[(3, 50)], requested=3)

    res = _run(backfill.run_backfill(dry_run=True))

    assert res.written == 2
    assert fake_db["inserts"] == []


def test_limit_caps_writes(fake_db):
    for i in range(5):
        _seed(fake_db, f"ord-{i}", fills=[(2, 40)], requested=2)

    res = _run(backfill.run_backfill(limit=2))

    assert res.written == 2
    assert len(fake_db["inserts"]) == 2


def test_rerun_after_first_pass_is_idempotent(fake_db):
    """Re-running after a successful pass must not double-write any row."""
    _seed(fake_db, "ord-a", fills=[(2, 40)], requested=2)
    _seed(fake_db, "ord-b", fills=[(3, 50)], requested=3)

    first = _run(backfill.run_backfill())
    second = _run(backfill.run_backfill())

    assert first.written == 2
    assert second.written == 0
    assert second.skipped_existing == 2
    # Only the original two inserts ran.
    assert len(fake_db["inserts"]) == 2
