"""Re-arm-on-conflict tests for the Edge Terminal poller.

Sports markets list on Kalshi only a few hours before first pitch, but
the Edge Terminal board badges the pick much earlier.  The first drain of
the day therefore resolves a pre-game pick to "no live market" and marks
it ``skipped_unresolved``.  Under the original ``ON CONFLICT DO NOTHING``
that disposition was frozen for the whole UTC day (the dedupe index blocks
re-insert), so the pick never traded even once its market opened — the root
cause of "only BTC trades, no MLB/sports".

The poller's INSERT now re-arms (``DO UPDATE SET status='pending'``) a
conflicting row *only* when its current status is exactly
``skipped_unresolved`` — the one transient market-availability stand-down
(resolver found no live market yet).  These tests prove:

  * A pick first stood down as ``skipped_unresolved`` is re-armed on the
    next scrape and fires once its market resolves.
  * A ``placed`` pick is NEVER re-armed (the safety boundary — re-arming a
    held position would double-bet).
  * A ``skipped_gate`` pick is NEVER re-armed (it is "overwhelmingly a
    settled/closed game", so retrying would re-fire finished fixtures, not
    fix anything).
"""
from __future__ import annotations

import asyncio
from typing import Any


# Statuses the poller's ON CONFLICT DO UPDATE ... WHERE clause re-arms.
# Kept in lockstep with the SQL in ``poller._insert_signal`` — exactly one
# transient market-availability status, nothing else.
_REARM_STATUSES = {"skipped_unresolved"}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class _FakeSignalsTable:
    """In-memory ``edge_terminal_signals`` that mirrors the unique index
    AND the conditional re-arm of ``ON CONFLICT DO UPDATE``."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.unique: dict[tuple[Any, ...], int] = {}
        self._next_id = 1
        self.status_updates: list[tuple[str | None, int]] = []
        self.insert_attempts = 0
        self.conflicts = 0
        self.rearms = 0

    async def fetchval(self, q: str, *args):
        if "INSERT INTO edge_terminal_signals" in q:
            self.insert_attempts += 1
            key = (args[0], args[1], args[2], args[3])
            if key in self.unique:
                rid = self.unique[key]
                existing = self.rows[rid]
                # ON CONFLICT DO UPDATE ... WHERE status IN (transient skips)
                if existing["status"] in _REARM_STATUSES:
                    existing["status"] = "pending"
                    existing["status_reason"] = None
                    existing["edge_pct"] = args[4]
                    existing["model_prob"] = args[5]
                    existing["market_price"] = args[6]
                    existing["kalshi_ticker"] = args[7]
                    existing["market_title"] = args[8]
                    existing["side"] = args[10]
                    self.rearms += 1
                    return rid
                # Frozen disposition (placed / non-transient) → WHERE false →
                # no row returned, exactly like DO NOTHING.
                self.conflicts += 1
                return None
            rid = self._next_id
            self._next_id += 1
            self.unique[key] = rid
            self.rows[rid] = {
                "id": rid,
                "vertical": args[0],
                "matchup": args[1],
                "selection": args[2],
                "signal_date": args[3],
                "edge_pct": args[4],
                "model_prob": args[5],
                "market_price": args[6],
                "kalshi_ticker": args[7],
                "market_title": args[8],
                "side": args[10],
                "status": "pending",
                "status_reason": None,
                "trades_id": None,
            }
            return rid
        return None

    async def fetch(self, q: str, *args):
        if "FROM edge_terminal_signals" in q and "status = 'pending'" in q:
            return [
                {
                    "id": r["id"],
                    "vertical": r["vertical"],
                    "matchup": r["matchup"],
                    "selection": r["selection"],
                    "signal_date": r["signal_date"],
                    "edge_pct": r["edge_pct"],
                    "model_prob": r["model_prob"],
                    "market_price": r["market_price"],
                    "kalshi_ticker": r["kalshi_ticker"],
                    "market_title": r["market_title"],
                    "side": r["side"],
                }
                for r in self.rows.values()
                if r["status"] == "pending"
            ]
        return []

    async def execute(self, q: str, *args):
        if "UPDATE edge_terminal_signals" in q:
            sid, status, reason, trades_id = args
            if sid in self.rows:
                self.rows[sid]["status"] = status
                self.rows[sid]["status_reason"] = reason
                self.rows[sid]["trades_id"] = trades_id
        elif "UPDATE edge_terminal_poller_status" in q:
            self.status_updates.append((args[0], args[1]))


def _setup_executor_stubs(monkeypatch, fires, resolver):
    """Wire the executor so a fire is recorded but never touches Kalshi.

    ``resolver`` is an async callable ``(sig) -> ResolveResult`` so a test
    can flip a pick from unresolved → resolvable across ticks.
    """
    from app.edge_terminal import executor
    from app.risk import trading_mode as trading_mode_module

    async def _bankroll(trading_mode="paper"):
        return 1000.0

    async def _kelly_cap():
        return 0.25

    async def _not_already_placed(ticker, side, trading_mode):
        return False

    async def _trading_mode(env_default=None):
        return "paper"

    async def _place_order(ctx, **kwargs):
        fires.append({"ctx": ctx, "kwargs": kwargs})
        return {"trades_id": 1000 + len(fires), "status": "placed"}

    monkeypatch.setattr(executor.market_resolver, "resolve_match", resolver)
    monkeypatch.setattr(executor.cycle_module, "_latest_bankroll", _bankroll)
    monkeypatch.setattr(executor, "_resolve_kelly_fraction_cap", _kelly_cap)
    monkeypatch.setattr(executor, "_already_placed", _not_already_placed)
    monkeypatch.setattr(
        trading_mode_module, "resolve_trading_mode", _trading_mode
    )
    monkeypatch.setattr(executor.paper_module, "place_order", _place_order)


def _signal() -> dict[str, Any]:
    return {
        "vertical": "mlb",
        "matchup": "Royals @ Reds",
        "selection": "Royals +1.5",
        "side": "YES",
        "signal_date": "2026-06-01",
        "edge_pct": 13.3,
        "model_prob": 0.70,
        "market_price": 0.50,
        "kalshi_ticker": None,
        "market_title": None,
        "raw": {},
    }


def test_unresolved_pregame_pick_is_rearmed_and_fires_when_market_lists(monkeypatch):
    """A pick stood down as ``skipped_unresolved`` (market not listed yet)
    is re-armed on the next scrape and fires once its market resolves."""
    from app.edge_terminal import poller, executor
    from app.edge_terminal.market_resolver import ResolveResult

    fake = _FakeSignalsTable()
    monkeypatch.setattr(poller.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(poller.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)

    async def _scrape():
        return [_signal()]

    monkeypatch.setattr(poller.scraper, "scrape_signals", _scrape)

    # Tick 1: market not listed yet → resolver returns no ticker.
    # Tick 2: market is live → resolver returns a real ticker.
    calls = {"n": 0}

    async def _resolver(sig, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ResolveResult(None, "no live market yet", [], kind="unresolved")
        return ResolveResult(
            "KXMLBSPREAD-26JUN011910KCCIN-CIN2", "matched", [], kind="matched"
        )

    fires: list[dict[str, Any]] = []
    _setup_executor_stubs(monkeypatch, fires, _resolver)

    _run(poller._one_tick())
    # First tick: inserted, drained, stood down as skipped_unresolved.
    only = next(iter(fake.rows.values()))
    assert only["status"] == "skipped_unresolved"
    assert len(fires) == 0

    _run(poller._one_tick())
    # Second tick: conflict re-armed the row, drain fired it once.
    assert fake.rearms == 1, "transient skip must be re-armed on re-scrape"
    assert len(fake.rows) == 1, "re-arm must not create a duplicate row"
    assert len(fires) == 1, "re-armed pick must fire once its market lists"
    assert only["status"] == "placed"


def test_placed_pick_is_never_rearmed(monkeypatch):
    """The safety boundary: a ``placed`` pick re-scraped on a later tick is
    NOT re-armed — re-arming a held position would double-bet real money."""
    from app.edge_terminal import poller, executor
    from app.edge_terminal.market_resolver import ResolveResult

    fake = _FakeSignalsTable()
    monkeypatch.setattr(poller.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(poller.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)

    async def _scrape():
        return [_signal()]

    monkeypatch.setattr(poller.scraper, "scrape_signals", _scrape)

    async def _resolver(sig, **kwargs):
        return ResolveResult(
            "KXMLBSPREAD-26JUN011910KCCIN-CIN2", "matched", [], kind="matched"
        )

    fires: list[dict[str, Any]] = []
    _setup_executor_stubs(monkeypatch, fires, _resolver)

    _run(poller._one_tick())  # places
    _run(poller._one_tick())  # re-scrape — must NOT re-fire

    assert fake.rearms == 0, "a placed pick must never be re-armed"
    assert fake.conflicts == 1, "the placed-row conflict behaves like DO NOTHING"
    assert len(fires) == 1, "placed pick must fire exactly once, never double-bet"
    only = next(iter(fake.rows.values()))
    assert only["status"] == "placed"


def test_skipped_gate_pick_is_never_rearmed(monkeypatch):
    """``skipped_gate`` (order-time ``market_not_found`` — overwhelmingly a
    settled/closed game) is NOT a transient market-availability skip, so a
    re-scrape must not re-arm it.  Only ``skipped_unresolved`` re-arms."""
    from app.edge_terminal import poller, executor
    from app.edge_terminal.market_resolver import ResolveResult

    fake = _FakeSignalsTable()
    monkeypatch.setattr(poller.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(poller.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)

    # Pre-seed a row already disposed as skipped_gate for this exact key.
    # The poller coerces the ISO ``signal_date`` to a ``date`` before the
    # INSERT binds it, so the unique key uses the ``date`` object, not the
    # string — mirror that here or the conflict never triggers.
    from datetime import date

    sig = _signal()
    sig_date = date.fromisoformat(sig["signal_date"])
    key = (sig["vertical"], sig["matchup"], sig["selection"], sig_date)
    fake.rows[1] = {
        "id": 1,
        "vertical": sig["vertical"],
        "matchup": sig["matchup"],
        "selection": sig["selection"],
        "signal_date": sig_date,
        "edge_pct": sig["edge_pct"],
        "model_prob": sig["model_prob"],
        "market_price": sig["market_price"],
        "kalshi_ticker": "KXMLBSPREAD-26JUN011910KCCIN-CIN2",
        "market_title": None,
        "side": sig["side"],
        "status": "skipped_gate",
        "status_reason": "Kalshi market not found — settled, closed, or "
        "ticker no longer open",
        "trades_id": None,
    }
    fake.unique[key] = 1
    fake._next_id = 2

    async def _scrape():
        return [_signal()]

    monkeypatch.setattr(poller.scraper, "scrape_signals", _scrape)

    async def _resolver(sig, **kwargs):
        return ResolveResult(
            "KXMLBSPREAD-26JUN011910KCCIN-CIN2", "matched", [], kind="matched"
        )

    fires: list[dict[str, Any]] = []
    _setup_executor_stubs(monkeypatch, fires, _resolver)

    _run(poller._one_tick())  # re-scrape of a skipped_gate row

    assert fake.rearms == 0, "skipped_gate must never be re-armed"
    assert fake.conflicts == 1, "skipped_gate conflict behaves like DO NOTHING"
    assert len(fires) == 0, "a settled-game gate must not re-fire"
    assert fake.rows[1]["status"] == "skipped_gate", "status stays frozen"
