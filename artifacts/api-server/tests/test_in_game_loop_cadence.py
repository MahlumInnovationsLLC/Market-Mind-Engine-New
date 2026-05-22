"""Tests for the in-game loop scheduler scaffolding (task #830).

Behaviour under test:
  * Ships per-sport disabled — first invocation on a fresh
    runtime_settings table returns ``enabled=False`` and a
    ``skipped_reason`` referencing the per-sport flag.
  * When enabled, a second invocation inside the cadence window is
    a no-op (skipped_reason mentions ``cadence floor``).
  * ``_tag_is_live`` is called with the market_ids of live-eligible
    candidates and the result counts the flipped rows.
  * The predict stub returns None (per-sport live-prediction wiring
    is a follow-up task).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.learning import in_game_loop as _loop


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop._reset_state_for_tests()
    yield
    loop.close()
    _loop._reset_state_for_tests()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeDB:
    def __init__(
        self,
        *,
        enabled: bool,
        cadence: str | None = None,
        rows: list[dict[str, Any]] | None = None,
        flipped: int = 0,
    ) -> None:
        self.enabled = enabled
        self.cadence = cadence
        self.rows = rows or []
        self.flipped = flipped
        self.update_calls: list[tuple] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "runtime_settings" in query and "live_loop_enabled_" in args[0]:
            return {"value": "true" if self.enabled else "false"}
        if "runtime_settings" in query and "live_loop_cadence_" in args[0]:
            return {"value": self.cadence} if self.cadence else None
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "sportradar_live_state" in query:
            return self.rows
        return []

    async def execute(self, query: str, *args: Any) -> str:
        if "UPDATE predictions" in query and "is_live = true" in query:
            self.update_calls.append(args)
            return f"UPDATE {self.flipped}"
        return "OK"


def test_disabled_by_default(monkeypatch):
    # Bare DB — no runtime_settings rows at all → defaults apply.
    class _Empty:
        async def fetchrow(self, *a, **kw):
            return None

        async def fetch(self, *a, **kw):
            return []

        async def execute(self, *a, **kw):
            return "OK"

    monkeypatch.setattr(_loop, "db", _Empty())
    result = _run(_loop.run_in_game_cycle("nba"))
    assert result.enabled is False
    assert result.skipped_reason is not None
    assert "live_loop_enabled_nba" in result.skipped_reason
    assert result.candidates_scanned == 0


def test_enabled_runs_then_cadence_skips_second_call(monkeypatch):
    fake = _FakeDB(
        enabled=True,
        cadence="3600",  # long cadence so the second call must skip
        rows=[
            {
                "market_id": "KX-NBA-A", "league": "NBA",
                "game_id": "g1", "game_time": None, "is_live": False,
                "model_yes_prob": 0.6, "kalshi_price": 0.55,
                "status": "inprogress", "state_age_seconds": 5.0,
            },
            {
                "market_id": "KX-NBA-B", "league": "NBA",
                "game_id": "g2", "game_time": None, "is_live": True,
                "model_yes_prob": 0.6, "kalshi_price": 0.55,
                "status": "inprogress", "state_age_seconds": 5.0,
            },
        ],
        flipped=1,
    )
    monkeypatch.setattr(_loop, "db", fake)

    r1 = _run(_loop.run_in_game_cycle("nba"))
    assert r1.enabled is True
    assert r1.skipped_reason is None
    assert r1.candidates_scanned == 2
    assert r1.candidates_live == 2
    assert r1.candidates_tagged == 1, (
        "only the row that was previously is_live=false should flip"
    )
    # The UPDATE must have been called with just the not-yet-live id.
    assert len(fake.update_calls) == 1
    assert fake.update_calls[0][0] == ["KX-NBA-A"]

    r2 = _run(_loop.run_in_game_cycle("nba"))
    assert r2.enabled is True
    assert r2.skipped_reason is not None
    assert "cadence" in r2.skipped_reason.lower()
    assert r2.candidates_scanned == 0, "cadence skip must not refetch"


def test_predict_live_for_market_returns_none_for_unwired_sport():
    # Soccer is the first sport with a wired predictor on the generic
    # dispatcher path (task #857).  NBA is wired on its own meta-model
    # path via `_predict_live_for_market_nba` (task #855), so on the
    # generic dispatcher it still returns (None, metadata) — same as
    # NHL / MLB / NFL — so the loop only tags rows + drives the
    # dashboard tile for them on this path.
    prob, meta = _run(_loop._predict_live_for_market(
        {"market_id": "KX-NBA-A", "league": "NBA"}
    ))
    assert prob is None
    assert isinstance(meta, dict)
    assert meta.get("source") is None
    assert "NBA" in meta.get("reason", "")


def test_minute_bucket_idempotency_within_window(monkeypatch):
    fake = _FakeDB(
        enabled=True, cadence="0.01",  # cadence basically nil
        rows=[{
            "market_id": "KX-NBA-A", "league": "NBA",
            "game_id": "g1", "game_time": None, "is_live": False,
            "model_yes_prob": 0.6, "kalshi_price": 0.55,
            "status": "inprogress", "state_age_seconds": 5.0,
        }],
        flipped=1,
    )
    monkeypatch.setattr(_loop, "db", fake)

    # First pass tags; second pass within the same minute bucket
    # short-circuits via _TAGGED_THIS_MINUTE before issuing a UPDATE.
    r1 = _run(_loop.run_in_game_cycle("nba"))
    assert r1.candidates_tagged == 1
    # Wait past the (tiny) cadence window so we re-enter the work
    # path, but still inside the same minute bucket.
    import time as _t
    _t.sleep(0.05)
    fake.flipped = 0  # the row is already live now
    r2 = _run(_loop.run_in_game_cycle("nba"))
    assert r2.candidates_skipped_duplicate == 1, (
        "same-minute-bucket re-evaluation must be a no-op via "
        "the _TAGGED_THIS_MINUTE idempotency cache"
    )
