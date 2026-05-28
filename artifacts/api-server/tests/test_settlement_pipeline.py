"""Settlement pipeline: happy path + the 5 swallow-site safety nets.

Task #925 hardens the exception hygiene in
``app.settlement.pipeline``.  This file is its dedicated unit-test
companion — before #925 the pipeline only had broad integration
coverage in ``test_three_way_settlement.py`` etc., so a refactor that
broke (say) the postmortem insert would not have surfaced an obvious
red light.

Coverage map (mirrors the docstring inside ``pipeline.py``):

  * Happy path -- single trade, every downstream write succeeds, the
    settlement contract is honoured (trade row updated, postmortem +
    online-learning rows inserted, position closed).

  * Swallow site #1 — canary decrement raises -> counter advances,
    settlement still succeeds.
  * Swallow site #2 — postmortem insert raises.
  * Swallow site #3 — online-learning-buffer insert raises.
  * Swallow site #4 — precip EMOS stamp raises (called from
    ``settle_market``; we drive it directly via the private helper).
  * Swallow site #5 — position close raises.

Each swallow test asserts (a) ``settlement_swallow_counts()[site]``
advanced by exactly 1, and (b) the trade row itself was still
``UPDATE``d -- the contract of these five swallow sites is "settlement
of the trade is sacred; everything else is best-effort".
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.settlement import pipeline as _pipeline


# ---------------------------------------------------------------------------
# Test-only fake DB.
#
# We don't import _stub_db from test_three_way_settlement.py because we
# need finer control: each test wants to inject failures at one
# specific downstream write and observe the trade UPDATE still landed.
# ---------------------------------------------------------------------------


class _FakeDB:
    """Minimal asyncpg-like stub for the four db helpers settle_market uses."""

    def __init__(
        self,
        *,
        trade_rows: list[dict[str, Any]] | None = None,
        fail_on_execute_for: str | None = None,
        fail_on_fetchrow: bool = False,
    ) -> None:
        self.trade_rows = trade_rows or []
        self.fail_on_execute_for = fail_on_execute_for
        self.fail_on_fetchrow = fail_on_fetchrow
        # Captured side-effects ---------------------------------------
        self.trade_updates: list[dict[str, Any]] = []
        self.postmortems: list[dict[str, Any]] = []
        self.online_buffer: list[dict[str, Any]] = []
        self.precip_stamps: list[tuple] = []
        self.position_closes: list[tuple] = []

    # asyncpg surface ------------------------------------------------------

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM trades" in query and "settled_at IS NULL" in query:
            return self.trade_rows
        return []

    async def fetchrow(
        self, query: str, *args: Any,
    ) -> dict[str, Any] | None:
        if "UPDATE trades" in query and "RETURNING id" in query:
            if self.fail_on_fetchrow:
                raise RuntimeError("simulated trade UPDATE failure")
            self.trade_updates.append({"id": args[0], "pnl": args[1]})
            return {"id": args[0]}
        return None

    async def fetchval(self, *_a: Any, **_kw: Any) -> Any:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO trade_postmortems" in query:
            if self.fail_on_execute_for == "postmortem":
                raise RuntimeError("simulated postmortem write failure")
            self.postmortems.append({"trade_id": args[0]})
        elif "INSERT INTO online_learning_buffer" in query:
            if self.fail_on_execute_for == "online_buffer":
                raise RuntimeError("simulated online_learning write failure")
            self.online_buffer.append({"trade_id": args[0]})
        elif "UPDATE precip_emos_observations" in query:
            if self.fail_on_execute_for == "precip":
                raise RuntimeError("simulated precip stamp failure")
            self.precip_stamps.append(args)
        elif "UPDATE positions" in query and "status = 'closed'" in query:
            if self.fail_on_execute_for == "position_close":
                raise RuntimeError("simulated position close failure")
            self.position_closes.append(args)
        return "OK"


def _wire(monkeypatch: pytest.MonkeyPatch, fake: _FakeDB) -> None:
    monkeypatch.setattr(_pipeline.db, "fetch", fake.fetch)
    monkeypatch.setattr(_pipeline.db, "fetchrow", fake.fetchrow)
    monkeypatch.setattr(_pipeline.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(_pipeline.db, "execute", fake.execute)
    # Push fan-out: stub so we don't drag the notifications module into
    # the settlement-only contract under test here.
    from app.notifications import push as _push
    monkeypatch.setattr(_push, "push_settlement", lambda **_kw: None)


def _trade(tid: int = 1, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": tid, "trade_id": f"t{tid}",
        "market_id": "MKT-NBA-1",
        "vertical": "sports", "league": "NBA",
        "side": "yes", "action": "buy",
        "quantity": 10.0, "price": 0.4,
        "model_prob": 0.55, "model_edge": 0.05, "edge": 0.05,
        "exchange": "kalshi", "trading_mode": "live",
        "metadata": {},
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_swallow_counters() -> None:
    """Each test starts with zeroed per-site counters."""
    for k in list(_pipeline._swallow_counts.keys()):
        _pipeline._swallow_counts[k] = 0


def _settle(market_id: str = "MKT-NBA-1", outcome_yes: int = 1) -> dict:
    return asyncio.run(
        _pipeline.settle_market(
            market_id, outcome_yes=outcome_yes, settle_price=float(outcome_yes),
        )
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_writes_every_downstream_row(monkeypatch):
    """Single YES trade, market settles YES.  Trade gets UPDATEd, a
    postmortem + online-buffer row both land, the position closes, and
    no swallow counter advances."""
    fake = _FakeDB(trade_rows=[_trade(1)])
    _wire(monkeypatch, fake)

    _settle()

    assert len(fake.trade_updates) == 1
    assert fake.trade_updates[0]["id"] == 1
    # 10 contracts * (1.0 payout - 0.4 entry) = +6.0
    assert fake.trade_updates[0]["pnl"] == pytest.approx(6.0)
    assert len(fake.postmortems) == 1
    assert len(fake.online_buffer) == 1
    assert len(fake.position_closes) == 1
    # No swallows on the happy path.
    assert _pipeline.settlement_swallow_counts() == {
        "canary_decrement": 0,
        "postmortem_insert": 0,
        "online_learning_buffer_insert": 0,
        "precip_emos_stamp": 0,
        "position_close": 0,
    }


# ---------------------------------------------------------------------------
# Swallow site #1 — canary decrement
# ---------------------------------------------------------------------------


def test_canary_decrement_failure_is_swallowed_and_counted(monkeypatch):
    fake = _FakeDB(trade_rows=[_trade(1)])
    _wire(monkeypatch, fake)

    # The canary module is imported lazily inside the swallow block, so
    # we patch the module's ``decrement_on_settle`` to raise.  Settlement
    # must continue and the counter must advance by exactly 1.
    from app.risk import canary as _canary

    async def _boom(_trade: dict) -> None:
        raise RuntimeError("canary subsystem unavailable")

    monkeypatch.setattr(_canary, "decrement_on_settle", _boom)

    _settle()

    counts = _pipeline.settlement_swallow_counts()
    assert counts["canary_decrement"] == 1
    assert counts["postmortem_insert"] == 0
    # Trade itself is sacred — the UPDATE still ran.
    assert len(fake.trade_updates) == 1


# ---------------------------------------------------------------------------
# Swallow site #2 — postmortem insert
# ---------------------------------------------------------------------------


def test_postmortem_insert_failure_is_swallowed_and_counted(monkeypatch):
    fake = _FakeDB(
        trade_rows=[_trade(1)], fail_on_execute_for="postmortem",
    )
    _wire(monkeypatch, fake)

    _settle()

    counts = _pipeline.settlement_swallow_counts()
    assert counts["postmortem_insert"] == 1
    assert counts["online_learning_buffer_insert"] == 0
    # The postmortem write blew up, so nothing landed in `postmortems`...
    assert fake.postmortems == []
    # ... but the trade UPDATE and the *subsequent* online-buffer
    # INSERT both still ran — the pipeline must NOT short-circuit on a
    # best-effort writer.
    assert len(fake.trade_updates) == 1
    assert len(fake.online_buffer) == 1


# ---------------------------------------------------------------------------
# Swallow site #3 — online-learning-buffer insert
# ---------------------------------------------------------------------------


def test_online_learning_buffer_failure_is_swallowed_and_counted(monkeypatch):
    fake = _FakeDB(
        trade_rows=[_trade(1)], fail_on_execute_for="online_buffer",
    )
    _wire(monkeypatch, fake)

    _settle()

    counts = _pipeline.settlement_swallow_counts()
    assert counts["online_learning_buffer_insert"] == 1
    assert counts["postmortem_insert"] == 0
    assert fake.online_buffer == []
    assert len(fake.trade_updates) == 1
    assert len(fake.postmortems) == 1


# ---------------------------------------------------------------------------
# Swallow site #4 — precip EMOS stamp
#
# The stamp helper is only called for a market_id that matches a KXRAIN
# row, but the SQL itself is a generic UPDATE that no-ops when nothing
# matches.  We exercise the swallow directly via the private helper to
# avoid coupling the test to the league-config / market-resolution
# plumbing.
# ---------------------------------------------------------------------------


def test_precip_emos_stamp_failure_is_swallowed_and_counted(monkeypatch):
    fake = _FakeDB(fail_on_execute_for="precip")
    _wire(monkeypatch, fake)

    asyncio.run(
        _pipeline._stamp_precip_emos_observation("KXRAIN-NYC", outcome_yes=1)
    )

    counts = _pipeline.settlement_swallow_counts()
    assert counts["precip_emos_stamp"] == 1
    # Nothing else moved.
    assert counts["postmortem_insert"] == 0
    assert counts["position_close"] == 0


# ---------------------------------------------------------------------------
# Swallow site #5 — position close
# ---------------------------------------------------------------------------


def test_position_close_failure_is_swallowed_and_counted(monkeypatch):
    fake = _FakeDB(
        trade_rows=[_trade(1)], fail_on_execute_for="position_close",
    )
    _wire(monkeypatch, fake)

    _settle()

    counts = _pipeline.settlement_swallow_counts()
    assert counts["position_close"] == 1
    assert fake.position_closes == []
    # Trade + postmortem + online-buffer all still landed.
    assert len(fake.trade_updates) == 1
    assert len(fake.postmortems) == 1
    assert len(fake.online_buffer) == 1


# ---------------------------------------------------------------------------
# Counter helper contract
# ---------------------------------------------------------------------------


def test_settlement_swallow_counts_returns_a_snapshot_not_the_live_dict():
    snap = _pipeline.settlement_swallow_counts()
    snap["postmortem_insert"] = 999
    # Mutating the snapshot must not corrupt the live counters used by
    # the dashboard / health probe.
    assert _pipeline._swallow_counts["postmortem_insert"] == 0


def test_settlement_swallow_counts_has_exactly_the_documented_sites():
    # If a future change adds a new swallow site, the dashboard tile
    # needs to learn about it too — guard the public key set.
    assert set(_pipeline.settlement_swallow_counts().keys()) == {
        "canary_decrement",
        "postmortem_insert",
        "online_learning_buffer_insert",
        "precip_emos_stamp",
        "position_close",
    }
