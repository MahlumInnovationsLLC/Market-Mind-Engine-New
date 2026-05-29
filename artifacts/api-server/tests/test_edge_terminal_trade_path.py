"""End-to-end Edge Terminal trade-path proof (task #953).

The two operator-mandated gates that decide whether an Edge Terminal
pick becomes a real order are:

  1. the pick must be **signal-badged**, and
  2. its model confidence (per chosen side) must clear the 65% floor,

after which it must **resolve to a Kalshi market** for an order to be
placed.  These tests drive the executor end to end through
``drain_pending`` (the same entry point the poller uses) and assert:

  * an eligible pick — badged, model_prob >= 0.65, resolvable — produces
    exactly one order via the ``paper.place_order`` chokepoint and is
    marked ``placed``;
  * a sub-65% pick is **not** placed (``skipped_low_confidence``);
  * a non-badged pick is **not** placed (``skipped_not_badged``);
  * an unresolvable pick is **not** placed (``skipped_unresolved``).

The DB, Kalshi resolver, bankroll lookup, trading-mode resolution, the
Kelly-fraction cap, the already-placed dedupe probe, and
``paper.place_order`` are all stubbed so this stays a pure unit test
(no Postgres, no network) that exercises the real ``_fire_one`` /
``drain_pending`` decision logic.
"""
from __future__ import annotations

import asyncio
from typing import Any


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# Columns drain_pending's SELECT projects, so a fake row looks exactly
# like the real asyncpg record the executor consumes.
_SIGNAL_COLUMNS = (
    "id", "vertical", "matchup", "selection", "signal_date",
    "edge_pct", "model_prob", "market_price",
    "kalshi_ticker", "market_title", "side",
    "pick_type", "avg_odds", "ev_pct",
    "source_screenshot_url", "kalshi_market_id",
    "signal_badged",
)


def _signal(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "vertical": "nba",
        "matchup": "Lakers @ Celtics",
        "selection": "Lakers",
        "signal_date": "2026-05-29",
        "edge_pct": 7.5,
        "model_prob": 0.70,
        "market_price": 0.50,
        "kalshi_ticker": "KX-NBA-LAL-BOS",
        "market_title": "Lakers vs Celtics",
        "side": "YES",
        "pick_type": None,
        "avg_odds": None,
        "ev_pct": None,
        "source_screenshot_url": None,
        "kalshi_market_id": None,
        "signal_badged": True,
    }
    base.update(overrides)
    return {k: base[k] for k in _SIGNAL_COLUMNS}


class _FakeDB:
    """In-memory stand-in for the executor's ``db`` module.

    Serves the pending-signal SELECT, records ``_mark`` UPDATEs (so the
    terminal disposition is assertable), swallows the best-effort
    ``_persist_decision`` UPDATE, and reports "no open position" for the
    already-placed probe (overridden via the stubbed helper anyway).
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        # sid -> (status, reason, trades_id) from _mark.
        self.marks: dict[int, tuple[str, str | None, int | None]] = {}

    async def fetch(self, q: str, *args):
        if "FROM edge_terminal_signals" in q and "status = 'pending'" in q:
            return [dict(r) for r in self._rows]
        return []

    async def execute(self, q: str, *args):
        if "UPDATE edge_terminal_signals" in q and "status_reason" in q:
            # _mark(sid, status, reason, trades_id)
            sid, status, reason, trades_id = args
            self.marks[int(sid)] = (status, reason, trades_id)
        # _persist_decision's UPDATE (different arity) is best-effort and
        # intentionally a no-op here.

    async def fetchval(self, q: str, *args):
        return None


def _wire(monkeypatch, fires: list[dict[str, Any]], *, ticker: str | None):
    """Stub every collaborator after the gates so ``place_order`` is the
    sole chokepoint and the test never touches Postgres / Kalshi."""
    from app.edge_terminal import executor, market_resolver
    from app.risk import trading_mode as trading_mode_module

    async def _resolve(sig):
        return ticker

    async def _resolve_match(sig):
        return market_resolver.ResolveResult(
            None, "no matching kalshi market", [], kind="no_match"
        )

    async def _bankroll(trading_mode="paper"):
        return 1000.0

    async def _kelly_cap():
        return 0.25

    async def _not_already_placed(t, side, trading_mode):
        return False

    async def _trading_mode(env_default=None):
        return "paper"

    async def _place_order(ctx, **kwargs):
        fires.append({"ctx": ctx, "kwargs": kwargs})
        return {"trades_id": 9000 + len(fires), "status": "placed"}

    monkeypatch.setattr(executor.market_resolver, "resolve", _resolve)
    monkeypatch.setattr(executor.market_resolver, "resolve_match", _resolve_match)
    monkeypatch.setattr(executor.cycle_module, "_latest_bankroll", _bankroll)
    monkeypatch.setattr(executor, "_resolve_kelly_fraction_cap", _kelly_cap)
    monkeypatch.setattr(executor, "_already_placed", _not_already_placed)
    monkeypatch.setattr(
        trading_mode_module, "resolve_trading_mode", _trading_mode
    )
    monkeypatch.setattr(executor.paper_module, "place_order", _place_order)


# ---------------------------------------------------------------------
# Positive case — eligible pick produces an order
# ---------------------------------------------------------------------

def test_eligible_badged_65pct_pick_produces_order(monkeypatch):
    from app.edge_terminal import executor

    fake = _FakeDB([_signal(id=1)])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    counts = _run(executor.drain_pending())

    # Exactly one order through the place_order chokepoint, tagged as
    # an Edge Terminal fire on the resolved ticker.
    assert len(fires) == 1, "an eligible pick must produce exactly one order"
    assert fires[0]["kwargs"].get("placed_by") == "edge_terminal"
    assert fires[0]["ctx"].market_id == "KX-NBA-LAL-BOS"

    assert counts["placed"] == 1
    assert fake.marks[1][0] == "placed"
    assert fake.marks[1][2] == 9001  # trades_id threaded back from place_order


# ---------------------------------------------------------------------
# Negative cases — ineligible picks must NOT place an order
# ---------------------------------------------------------------------

def test_sub_65pct_pick_does_not_place(monkeypatch):
    from app.edge_terminal import executor

    fake = _FakeDB([_signal(id=2, model_prob=0.60)])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    counts = _run(executor.drain_pending())

    assert fires == [], "a sub-65% pick must not place an order"
    assert counts["placed"] == 0
    assert fake.marks[2][0] == "skipped_low_confidence"


def test_non_badged_pick_does_not_place(monkeypatch):
    from app.edge_terminal import executor

    fake = _FakeDB([_signal(id=3, signal_badged=False)])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    counts = _run(executor.drain_pending())

    assert fires == [], "a non-badged pick must not place an order"
    assert counts["placed"] == 0
    assert fake.marks[3][0] == "skipped_not_badged"


def test_unresolved_pick_does_not_place(monkeypatch):
    from app.edge_terminal import executor

    # Eligible on badge + confidence, but the resolver maps it to no
    # Kalshi market — so no order can be placed.
    fake = _FakeDB([_signal(id=4, kalshi_ticker=None)])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker=None)

    counts = _run(executor.drain_pending())

    assert fires == [], "an unresolved pick must not place an order"
    assert counts["placed"] == 0
    assert fake.marks[4][0] == "skipped_unresolved"


# ---------------------------------------------------------------------
# Bet-sizing fallbacks BELOW the gate (task #955)
#
# Task #953 (above) proved the badge + 65%-confidence gate end to end.
# These extend that to the executor's *sizing* fallbacks: once a pick is
# eligible, what decides whether it becomes a real order and at what
# quantity.  Same pure-unit harness — DB, resolver, bankroll, kelly cap,
# dedupe probe, trading-mode and ``place_order`` are all stubbed so the
# real ``_fire_one`` sizing logic is the only thing under test.
# ---------------------------------------------------------------------

def test_min_stake_fallback_places_one_contract(monkeypatch):
    """No model_prob / edge% / ev% to size from → Kelly collapses to
    zero, but the pick is badged and resolved to a real ticker, so the
    min-stake fallback honours operator intent with exactly **1**
    contract (``kelly`` tagged 0.0 — signal-driven, not edge-sized)."""
    from app.edge_terminal import executor

    fake = _FakeDB([
        _signal(id=10, model_prob=None, edge_pct=None, ev_pct=None,
                market_price=0.50),
    ])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    counts = _run(executor.drain_pending())

    assert len(fires) == 1, "an unsizable-but-eligible pick must place 1 contract"
    assert fires[0]["ctx"].intended_quantity == 1
    assert fires[0]["kwargs"].get("kelly") == 0.0
    assert counts["placed"] == 1
    assert fake.marks[10][0] == "placed"


def test_min_stake_fallback_shadows_zero_kelly(monkeypatch):
    """Even WITH a model_prob that clears the 65% floor, a Kelly result
    of zero (e.g. a non-positive edge) is shadowed by the min-stake
    fallback: with bankroll > 0 and side_price < 1.0 the pick still
    places exactly 1 contract rather than skipping.  Pins this so a
    regression that silently drops these trades is caught."""
    from app.edge_terminal import executor

    fake = _FakeDB([_signal(id=11, model_prob=0.70)])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    # Force Kelly to size to zero regardless of the (favourable) inputs.
    def _zero_kelly(**kwargs):
        return 0, 0.0

    monkeypatch.setattr(executor, "_kelly_quantity", _zero_kelly)

    counts = _run(executor.drain_pending())

    assert len(fires) == 1, "min-stake fallback must shadow a zero-Kelly result"
    assert fires[0]["ctx"].intended_quantity == 1
    assert counts["placed"] == 1
    assert fake.marks[11][0] == "placed"


def test_zero_kelly_skip_no_bankroll(monkeypatch):
    """No bankroll to size against → the min-stake fallback declines
    (it requires bankroll > 0) and the pick is recorded as
    ``skipped_zero_kelly`` with the granular **no bankroll** reason so
    the operator can tell an empty book apart from a degenerate price."""
    from app.edge_terminal import executor

    fake = _FakeDB([
        _signal(id=12, model_prob=None, edge_pct=None, ev_pct=None),
    ])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    async def _no_bankroll(trading_mode="paper"):
        return 0.0

    monkeypatch.setattr(executor.cycle_module, "_latest_bankroll", _no_bankroll)

    counts = _run(executor.drain_pending())

    assert fires == [], "a zero-bankroll pick must not place an order"
    assert counts["placed"] == 0
    status, reason, _ = fake.marks[12]
    assert status == "skipped_zero_kelly"
    assert reason == "no bankroll available for sizing"


def test_zero_kelly_skip_kelly_sized_to_zero_reason(monkeypatch):
    """The granular **kelly sized to zero contracts** reason is the
    fall-through of the zero-Kelly reason selector.  For every finite
    positive bankroll the min-stake fallback shadows it, so the only
    input that reaches it is a non-finite bankroll (NaN) — which makes
    BOTH the fallback guard (``bankroll > 0``) and the no-bankroll check
    (``bankroll <= 0``) false.  Exercising it pins the if/elif/else
    ordering of the reason selector against a future reshuffle."""
    from app.edge_terminal import executor

    fake = _FakeDB([
        _signal(id=13, model_prob=None, edge_pct=None, ev_pct=None),
    ])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    async def _nan_bankroll(trading_mode="paper"):
        return float("nan")

    monkeypatch.setattr(executor.cycle_module, "_latest_bankroll", _nan_bankroll)

    counts = _run(executor.drain_pending())

    assert fires == [], "a non-finite-bankroll pick must not place an order"
    assert counts["placed"] == 0
    status, reason, _ = fake.marks[13]
    assert status == "skipped_zero_kelly"
    assert reason == "kelly sized to zero contracts"


def test_already_placed_dedupe_skips(monkeypatch):
    """An open position on the same ticker+side+mode → the dedupe probe
    stands the pick down as ``skipped_already_placed`` (honouring the
    existing exposure) instead of double-buying."""
    from app.edge_terminal import executor

    fake = _FakeDB([_signal(id=14, model_prob=0.70)])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    async def _already_true(ticker, side, trading_mode):
        return True

    monkeypatch.setattr(executor, "_already_placed", _already_true)

    counts = _run(executor.drain_pending())

    assert fires == [], "a deduped pick must not place a second order"
    assert counts["placed"] == 0
    assert counts["already_placed"] == 1
    status, reason, _ = fake.marks[14]
    assert status == "skipped_already_placed"
    assert "KX-NBA-LAL-BOS" in reason


def test_no_side_floor_rejects_sub_threshold_model_prob(monkeypatch):
    """NO-side confidence floor uses ``1 - model_prob``: a 0.60
    model_prob on a NO pick is 0.40 from the NO side's perspective —
    below the 65% floor — so it's rejected as ``skipped_low_confidence``
    (buying the wrong side here would be the worst mis-trade)."""
    from app.edge_terminal import executor

    fake = _FakeDB([_signal(id=15, side="NO", model_prob=0.60)])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    counts = _run(executor.drain_pending())

    assert fires == [], "a sub-floor NO pick must not place an order"
    assert counts["placed"] == 0
    status, reason, _ = fake.marks[15]
    assert status == "skipped_low_confidence"
    assert "40%" in reason  # 1 - 0.60, from the NO side's perspective


def test_no_side_sizes_on_one_minus_model_prob(monkeypatch):
    """NO-side sizing uses ``1 - model_prob`` too: a 0.30 model_prob on a
    NO pick is 0.70 from the NO side, clears the floor, and sizes a real
    Kelly position (>> the min-stake 1 contract).  If sizing wrongly used
    model_prob directly (0.30) the NO edge would be negative → min-stake
    1 contract, so a quantity well above 1 proves the side flip."""
    from app.edge_terminal import executor

    fake = _FakeDB([
        _signal(id=16, side="NO", model_prob=0.30, market_price=0.50),
    ])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    counts = _run(executor.drain_pending())

    assert len(fires) == 1, "an eligible NO pick must place an order"
    assert fires[0]["ctx"].side == "NO"
    assert fires[0]["ctx"].intended_quantity > 1, (
        "NO-side Kelly must size on 1 - model_prob, not the min-stake floor"
    )
    assert counts["placed"] == 1
    assert fake.marks[16][0] == "placed"
