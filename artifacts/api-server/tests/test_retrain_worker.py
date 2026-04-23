"""Tests for the online-learning retrain worker.

The worker drains `online_learning_buffer`, refits a per-vertical
isotonic calibrator when warranted, writes a `calibration_snapshots`
row tagged retrained, and announces the result on the settlement
event hub.

These tests use an in-memory shim for `app.db` so the asyncpg pool is
never touched, and a sniffer queue on the real `settlement_event_hub`
so we can assert the published events.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import db as _db
from app.settlement import retrain as _retrain
from app.settlement.streams import settlement_event_hub


# ---------------------------------------------------------------------------
# In-memory DB shim
# ---------------------------------------------------------------------------


class _FakeDb:
    """Just enough postgres to drive `retrain.run_retrain_cycle`.

    Holds a list of buffer rows and a list of inserted snapshots; the
    `model_health_alerts` table is modelled as a set of verticals with
    an active `brier_drift` alert.
    """

    def __init__(self) -> None:
        # buffer rows: dicts with id, vertical, predicted_prob, actual_outcome, processed
        self.buffer: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.active_alert_verticals: set[str] = set()
        self._next_snapshot_id = 1

    # asyncpg-style call shapes
    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = " ".join(query.split())
        if "online_learning_buffer" in q and "GROUP BY vertical" in q:
            counts: dict[str, int] = {}
            for r in self.buffer:
                if r["processed"]:
                    continue
                if r["predicted_prob"] is None or r["actual_outcome"] is None:
                    continue
                if r["vertical"] is None:
                    continue
                counts[r["vertical"]] = counts.get(r["vertical"], 0) + 1
            return [
                {
                    "vertical": v,
                    "n": n,
                    "active_alert": v in self.active_alert_verticals,
                }
                for v, n in counts.items()
            ]
        if "FROM online_learning_buffer" in q and "ORDER BY id" in q:
            vertical, limit = args
            out = []
            for r in sorted(self.buffer, key=lambda x: x["id"]):
                if r["processed"] or r["vertical"] != vertical:
                    continue
                if r["predicted_prob"] is None or r["actual_outcome"] is None:
                    continue
                out.append({
                    "id": r["id"],
                    "p": float(r["predicted_prob"]),
                    "y": int(r["actual_outcome"]),
                })
                if len(out) >= limit:
                    break
            return out
        raise AssertionError(f"unhandled fetch: {q!r}")

    async def execute(self, query: str, *args: Any) -> str:
        q = " ".join(query.split())
        if "UPDATE online_learning_buffer SET processed = true" in q:
            ids = set(int(i) for i in args[0])
            for r in self.buffer:
                if r["id"] in ids:
                    r["processed"] = True
            return f"UPDATE {len(ids)}"
        raise AssertionError(f"unhandled execute: {q!r}")

    async def fetchval(self, query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if "INSERT INTO calibration_snapshots" in q and "RETURNING id" in q:
            (vertical, ece, mce, brier, n, calibrator_state_json,
             triggered_by, brier_warn, brier_crit) = args
            sid = self._next_snapshot_id
            self._next_snapshot_id += 1
            self.snapshots.append({
                "id": sid,
                "vertical": vertical,
                "sub_category": "retrain",
                "ece": ece,
                "mce": mce,
                "brier_score": brier,
                "sample_count": n,
                "calibrator_state": calibrator_state_json,
                "drift_status": {
                    "status": "retrained",
                    "triggered_by": triggered_by,
                    "brier_warn": brier_warn,
                    "brier_crit": brier_crit,
                },
            })
            return sid
        raise AssertionError(f"unhandled fetchval: {q!r}")


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeDb:
    fdb = _FakeDb()
    monkeypatch.setattr(_db, "fetch", fdb.fetch)
    monkeypatch.setattr(_db, "execute", fdb.execute)
    monkeypatch.setattr(_db, "fetchval", fdb.fetchval)
    return fdb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(i: int, vertical: str, p: float, y: int, processed: bool = False) -> dict[str, Any]:
    return {
        "id": i,
        "vertical": vertical,
        "predicted_prob": p,
        "actual_outcome": y,
        "processed": processed,
    }


def _sniff_events() -> tuple[asyncio.Queue, callable]:
    q = settlement_event_hub.subscribe()
    return q, lambda: settlement_event_hub.unsubscribe(q)


def _drain(q: asyncio.Queue) -> list[dict[str, Any]]:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_retrain_skipped_when_buffer_below_threshold(fake_db: _FakeDb) -> None:
    """Fewer than MIN_BATCH rows and no active alert -> no refit, no snapshot."""
    for i in range(_retrain.MIN_BATCH - 1):
        fake_db.buffer.append(_row(i, "sports", 0.5, i % 2))

    async def go() -> None:
        q, unsub = _sniff_events()
        try:
            cr = await _retrain.run_retrain_cycle()
            assert cr.verticals_examined == 1
            assert cr.verticals_retrained == 0
            assert cr.rows_processed == 0
            assert fake_db.snapshots == []
            # Buffer rows still un-processed, ready for a future cycle.
            assert all(r["processed"] is False for r in fake_db.buffer)
            evts = _drain(q)
            assert any(e["type"] == "retrain_skipped" for e in evts)
            assert not any(e["type"] == "retrain_complete" for e in evts)
        finally:
            unsub()

    asyncio.run(go())


def test_retrain_fires_when_batch_threshold_met(fake_db: _FakeDb) -> None:
    """>= MIN_BATCH rows triggers a fit + snapshot + processed mark."""
    # Manufacture a clearly mis-calibrated dataset: model_prob ~ 0.5 but
    # outcomes lean YES.  Isotonic should pull the calibrated probability
    # up, dropping post-fit Brier below pre-fit Brier.
    for i in range(_retrain.MIN_BATCH + 5):
        fake_db.buffer.append(_row(i, "sports", 0.5, 1 if i % 4 != 0 else 0))

    async def go() -> None:
        q, unsub = _sniff_events()
        try:
            cr = await _retrain.run_retrain_cycle()
            assert cr.verticals_retrained == 1
            assert cr.rows_processed == _retrain.MIN_BATCH + 5
            assert len(fake_db.snapshots) == 1
            snap = fake_db.snapshots[0]
            assert snap["vertical"] == "sports"
            assert snap["sub_category"] == "retrain"
            assert snap["sample_count"] == _retrain.MIN_BATCH + 5
            assert snap["drift_status"]["status"] == "retrained"
            assert snap["drift_status"]["triggered_by"] == "batch"
            # Calibrator state was persisted with isotonic knots.
            import json as _j
            state = _j.loads(snap["calibrator_state"])
            assert state["type"] == "isotonic"
            assert state["n"] == _retrain.MIN_BATCH + 5
            assert state["brier_after"] <= state["brier_before"] + 1e-9
            # All buffer rows now processed.
            assert all(r["processed"] for r in fake_db.buffer)
            # Settlement bus saw decision + complete events.
            evts = _drain(q)
            kinds = {e["type"] for e in evts}
            assert "retrain_decision" in kinds
            assert "retrain_complete" in kinds
            assert "retrain_cycle_complete" in kinds
        finally:
            unsub()

    asyncio.run(go())


def test_active_alert_lowers_threshold(fake_db: _FakeDb) -> None:
    """An unack'd brier_drift alert refits even with very few rows."""
    # Only 3 rows — well below MIN_BATCH — but the vertical has an
    # active drift alert so we should still refit.
    fake_db.buffer.extend([
        _row(1, "sports", 0.7, 1),
        _row(2, "sports", 0.6, 0),
        _row(3, "sports", 0.55, 1),
    ])
    fake_db.active_alert_verticals.add("sports")

    async def go() -> None:
        q, unsub = _sniff_events()
        try:
            cr = await _retrain.run_retrain_cycle()
            assert cr.verticals_retrained == 1
            assert cr.rows_processed == 3
            assert fake_db.snapshots[0]["drift_status"]["triggered_by"] == "alert"
            evts = _drain(q)
            decision = next(e for e in evts if e["type"] == "retrain_decision")
            assert decision["triggered_by"] == "alert"
            assert decision["unprocessed"] == 3
        finally:
            unsub()

    asyncio.run(go())


def test_buffer_rows_left_when_snapshot_insert_fails(
    fake_db: _FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the snapshot can't be persisted, do NOT mark the buffer rows
    processed — we want the next cycle to retry on a healthy DB.
    """
    for i in range(_retrain.MIN_BATCH):
        fake_db.buffer.append(_row(i, "sports", 0.5, i % 2))

    async def boom_fetchval(*_a: Any, **_kw: Any) -> Any:
        return None  # Simulate insert returning no id.

    monkeypatch.setattr(_db, "fetchval", boom_fetchval)

    async def go() -> None:
        cr = await _retrain.run_retrain_cycle()
        assert cr.verticals_retrained == 0
        assert cr.rows_processed == 0
        assert all(r["processed"] is False for r in fake_db.buffer)

    asyncio.run(go())


def _make_brier_regression_buffer(fake_db: _FakeDb) -> None:
    """Stuff the buffer with enough well-formed rows for a refit to
    even be attempted.  Pair this with `_force_brier_regression` to
    actually trip the rollback gate — isotonic regression on real
    data is monotonic by construction and rarely worsens Brier, so
    we patch the fit's reported numbers to simulate the bug-or-noise
    case the gate is built to defend against.
    """
    for i in range(_retrain.MIN_BATCH):
        fake_db.buffer.append(_row(i, "sports", 0.5 + 0.001 * i, i % 2))


def _force_brier_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap the real fitter so it reports a *worse* post-Brier."""
    real_fit = _retrain._fit_isotonic_async

    async def bad_fit(probs, outcomes):
        st = await real_fit(probs, outcomes)
        b_before = float(st.get("brier_before") or 0.0)
        st["brier_after"] = b_before + 0.05  # well above ROLLBACK_TOLERANCE
        return st

    monkeypatch.setattr(_retrain, "_fit_isotonic_async", bad_fit)


def test_rollback_writes_log_and_leaves_buffer_unacked(
    fake_db: _FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Brier worsens beyond tolerance: write rollback row, raise
    health alert, tag snapshot rolled_back, and leave buffer un-acked.
    """
    _make_brier_regression_buffer(fake_db)
    _force_brier_regression(monkeypatch)
    rollback_inserts: list[tuple[Any, ...]] = []
    health_inserts: list[tuple[Any, ...]] = []
    snapshot_updates: list[tuple[Any, ...]] = []

    real_fetchval = fake_db.fetchval
    real_execute = fake_db.execute

    async def fetchval_router(query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if "INSERT INTO model_rollback_log" in q:
            rollback_inserts.append(args)
            return 9001
        if "FROM calibration_snapshots" in q and "rolled_back" in q:
            return None  # no prior good snapshot in this test
        if "INSERT INTO calibration_snapshots" in q:
            return await real_fetchval(query, *args)
        return None

    async def execute_router(query: str, *args: Any) -> str:
        q = " ".join(query.split())
        if "INSERT INTO model_health_alerts" in q:
            health_inserts.append(args)
            return "INSERT 1"
        if "UPDATE calibration_snapshots" in q and "drift_status" in q:
            snapshot_updates.append(args)
            return "UPDATE 1"
        return await real_execute(query, *args)

    monkeypatch.setattr(_db, "fetchval", fetchval_router)
    monkeypatch.setattr(_db, "execute", execute_router)

    async def go() -> None:
        cr = await _retrain.run_retrain_cycle()
        assert cr.rollbacks == 1, "expected exactly one rollback"
        assert cr.rows_processed == 0, "rolled-back refit must not ack buffer rows"
        assert all(r["processed"] is False for r in fake_db.buffer)
        assert len(rollback_inserts) == 1
        assert len(health_inserts) == 1
        assert len(snapshot_updates) == 1

    asyncio.run(go())


def test_rollback_fails_closed_when_audit_log_write_fails(
    fake_db: _FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `_write_rollback`'s INSERT raises, we still must NOT promote
    the bad refit nor ack buffer rows. The detection of the regression
    is the source of truth for rollback; the audit-log row is best
    effort.
    """
    _make_brier_regression_buffer(fake_db)
    _force_brier_regression(monkeypatch)
    real_fetchval = fake_db.fetchval

    async def fetchval_router(query: str, *args: Any) -> Any:
        q = " ".join(query.split())
        if "INSERT INTO model_rollback_log" in q:
            raise RuntimeError("simulated DB outage on audit log")
        if "FROM calibration_snapshots" in q and "rolled_back" in q:
            return None
        if "INSERT INTO calibration_snapshots" in q:
            return await real_fetchval(query, *args)
        return None

    async def execute_swallow(query: str, *args: Any) -> str:
        q = " ".join(query.split())
        if "INSERT INTO model_health_alerts" in q:
            return "INSERT 1"
        if "UPDATE calibration_snapshots" in q and "drift_status" in q:
            return "UPDATE 1"
        if "UPDATE online_learning_buffer SET processed = true" in q:
            ids = set(int(i) for i in args[0])
            for r in fake_db.buffer:
                if r["id"] in ids:
                    r["processed"] = True
            return f"UPDATE {len(ids)}"
        return ""

    monkeypatch.setattr(_db, "fetchval", fetchval_router)
    monkeypatch.setattr(_db, "execute", execute_swallow)

    async def go() -> None:
        cr = await _retrain.run_retrain_cycle()
        # Even though the audit-log INSERT raised, the gate fired and
        # the rollback was counted; promotion was skipped; buffer
        # rows were left un-acked for the next cycle.
        assert cr.rollbacks == 1
        assert cr.rows_processed == 0
        assert cr.verticals_retrained == 0
        assert all(r["processed"] is False for r in fake_db.buffer)

    asyncio.run(go())


def test_multiple_verticals_independent(fake_db: _FakeDb) -> None:
    """Per-vertical decisions are independent: 'sports' has enough rows,
    'crypto' does not — only sports should refit.
    """
    for i in range(_retrain.MIN_BATCH):
        fake_db.buffer.append(_row(i, "sports", 0.4, 1))
    for i in range(5):
        fake_db.buffer.append(_row(1000 + i, "crypto", 0.3, 0))

    async def go() -> None:
        cr = await _retrain.run_retrain_cycle()
        assert cr.verticals_retrained == 1
        assert {s["vertical"] for s in fake_db.snapshots} == {"sports"}
        # crypto rows untouched.
        assert all(
            r["processed"] is False
            for r in fake_db.buffer if r["vertical"] == "crypto"
        )

    asyncio.run(go())
