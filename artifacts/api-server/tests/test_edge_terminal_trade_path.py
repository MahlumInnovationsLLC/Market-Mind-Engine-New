"""End-to-end Edge Terminal trade-path proof (task #953).

The two operator-mandated gates that decide whether an Edge Terminal
pick becomes a real order are:

  1. the pick must be **signal-badged**, and
  2. its model confidence (per chosen side) must clear the 60% floor,

after which it must **resolve to a Kalshi market** for an order to be
placed.  These tests drive the executor end to end through
``drain_pending`` (the same entry point the poller uses) and assert:

  * an eligible pick — badged, model_prob >= 0.60, resolvable — produces
    exactly one order via the ``paper.place_order`` chokepoint and is
    marked ``placed``;
  * a sub-60% pick is **not** placed (``skipped_low_confidence``);
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

    async def _resolve_match(sig, **kwargs):
        # ``_fire_one`` resolves through ``resolve_match`` first, so this must
        # mirror the ticker the legacy ``resolve`` surface would return: a
        # matched result when a ticker is configured, else the structured
        # no-match that drives the ``skipped_unresolved`` disposition.
        if ticker:
            return market_resolver.ResolveResult(
                ticker, "matched", [], kind="matched",
            )
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

def test_eligible_badged_60pct_pick_produces_order(monkeypatch):
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

def test_sub_60pct_pick_does_not_place(monkeypatch):
    from app.edge_terminal import executor

    fake = _FakeDB([_signal(id=2, model_prob=0.55)])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    counts = _run(executor.drain_pending())

    assert fires == [], "a sub-60% pick must not place an order"
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
# Task #953 (above) proved the badge + 60%-confidence gate end to end.
# These extend that to the executor's *sizing* fallbacks: once a pick is
# eligible, what decides whether it becomes a real order and at what
# quantity.  Same pure-unit harness — DB, resolver, bankroll, kelly cap,
# dedupe probe, trading-mode and ``place_order`` are all stubbed so the
# real ``_fire_one`` sizing logic is the only thing under test.
# ---------------------------------------------------------------------

def test_flat_five_pct_sizes_full_position_when_edge_uncomputable(monkeypatch):
    """No model_prob / edge% / ev% to size from → there is NO min-stake
    single-contract fallback any more (that was the sub-$1 source the
    operator banned).  A badged, resolvable pick is sized at a flat 5% of
    bankroll instead: $1000 bankroll → $50 / $0.50 = 100 contracts, with
    ``kelly`` tagged as the flat fraction (0.05)."""
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

    assert len(fires) == 1, "an eligible pick must place a flat-5% order"
    # 5% of $1000 = $50 notional; $50 / $0.50 per contract = 100 contracts.
    assert fires[0]["ctx"].intended_quantity == 100
    assert fires[0]["ctx"].intended_quantity > 1, "no sub-$1 single-contract fallback"
    assert fires[0]["kwargs"].get("kelly") == executor.FLAT_BANKROLL_FRACTION
    assert counts["placed"] == 1
    assert fake.marks[10][0] == "placed"


def test_flat_five_pct_ignores_kelly_entirely(monkeypatch):
    """Sizing is a flat 5% of bankroll regardless of the Kelly math: even
    if the (now-unused) Kelly helper would size to zero, the pick is still
    sized off the bankroll fraction and placed at full size — there is no
    Kelly-driven down-size and no single-contract fallback.  Pins this so a
    regression that reintroduces Kelly/min-stake sizing is caught."""
    from app.edge_terminal import executor

    fake = _FakeDB([_signal(id=11, model_prob=0.70)])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    # Even if the legacy Kelly helper collapses to zero, sizing no longer
    # consults it — the flat 5% fraction is used unconditionally.
    def _zero_kelly(**kwargs):
        return 0, 0.0

    monkeypatch.setattr(executor, "_kelly_quantity", _zero_kelly)

    counts = _run(executor.drain_pending())

    assert len(fires) == 1, "flat-5% sizing must place regardless of Kelly"
    # 5% of $1000 = $50 / $0.50 = 100 contracts.
    assert fires[0]["ctx"].intended_quantity == 100
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


def test_non_finite_bankroll_skips_cleanly(monkeypatch):
    """A non-finite bankroll (NaN) must not crash the sizing math and must
    never place an order: the flat-5% helper treats it as no usable
    bankroll and the pick is recorded as ``skipped_zero_kelly`` with the
    no-bankroll reason.  Pins the NaN guard so a degenerate bankroll can't
    raise ``ValueError: cannot convert float NaN to integer`` and mask the
    skip as a generic ``failed``."""
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
    assert reason == "no bankroll available for sizing"


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
    """``model_prob`` is the win-probability of the pick itself, so an
    "Under"/"No" tile's number is already the NO side's confidence and is
    NOT flipped.  A 0.55 model_prob NO pick is a 55% pick — below the 60%
    floor — so it's rejected as ``skipped_low_confidence``."""
    from app.edge_terminal import executor

    fake = _FakeDB([_signal(id=15, side="NO", model_prob=0.55)])
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
    assert "55%" in reason  # the pick's own win-probability, not flipped


def test_no_side_low_model_prob_is_rejected_not_flipped(monkeypatch):
    """Regression: a 0.30 model_prob NO pick is a 30%-win pick and MUST be
    rejected.  The old code flipped it to 0.70 and placed a real (actually
    -EV) order — the dangerous mis-trade this fix removes."""
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

    assert fires == [], "a 30%-win NO pick must NOT place an order"
    assert counts["placed"] == 0
    status, reason, _ = fake.marks[16]
    assert status == "skipped_low_confidence"
    assert "30%" in reason


def test_no_side_eligible_pick_sizes_and_places(monkeypatch):
    """An eligible NO pick (0.72 model_prob, the Under's own win-prob) clears
    the 60% floor and sizes a real Kelly position (>> the min-stake 1
    contract) against the NO price ``1 - market_price``."""
    from app.edge_terminal import executor

    fake = _FakeDB([
        _signal(id=17, side="NO", model_prob=0.72, market_price=0.50),
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
        "NO-side Kelly must size on the pick's own win-prob, not min-stake"
    )
    assert counts["placed"] == 1
    assert fake.marks[17][0] == "placed"


def test_no_side_submits_at_no_price_not_yes_quote(monkeypatch):
    """A NO order's submitted price must be in NO coordinates
    (``1 - market_price``).  The lifecycle stamps ``limit_price_cents``
    directly as ``no_price``, so submitting the YES quote (0.62) instead of
    the NO price (0.38) would massively overpay for the NO contract."""
    from app.edge_terminal import executor

    fake = _FakeDB([
        _signal(id=18, side="NO", model_prob=0.72, market_price=0.62),
    ])
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetchval", fake.fetchval)

    fires: list[dict[str, Any]] = []
    _wire(monkeypatch, fires, ticker="KX-NBA-LAL-BOS")

    counts = _run(executor.drain_pending())

    assert counts["placed"] == 1
    ctx = fires[0]["ctx"]
    assert ctx.side == "NO"
    assert abs(ctx.current_price - 0.38) < 1e-9, (
        "NO order must price off 1 - market_price, not the YES quote"
    )
    assert abs(fires[0]["kwargs"]["cand"]["kalshi_price"] - 0.38) < 1e-9
    # Notional is in NO coordinates too (qty * NO price).
    assert abs(
        ctx.intended_notional_usd - ctx.intended_quantity * 0.38
    ) < 1e-9


def test_classify_exception_settled_market_404_is_clean_skip():
    """A finished game's market answers Kalshi 404 ``market_not_found``;
    re-queued picks for fixtures that already ended must skip cleanly
    rather than surface a red ``failed`` "Kalshi rejected order"."""
    from app.edge_terminal import executor

    cls, reason = executor._classify_exception(
        RuntimeError(
            "Kalshi rejected order: Kalshi API error 404: "
            "{'error': {'code': 'market_not_found'}}"
        )
    )
    assert cls == "skipped_gate"
    low = reason.lower()
    assert "not found" in low
    assert "settled" in low or "closed" in low


def test_classify_exception_unknown_error_still_fails():
    from app.edge_terminal import executor

    cls, _ = executor._classify_exception(RuntimeError("kaboom unexpected"))
    assert cls is None
