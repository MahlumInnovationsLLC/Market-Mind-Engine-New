"""Poller dedupe integration tests (task #847).

Task #844's done criteria called out proving that the
``(vertical, matchup, selection, signal_date)`` unique index on
``edge_terminal_signals`` drops duplicate scrapes without causing
``drain_pending`` to double-fire the same pick.

We simulate the unique index in-process with a dict keyed on the
same tuple so the test stays a pure unit test (no Postgres), then
drive two consecutive ``_one_tick`` calls with identical scraper
output and assert:

  * Only one row lands in the table.
  * ``drain_pending`` fires the pick exactly once across both ticks
    (the second tick sees an empty pending queue).
  * The poller status row reflects ``last_signal_count=1`` on the
    first tick and ``0`` on the second (the conflict dropped it).
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


class _FakeSignalsTable:
    """In-memory stand-in for ``edge_terminal_signals`` with the same
    unique-index behavior the migration installs."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        # Maps (vertical, matchup, selection, signal_date) → row id.
        self.unique: dict[tuple[Any, ...], int] = {}
        self._next_id = 1
        self.status_updates: list[tuple[str | None, int]] = []
        self.insert_attempts = 0
        self.conflicts = 0

    async def fetchval(self, q: str, *args):
        if "INSERT INTO edge_terminal_signals" in q:
            self.insert_attempts += 1
            key = (args[0], args[1], args[2], args[3])
            if key in self.unique:
                # ON CONFLICT (vertical, matchup, selection, signal_date)
                # DO NOTHING RETURNING id → no row returned.
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
                "raw": args[9],
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
            # _mark(sid, status, reason, trades_id)
            sid, status, reason, trades_id = args
            if sid in self.rows:
                self.rows[sid]["status"] = status
                self.rows[sid]["status_reason"] = reason
                self.rows[sid]["trades_id"] = trades_id
        elif "UPDATE edge_terminal_poller_status" in q:
            # (error, signal_count)
            self.status_updates.append((args[0], args[1]))


def _setup_executor_stubs(monkeypatch, fires: list[dict[str, Any]]):
    """Wire the executor so each fire is recorded but never touches
    Kalshi / bankroll / risk gates."""
    from app.edge_terminal import executor

    async def _resolve(sig):
        return "KX-NBA-LAL-BOS"

    async def _bankroll(trading_mode="paper"):
        return 1000.0

    async def _place_order(ctx, **kwargs):
        fires.append({"ctx": ctx, "kwargs": kwargs})
        # Use the signal id so the dedupe assertion stays meaningful.
        return {"trades_id": 1000 + len(fires), "status": "placed"}

    monkeypatch.setattr(executor.market_resolver, "resolve", _resolve)
    monkeypatch.setattr(executor.cycle_module, "_latest_bankroll", _bankroll)
    monkeypatch.setattr(executor.paper_module, "place_order", _place_order)


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

def test_poller_dedupes_repeat_scrape_via_unique_index(monkeypatch):
    """Two consecutive ticks return the same signal; the unique index
    drops the second insert and drain only fires the pick once."""
    from app.edge_terminal import poller

    fake = _FakeSignalsTable()
    monkeypatch.setattr(poller.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(poller.db, "execute", fake.execute)

    # executor uses its own ``db`` reference (same module, but patched
    # per-attribute by monkeypatch — patch the executor's import too).
    from app.edge_terminal import executor
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)

    signal = {
        "vertical": "nba",
        "matchup": "Lakers @ Celtics",
        "selection": "Lakers",
        "side": "YES",
        "signal_date": "2026-05-22",
        "edge_pct": 7.5,
        "model_prob": 0.60,
        "market_price": 0.50,
        "kalshi_ticker": "KX-NBA-LAL-BOS",
        "market_title": "Lakers vs Celtics",
        "raw": {},
    }

    async def _scrape():
        return [dict(signal)]

    monkeypatch.setattr(poller.scraper, "scrape_signals", _scrape)

    fires: list[dict[str, Any]] = []
    _setup_executor_stubs(monkeypatch, fires)

    # Two ticks, same scrape output.
    _run(poller._one_tick())
    _run(poller._one_tick())

    # The unique index dropped the second insert.
    assert fake.insert_attempts == 2
    assert fake.conflicts == 1
    assert len(fake.rows) == 1, "duplicate scrape must not create a second row"

    # drain_pending fired the pick exactly once across both ticks.
    assert len(fires) == 1, (
        f"drain double-fired a deduped signal: {len(fires)} place_order calls"
    )
    assert fires[0]["kwargs"].get("placed_by") == "edge_terminal"

    # Row status was advanced to 'placed' on the first tick so the
    # second tick sees no pending work.
    only_row = next(iter(fake.rows.values()))
    assert only_row["status"] == "placed"

    # Status updates: first tick inserted=1, second tick inserted=0
    # (the conflict dropped it).
    assert fake.status_updates == [(None, 1), (None, 0)]


def test_poller_dedupes_within_a_single_tick(monkeypatch):
    """Two identical tiles in the SAME scrape result must also dedupe —
    the unique index protects against scraper-side duplication too."""
    from app.edge_terminal import poller, executor

    fake = _FakeSignalsTable()
    monkeypatch.setattr(poller.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(poller.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)

    signal = {
        "vertical": "nhl",
        "matchup": "Rangers @ Bruins",
        "selection": "Rangers",
        "side": "YES",
        "signal_date": "2026-05-22",
        "edge_pct": 3.1,
        "model_prob": 0.55,
        "market_price": 0.50,
        "kalshi_ticker": None,
        "market_title": None,
        "raw": {},
    }

    async def _scrape():
        return [dict(signal), dict(signal)]  # duplicated within the tick

    monkeypatch.setattr(poller.scraper, "scrape_signals", _scrape)

    fires: list[dict[str, Any]] = []
    _setup_executor_stubs(monkeypatch, fires)

    _run(poller._one_tick())

    assert fake.insert_attempts == 2
    assert fake.conflicts == 1
    assert len(fake.rows) == 1
    assert len(fires) == 1
    assert fake.status_updates == [(None, 1)]


def test_poller_distinct_signal_dates_are_not_deduped(monkeypatch):
    """Same matchup/selection on a different signal_date is a NEW
    signal — the unique index includes signal_date so both should
    insert and both should fire."""
    from app.edge_terminal import poller, executor

    fake = _FakeSignalsTable()
    monkeypatch.setattr(poller.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(poller.db, "execute", fake.execute)
    monkeypatch.setattr(executor.db, "fetch", fake.fetch)
    monkeypatch.setattr(executor.db, "execute", fake.execute)

    base = {
        "vertical": "nba",
        "matchup": "Lakers @ Celtics",
        "selection": "Lakers",
        "side": "YES",
        "edge_pct": 7.5,
        "model_prob": 0.60,
        "market_price": 0.50,
        "kalshi_ticker": "KX-NBA-LAL-BOS",
        "market_title": None,
        "raw": {},
    }
    day1 = {**base, "signal_date": "2026-05-22"}
    day2 = {**base, "signal_date": "2026-05-23"}

    scrapes = iter([[day1], [day2]])

    async def _scrape():
        return next(scrapes)

    monkeypatch.setattr(poller.scraper, "scrape_signals", _scrape)

    fires: list[dict[str, Any]] = []
    _setup_executor_stubs(monkeypatch, fires)

    _run(poller._one_tick())
    _run(poller._one_tick())

    assert fake.conflicts == 0
    assert len(fake.rows) == 2, "distinct signal_dates must both insert"
    assert len(fires) == 2, "both distinct-day signals must fire"
