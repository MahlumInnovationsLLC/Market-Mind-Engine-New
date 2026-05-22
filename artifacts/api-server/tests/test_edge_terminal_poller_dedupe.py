"""Task #847: poller dedupe integration test.

Proves that two scrape cycles returning the same payload result in
exactly one row inserted into ``edge_terminal_signals`` (the
``(vertical, matchup, selection, signal_date)`` unique index drops
the duplicate) and that the executor's ``drain_pending`` is called
on each cycle but sees the freshly-inserted row exactly once.

This is a unit-level integration test: the asyncpg call surface is
stubbed with an in-memory dict that simulates the unique constraint
``ON CONFLICT DO NOTHING`` semantics, so no real Postgres is needed.
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


class _InMemorySignalsDB:
    """Models the ``edge_terminal_signals`` insert path + the unique
    ``(vertical, matchup, selection, signal_date)`` index."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.status_updates: list[tuple[str | None, int]] = []
        self._next_id = 1

    async def fetchval(self, q: str, *args):
        if "INSERT INTO edge_terminal_signals" in q:
            vertical, matchup, selection, signal_date = args[0:4]
            key = (vertical, matchup, selection, signal_date)
            # ON CONFLICT DO NOTHING → second insert returns None.
            if any(
                (r["vertical"], r["matchup"], r["selection"], r["signal_date"]) == key
                for r in self.rows
            ):
                return None
            row_id = self._next_id
            self._next_id += 1
            self.rows.append({
                "id": row_id,
                "vertical": vertical,
                "matchup": matchup,
                "selection": selection,
                "signal_date": signal_date,
                "side": args[10],
                "pick_type": args[11],
            })
            return row_id
        return None

    async def execute(self, q: str, *args):
        if "UPDATE edge_terminal_poller_status" in q:
            self.status_updates.append((args[0], args[1]))


def _patch_poller(monkeypatch, fake: _InMemorySignalsDB):
    from app.edge_terminal import poller
    monkeypatch.setattr(poller.db, "fetchval", fake.fetchval)
    monkeypatch.setattr(poller.db, "execute", fake.execute)


def test_poller_inserts_once_when_same_signal_returned_twice(monkeypatch):
    """Two scrape cycles returning the same row → unique index drops
    the duplicate, dashboard signal-count counts the placement once."""
    from app.edge_terminal import poller

    signal = {
        "vertical": "nba",
        "matchup": "Oklahoma City Thunder @ San Antonio Spurs",
        "selection": "Thunder +2",
        "side": "YES",
        "signal_date": "2026-05-22",
        "pick_type": "spread",
        "avg_odds": -103.0,
        "ev_pct": 27.9,
        "edge_pct": None,
        "model_prob": 0.64,
        "market_price": None,
        "kalshi_ticker": "KXNBA-2026-OKC-SAS-SPREAD-THU2",
        "market_title": None,
        "source_screenshot_url": None,
        "kalshi_market_id": None,
        "raw": {},
    }

    fake = _InMemorySignalsDB()
    _patch_poller(monkeypatch, fake)

    # Stub scraper + executor so we only test the dedupe path.
    drain_calls: list[int] = []

    async def _fake_scrape():
        return [dict(signal)]

    async def _fake_drain():
        drain_calls.append(1)

    monkeypatch.setattr(poller.scraper, "scrape_signals", _fake_scrape)
    monkeypatch.setattr(poller.executor, "drain_pending", _fake_drain)

    # Cycle 1: row inserted (id=1), status updated with count=1.
    _run(poller._one_tick())
    # Cycle 2: same row → INSERT...ON CONFLICT DO NOTHING returns None,
    #          status updated with count=0.
    _run(poller._one_tick())

    # Only one row in the table.
    assert len(fake.rows) == 1
    assert fake.rows[0]["selection"] == "Thunder +2"
    assert fake.rows[0]["side"] == "YES"

    # drain_pending was called on every tick (so a row inserted-but-not-fired
    # on a prior crash still recovers), but only the first tick recorded an
    # inserted-signal count of 1.
    assert drain_calls == [1, 1]
    assert fake.status_updates == [(None, 1), (None, 0)]


def test_poller_inserts_two_distinct_signals_in_one_tick(monkeypatch):
    """Sanity counter-case: two different selections from one scrape
    both land, so the dedupe path doesn't over-fire."""
    from app.edge_terminal import poller

    fake = _InMemorySignalsDB()
    _patch_poller(monkeypatch, fake)

    async def _fake_scrape():
        return [
            {
                "vertical": "nba",
                "matchup": "OKC @ SAS",
                "selection": "Thunder +2",
                "side": "YES",
                "signal_date": "2026-05-22",
                "raw": {},
            },
            {
                "vertical": "nba",
                "matchup": "OKC @ SAS",
                "selection": "Under 218",
                "side": "NO",
                "signal_date": "2026-05-22",
                "raw": {},
            },
        ]

    async def _fake_drain():
        pass

    monkeypatch.setattr(poller.scraper, "scrape_signals", _fake_scrape)
    monkeypatch.setattr(poller.executor, "drain_pending", _fake_drain)

    _run(poller._one_tick())

    assert len(fake.rows) == 2
    assert {r["selection"] for r in fake.rows} == {"Thunder +2", "Under 218"}
    assert fake.status_updates == [(None, 2)]
