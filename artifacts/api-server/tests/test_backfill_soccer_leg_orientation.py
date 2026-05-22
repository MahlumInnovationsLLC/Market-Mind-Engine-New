"""Unit tests for ``scripts/backfill_soccer_leg_orientation.py``.

The script reads ``predictions`` rows whose ``metadata`` blob is
missing a ``leg_orientation`` label and writes the parsed leg back via
``jsonb_set``.  Tests stub ``app.db.fetch`` / ``app.db.execute`` with
an in-memory state machine so the parsing, idempotency, and dry-run
behaviour can be exercised without Postgres.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_soccer_leg_orientation.py"
_spec = importlib.util.spec_from_file_location("backfill_soccer_leg_orientation", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["backfill_soccer_leg_orientation"] = backfill
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
    rows: list[dict[str, Any]] = []
    updates: list[tuple[str, int]] = []

    async def fake_fetch(query: str, *args):
        series_filter = set(args[0])
        last_id = args[1]
        limit = args[2]
        out = []
        for r in rows:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            if meta.get("kalshi_series") not in series_filter:
                continue
            leg = (meta.get("leg_orientation") or "").upper()
            if leg in ("HOME", "DRAW", "AWAY"):
                continue
            if r["settled_at"] is not None:
                continue
            if r["kalshi_market_id"] is None:
                continue
            if r["id"] <= last_id:
                continue
            out.append(r)
        out.sort(key=lambda r: r["id"])
        return out[:limit]

    async def fake_execute(query: str, *args):
        leg, row_id = args
        updates.append((leg, row_id))
        for r in rows:
            if r["id"] == row_id:
                raw = r.get("metadata") or {}
                if isinstance(raw, str):
                    meta = json.loads(raw)
                else:
                    meta = dict(raw)
                meta["leg_orientation"] = leg
                r["metadata"] = meta

    monkeypatch.setattr(backfill.db, "fetch", fake_fetch)
    monkeypatch.setattr(backfill.db, "execute", fake_execute)
    return {"rows": rows, "updates": updates}


def _row(
    rid: int,
    *,
    title: str,
    home: str,
    away: str,
    series: str = "KXEPLGAME",
    settled: bool = False,
    leg: str | None = None,
    market_id: str | None = "MKT",
) -> dict[str, Any]:
    meta: dict[str, Any] = {"kalshi_series": series, "title": title}
    if leg is not None:
        meta["leg_orientation"] = leg
    return {
        "id": rid,
        "kalshi_market_id": f"{market_id}-{rid}" if market_id else None,
        "home_team": home,
        "away_team": away,
        "metadata": meta,
        "settled_at": "2026-01-01" if settled else None,
    }


def test_backfills_home_draw_and_away_legs(fake_db):
    fake_db["rows"].extend([
        _row(1, title="Will Arsenal beat Chelsea?", home="Arsenal", away="Chelsea"),
        _row(2, title="Arsenal vs Chelsea ends in a draw", home="Arsenal", away="Chelsea"),
        _row(3, title="Will Chelsea win?", home="Arsenal", away="Chelsea"),
    ])
    stats = _run(backfill.run(batch_size=10, dry_run=False))
    assert stats == {"scanned": 3, "updated": 3, "skipped": 0}
    legs = {row_id: leg for leg, row_id in fake_db["updates"]}
    assert legs == {1: "HOME", 2: "DRAW", 3: "AWAY"}


def test_skips_unparseable_titles(fake_db):
    fake_db["rows"].append(
        _row(7, title="Top scorer of the match", home="Arsenal", away="Chelsea"),
    )
    stats = _run(backfill.run(batch_size=10, dry_run=False))
    assert stats == {"scanned": 1, "updated": 0, "skipped": 1}
    assert fake_db["updates"] == []


def test_idempotent_skips_already_labelled_rows(fake_db):
    fake_db["rows"].extend([
        _row(1, title="Will Arsenal beat Chelsea?", home="Arsenal", away="Chelsea", leg="HOME"),
        _row(2, title="Will Chelsea win?", home="Arsenal", away="Chelsea"),
    ])
    stats = _run(backfill.run(batch_size=10, dry_run=False))
    assert stats == {"scanned": 1, "updated": 1, "skipped": 0}
    assert fake_db["updates"] == [("AWAY", 2)]
    # Second invocation finds nothing new.
    stats2 = _run(backfill.run(batch_size=10, dry_run=False))
    assert stats2 == {"scanned": 0, "updated": 0, "skipped": 0}


def test_ignores_non_soccer_series_and_settled_rows(fake_db):
    fake_db["rows"].extend([
        _row(1, title="Will Arsenal beat Chelsea?", home="Arsenal", away="Chelsea",
             series="KXNFLGAME"),
        _row(2, title="Will Arsenal beat Chelsea?", home="Arsenal", away="Chelsea",
             settled=True),
        _row(3, title="Will Arsenal beat Chelsea?", home="Arsenal", away="Chelsea"),
    ])
    stats = _run(backfill.run(batch_size=10, dry_run=False))
    assert stats == {"scanned": 1, "updated": 1, "skipped": 0}
    assert fake_db["updates"] == [("HOME", 3)]


def test_dry_run_makes_progress_without_writes(fake_db):
    fake_db["rows"].extend([
        _row(1, title="Will Arsenal beat Chelsea?", home="Arsenal", away="Chelsea"),
        _row(2, title="Will Chelsea win?", home="Arsenal", away="Chelsea"),
        _row(3, title="Arsenal vs Chelsea ends in a draw", home="Arsenal", away="Chelsea"),
    ])
    stats = _run(backfill.run(batch_size=1, dry_run=True))
    assert stats == {"scanned": 3, "updated": 3, "skipped": 0}
    assert fake_db["updates"] == []


def test_handles_json_string_metadata(fake_db):
    row = _row(1, title="Will Arsenal beat Chelsea?", home="Arsenal", away="Chelsea")
    row["metadata"] = json.dumps(row["metadata"])
    fake_db["rows"].append(row)
    stats = _run(backfill.run(batch_size=10, dry_run=False))
    assert stats == {"scanned": 1, "updated": 1, "skipped": 0}
    assert fake_db["updates"] == [("HOME", 1)]
