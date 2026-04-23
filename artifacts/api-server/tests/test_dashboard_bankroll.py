"""Integration test for `dashboard.get_recent_bankroll_events`.

Spins up an isolated Postgres (same approach as the over-filled-orders
test), materializes the minimum `bankroll_events` schema introduced by
the 0003 migration, and exercises the dashboard helper end-to-end.

Covers:
  * Rows are returned chronologically (oldest first) so the dashboard
    sparkline can iterate without re-sorting.
  * `current_balance_cents` matches the *most recent* row's balance.
  * `last_event_at` is populated from the most recent row.
  * The `exchange` filter excludes other-exchange rows.
  * The `limit` cap is honoured.
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def pg_dsn():
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("no local Postgres binaries on PATH")

    tmp = Path(tempfile.mkdtemp(prefix="axiom_pg_bankroll_"))
    data = tmp / "data"
    sock = tmp / "sock"
    sock.mkdir()
    log = tmp / "pg.log"
    port = _free_port()

    subprocess.check_call(
        [initdb, "-D", str(data), "-A", "trust", "-U", "postgres"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    opts = f"-p {port} -k {sock} -h 127.0.0.1"
    subprocess.check_call(
        [pg_ctl, "-D", str(data), "-l", str(log), "-o", opts, "-w", "start"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)

    try:
        yield dsn
    finally:
        subprocess.call(
            [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(tmp, ignore_errors=True)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bankroll_events (
    id              bigserial   PRIMARY KEY,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    exchange        text        NOT NULL DEFAULT 'kalshi',
    reason          text        NOT NULL,
    delta_cents     bigint      NOT NULL,
    balance_cents   bigint      NOT NULL,
    details         jsonb       NOT NULL DEFAULT '{}'::jsonb
);
"""


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_recent_bankroll_events_returns_chronological_window(
    pg_dsn, monkeypatch
):
    from app import db as app_db
    from app import dashboard

    async def go():
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)

        async def _fetchval(q, *a):
            async with pool.acquire() as c:
                return await c.fetchval(q, *a)

        async def _fetch(q, *a):
            async with pool.acquire() as c:
                return await c.fetch(q, *a)

        async def _execute(q, *a):
            async with pool.acquire() as c:
                return await c.execute(q, *a)

        monkeypatch.setattr(app_db, "fetchval", _fetchval)
        monkeypatch.setattr(app_db, "fetch", _fetch)
        monkeypatch.setattr(app_db, "execute", _execute)

        await _execute(_SCHEMA_SQL)

        # 3 kalshi rows + 1 polymarket row that must be excluded.
        await _execute(
            "INSERT INTO bankroll_events "
            "(exchange, reason, delta_cents, balance_cents, occurred_at) "
            "VALUES "
            "('kalshi', 'exchange_sync', 0, 100000, now() - interval '3 hours'),"
            "('kalshi', 'fill', -2500, 97500, now() - interval '2 hours'),"
            "('kalshi', 'exchange_sync', 1500, 99000, now() - interval '1 hour'),"
            "('polymarket', 'exchange_sync', 5000, 50000, now() - interval '90 minutes')"
        )

        out = await dashboard.get_recent_bankroll_events(limit=200)
        out_limited = await dashboard.get_recent_bankroll_events(limit=2)
        await pool.close()
        return out, out_limited

    out, out_limited = _run(go())

    assert out["exchange"] == "kalshi"
    items = out["items"]
    # Polymarket row excluded.
    assert len(items) == 3
    # Chronological — oldest first.
    balances = [r["balance_cents"] for r in items]
    assert balances == [100000, 97500, 99000]
    # Current balance + last_event_at come from the *most recent* row.
    assert out["current_balance_cents"] == 99000
    assert out["last_event_at"] is not None
    assert items[-1]["balance_cents"] == 99000
    assert items[-1]["reason"] == "exchange_sync"

    # Limit caps row count but still picks the most recent rows and
    # returns them chronologically.
    items_limited = out_limited["items"]
    assert len(items_limited) == 2
    balances_limited = [r["balance_cents"] for r in items_limited]
    assert balances_limited == [97500, 99000]
    assert out_limited["current_balance_cents"] == 99000


def test_recent_bankroll_events_empty_table_returns_clean_shape(
    pg_dsn, monkeypatch
):
    from app import db as app_db
    from app import dashboard

    async def go():
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)

        async def _fetch(q, *a):
            async with pool.acquire() as c:
                return await c.fetch(q, *a)

        async def _execute(q, *a):
            async with pool.acquire() as c:
                return await c.execute(q, *a)

        monkeypatch.setattr(app_db, "fetch", _fetch)
        monkeypatch.setattr(app_db, "execute", _execute)

        await _execute(_SCHEMA_SQL)
        # Wipe any rows inserted by sibling tests sharing the module-scoped
        # Postgres fixture so this case truly exercises the empty path.
        await _execute("DELETE FROM bankroll_events")

        out = await dashboard.get_recent_bankroll_events(limit=10)
        await pool.close()
        return out

    out = _run(go())
    assert out["exchange"] == "kalshi"
    assert out["items"] == []
    assert out["current_balance_cents"] is None
    assert out["last_event_at"] is None
