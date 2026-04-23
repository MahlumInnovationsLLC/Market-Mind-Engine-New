"""Tests for the per-vertical retrained calibrator loader.

The online-learning worker writes fresh isotonic snapshots to
``calibration_snapshots`` tagged ``drift_status.status='retrained'``;
``app.models.live_calibrator`` is the trading-pipeline consumer that
caches the latest one per vertical and exposes ``apply()``.

Covers:
  * apply() bypasses cleanly when no snapshot is loaded
  * apply() interpolates across isotonic knots once loaded
  * the cache short-circuits the second call (no duplicate DB fetch)
  * a published ``retrain_complete`` event invalidates the cached entry
  * non-retrained / rolled-back rows are filtered out by the loader
  * malformed calibrator state degrades to a pass-through
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from app import db as _db
from app.models import live_calibrator
from app.settlement.streams import settlement_event_hub


class _FakeDb:
    """Tiny shim: holds rows that look like ``calibration_snapshots``
    and answers the single SELECT the loader issues.
    """

    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []
        self.fetchrow_calls = 0

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_calls += 1
        q = " ".join(query.split())
        assert "FROM calibration_snapshots" in q
        assert "drift_status->>'status'" in q
        vertical = args[0]
        rows = [
            r for r in self.snapshots
            if r.get("vertical") == vertical
            and r.get("status") == "retrained"
            and r.get("calibrator_state") not in (None, "{}")
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        r = rows[0]
        return {
            "id": r["id"],
            "calibrator_state": r["calibrator_state"],
            "sample_count": r["sample_count"],
            "fit_at": float(r["created_at"]),
        }


@pytest.fixture
def fake_db(monkeypatch):
    fake = _FakeDb()
    monkeypatch.setattr(_db, "fetchrow", fake.fetchrow)
    live_calibrator.invalidate()
    yield fake
    live_calibrator.invalidate()


def _isotonic_state() -> str:
    return json.dumps({
        "type": "isotonic",
        "x": [0.0, 0.5, 1.0],
        "y": [0.1, 0.4, 0.9],
        "n": 200,
    })


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------


def test_apply_bypasses_when_vertical_missing(fake_db):
    out = asyncio.run(live_calibrator.apply(None, 0.7))
    assert out == pytest.approx(0.7)
    assert fake_db.fetchrow_calls == 0


def test_apply_bypasses_when_no_snapshot(fake_db):
    out = asyncio.run(live_calibrator.apply("sports", 0.6))
    assert out == pytest.approx(0.6)
    # The miss is cached as None.
    assert fake_db.fetchrow_calls == 1
    out2 = asyncio.run(live_calibrator.apply("sports", 0.3))
    assert out2 == pytest.approx(0.3)
    # Cached negative result — no second DB fetch.
    assert fake_db.fetchrow_calls == 1


def test_apply_interpolates_across_isotonic_knots(fake_db):
    fake_db.snapshots.append({
        "id": 7,
        "vertical": "sports",
        "status": "retrained",
        "calibrator_state": _isotonic_state(),
        "sample_count": 200,
        "created_at": time.time(),
    })
    out = asyncio.run(live_calibrator.apply("sports", 0.5))
    assert out == pytest.approx(0.4)  # exact knot
    out2 = asyncio.run(live_calibrator.apply("sports", 0.25))
    # Linear midpoint between 0.1 and 0.4
    assert out2 == pytest.approx(0.25)
    # Two apply calls -> still only one fetchrow (cache hit on second).
    assert fake_db.fetchrow_calls == 1


def test_apply_clamps_input(fake_db):
    fake_db.snapshots.append({
        "id": 1,
        "vertical": "crypto",
        "status": "retrained",
        "calibrator_state": _isotonic_state(),
        "sample_count": 50,
        "created_at": time.time(),
    })
    out_lo = asyncio.run(live_calibrator.apply("crypto", -0.5))
    out_hi = asyncio.run(live_calibrator.apply("crypto", 1.5))
    assert out_lo == pytest.approx(0.1)  # x[0] -> y[0]
    assert out_hi == pytest.approx(0.9)  # x[-1] -> y[-1]


def test_loader_picks_most_recent_retrained_row(fake_db):
    now = time.time()
    fake_db.snapshots.extend([
        {
            "id": 1,
            "vertical": "sports",
            "status": "retrained",
            "calibrator_state": json.dumps({
                "type": "isotonic",
                "x": [0.0, 1.0], "y": [0.0, 1.0],  # identity
            }),
            "sample_count": 100,
            "created_at": now - 1000,
        },
        {
            "id": 2,
            "vertical": "sports",
            "status": "retrained",
            "calibrator_state": _isotonic_state(),  # squashes to 0.4
            "sample_count": 200,
            "created_at": now,
        },
    ])
    out = asyncio.run(live_calibrator.apply("sports", 0.5))
    # Newer row (id=2) wins: 0.5 -> 0.4
    assert out == pytest.approx(0.4)


def test_loader_skips_rolled_back_and_non_retrained_rows(fake_db):
    now = time.time()
    fake_db.snapshots.extend([
        {
            "id": 5,
            "vertical": "sports",
            "status": "rolled_back",
            "calibrator_state": _isotonic_state(),
            "sample_count": 200,
            "created_at": now,
        },
        {
            "id": 6,
            "vertical": "sports",
            "status": "fitted",  # per-league refit, not retrain
            "calibrator_state": _isotonic_state(),
            "sample_count": 50,
            "created_at": now,
        },
    ])
    out = asyncio.run(live_calibrator.apply("sports", 0.5))
    assert out == pytest.approx(0.5)  # nothing matched; pass-through


def test_malformed_state_degrades_to_passthrough(fake_db):
    fake_db.snapshots.append({
        "id": 9,
        "vertical": "weather",
        "status": "retrained",
        "calibrator_state": json.dumps({"type": "isotonic", "x": [], "y": []}),
        "sample_count": 0,
        "created_at": time.time(),
    })
    out = asyncio.run(live_calibrator.apply("weather", 0.42))
    assert out == pytest.approx(0.42)


def test_unknown_calibrator_type_falls_back(fake_db):
    fake_db.snapshots.append({
        "id": 11,
        "vertical": "events_econ",
        "status": "retrained",
        "calibrator_state": json.dumps({"type": "platt", "a": 2.0, "b": -1.0}),
        "sample_count": 30,
        "created_at": time.time(),
    })
    # The retrain worker only writes isotonic; non-isotonic rows are
    # treated as a miss so we don't risk applying a wrong shape.
    out = asyncio.run(live_calibrator.apply("events_econ", 0.5))
    assert out == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Cache invalidation via the settlement event hub
# ---------------------------------------------------------------------------


def test_retrain_complete_event_invalidates_cache(fake_db):
    async def _run() -> None:
        # Seed an initial calibrator and warm the cache.
        fake_db.snapshots.append({
            "id": 1,
            "vertical": "sports",
            "status": "retrained",
            "calibrator_state": json.dumps({
                "type": "isotonic",
                "x": [0.0, 1.0], "y": [0.0, 1.0],  # identity
            }),
            "sample_count": 100,
            "created_at": time.time() - 100,
        })
        out = await live_calibrator.apply("sports", 0.5)
        assert out == pytest.approx(0.5)  # identity calibrator
        before_calls = fake_db.fetchrow_calls

        # Promote a freshly-retrained snapshot in the DB and announce
        # it on the hub.  The listener should drop the cache entry so
        # the next apply reloads.
        fake_db.snapshots.append({
            "id": 2,
            "vertical": "sports",
            "status": "retrained",
            "calibrator_state": _isotonic_state(),  # 0.5 -> 0.4
            "sample_count": 200,
            "created_at": time.time(),
        })
        live_calibrator.start_invalidation_listener()
        try:
            await settlement_event_hub.publish({
                "type": "retrain_complete",
                "vertical": "sports",
                "snapshot_id": 2,
                "ok": True,
            })
            # Yield so the listener task drains the queue and clears
            # the cached entry.  The listener runs in a separate task
            # so we need a real (non-zero) sleep to let it actually
            # be scheduled.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if "sports" not in live_calibrator.cache_snapshot():
                    break
            out2 = await live_calibrator.apply("sports", 0.5)
            assert out2 == pytest.approx(0.4)
            assert fake_db.fetchrow_calls > before_calls
        finally:
            live_calibrator.stop_invalidation_listener()

    asyncio.run(_run())


def test_invalidate_drops_specific_vertical(fake_db):
    async def _run() -> None:
        fake_db.snapshots.append({
            "id": 1,
            "vertical": "sports",
            "status": "retrained",
            "calibrator_state": _isotonic_state(),
            "sample_count": 50,
            "created_at": time.time(),
        })
        await live_calibrator.apply("sports", 0.5)
        await live_calibrator.apply("crypto", 0.5)  # negative cache
        assert fake_db.fetchrow_calls == 2
        live_calibrator.invalidate("crypto")
        await live_calibrator.apply("crypto", 0.5)
        assert fake_db.fetchrow_calls == 3
        # sports cache untouched.
        await live_calibrator.apply("sports", 0.5)
        assert fake_db.fetchrow_calls == 3

    asyncio.run(_run())
