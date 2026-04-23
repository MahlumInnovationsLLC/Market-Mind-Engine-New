"""Integration test for `dashboard.get_over_filled_orders_today`.

Spins up an isolated Postgres (same approach as `test_position_rollup_sql.py`),
materializes the minimum `positions` + `order_fills` schema touched by
the 0002 migration, and exercises the dashboard helper end-to-end.

Covers:
  * Over-filled rows from today are surfaced with the correct
    requested / filled / discarded counts (filled - requested).
  * Rows with `over_filled=False` are excluded.
  * Rows whose most-recent fill is from yesterday are excluded
    (the badge is intentionally scoped to the current UTC day).
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

    tmp = Path(tempfile.mkdtemp(prefix="axiom_pg_ovf_"))
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
CREATE TABLE IF NOT EXISTS positions (
    id            bigserial PRIMARY KEY,
    market_id     text,
    side          text,
    vertical      text,
    exchange      text,
    quantity      integer,
    avg_price     double precision,
    status        text,
    order_id      text,
    filled_count  integer,
    over_filled   boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX IF NOT EXISTS positions_order_id_uidx
    ON positions (order_id);

CREATE TABLE IF NOT EXISTS order_fills (
    order_id    text NOT NULL,
    fill_id     text NOT NULL,
    count       integer NOT NULL,
    price_cents integer NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id, fill_id)
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


def test_over_filled_orders_today_surfaces_recent_overfills(pg_dsn, monkeypatch):
    from app import db as app_db
    from app import dashboard
    from app.kalshi import market_titles as _market_titles

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
        # Stub title resolver so the test doesn't hit Kalshi.
        async def _resolve(_ids):
            return {"MKT-OVF": "Over-filled market"}
        monkeypatch.setattr(
            dashboard, "resolve_market_titles", _resolve
        )

        await _execute(_SCHEMA_SQL)

        # Over-filled today: requested=10, total received=12, discarded=2.
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, exchange, "
            "quantity, avg_price, status, order_id, filled_count, over_filled) "
            "VALUES ($1, 'yes', 'sports', 'kalshi', 10, 0.55, 'open', $2, 10, TRUE)",
            "MKT-OVF", "oid-overfilled-today",
        )
        await _execute(
            "INSERT INTO order_fills (order_id, fill_id, count, price_cents) "
            "VALUES ($1, 'f1', 7, 50), ($1, 'f2', 5, 60)",
            "oid-overfilled-today",
        )

        # Normal fill (over_filled=False): must NOT appear.
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, exchange, "
            "quantity, avg_price, status, order_id, filled_count, over_filled) "
            "VALUES ($1, 'no', 'sports', 'kalshi', 5, 0.40, 'open', $2, 5, FALSE)",
            "MKT-OK", "oid-normal",
        )
        await _execute(
            "INSERT INTO order_fills (order_id, fill_id, count, price_cents) "
            "VALUES ($1, 'f1', 5, 40)",
            "oid-normal",
        )

        # Over-filled YESTERDAY: must NOT appear in today's badge.
        await _execute(
            "INSERT INTO positions (market_id, side, vertical, exchange, "
            "quantity, avg_price, status, order_id, filled_count, over_filled) "
            "VALUES ($1, 'yes', 'sports', 'kalshi', 4, 0.30, 'open', $2, 4, TRUE)",
            "MKT-OLD", "oid-overfilled-yesterday",
        )
        await _execute(
            "INSERT INTO order_fills (order_id, fill_id, count, price_cents, received_at) "
            "VALUES ($1, 'f1', 6, 30, now() - interval '2 days')",
            "oid-overfilled-yesterday",
        )

        out = await dashboard.get_over_filled_orders_today()
        await pool.close()
        return out

    out = _run(go())

    assert out["count"] == 1
    items = out["items"]
    assert len(items) == 1
    row = items[0]
    assert row["order_id"] == "oid-overfilled-today"
    assert row["market_id"] == "MKT-OVF"
    assert row["market_title"] == "Over-filled market"
    assert row["requested_count"] == 10
    assert row["filled_count"] == 12
    assert row["discarded_count"] == 2
    assert row["last_fill_at"] is not None
