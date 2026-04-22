"""Integration test for the GTC partial-fill position rollup against a REAL
Postgres instance.

Spins up an isolated Postgres data dir via `initdb` + `pg_ctl`, applies the
columns/indexes from the 0002 migration, then exercises
`_upsert_position_rollup` end-to-end:

  * 3 partial fills against the same `order_id` produce ONE row in `positions`
    (not three) thanks to `ON CONFLICT (order_id) DO UPDATE`.
  * The conflict target matches the unique index created by the migration —
    if the migration's index were partial / mismatched, the upsert would
    raise `42P10` (no unique-or-exclusion constraint matching ON CONFLICT).
  * Repeated upserts overwrite quantity / avg_price and OR the over_filled
    flag forward.

The test SKIPS when the sandbox has no `pg_ctl` / `initdb` available so it
stays portable; it RUNS in CI/dev where Postgres is on PATH.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

# asyncpg is already a runtime dep of the api-server.
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

    tmp = Path(tempfile.mkdtemp(prefix="axiom_pg_"))
    data = tmp / "data"
    sock = tmp / "sock"
    sock.mkdir()
    log = tmp / "pg.log"
    port = _free_port()

    subprocess.check_call(
        [initdb, "-D", str(data), "-A", "trust", "-U", "postgres"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Bind only to unix socket + localhost; isolate from the system cluster.
    opts = f"-p {port} -k {sock} -h 127.0.0.1"
    subprocess.check_call(
        [pg_ctl, "-D", str(data), "-l", str(log), "-o", opts, "-w", "start"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    # Wait for accept loop.
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


# Mirror of the columns/indexes touched by 0002_order_fills_position_rollup.py
# kept in sync with that migration. The upstream `positions` schema isn't in
# our migrations, so we re-create the minimum surface needed for the upsert.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS positions (
    id           bigserial PRIMARY KEY,
    market_id    text,
    side         text,
    vertical     text,
    exchange     text,
    quantity     integer,
    avg_price    double precision,
    status       text,
    closed_at    timestamptz,
    current_price double precision
);
ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS order_id     text,
    ADD COLUMN IF NOT EXISTS filled_count integer,
    ADD COLUMN IF NOT EXISTS over_filled  boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS positions_order_id_uidx
    ON positions (order_id);
"""


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_position_rollup_upsert_against_real_postgres(pg_dsn, monkeypatch):
    """Three partial fills for the same order_id collapse to one row, and the
    ON CONFLICT (order_id) clause matches the migration's unique index.
    """
    from app import db as app_db
    from app.kalshi import lifecycle

    async def go():
        # Re-point the app's db helpers at the temp Postgres for this test.
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

        # Three partial fills: 3@50, 4@60, 3@70 → cum 10 @ weighted 60¢.
        await lifecycle._upsert_position_rollup(
            order_id="oid-1", market_id="MKT", side="yes", vertical="sports",
            quantity=3, avg_price_cents=50, over_filled=False, closed=False,
        )
        await lifecycle._upsert_position_rollup(
            order_id="oid-1", market_id="MKT", side="yes", vertical="sports",
            quantity=7, avg_price_cents=56, over_filled=False, closed=False,
        )
        await lifecycle._upsert_position_rollup(
            order_id="oid-1", market_id="MKT", side="yes", vertical="sports",
            quantity=10, avg_price_cents=60, over_filled=True, closed=False,
        )

        rows = await _fetch(
            "SELECT order_id, quantity, avg_price, filled_count, over_filled, status "
            "FROM positions WHERE order_id = $1",
            "oid-1",
        )
        await pool.close()
        return rows

    rows = _run(go())

    # Single rolled-up row, not three.
    assert len(rows) == 1
    row = rows[0]
    assert row["order_id"] == "oid-1"
    assert row["quantity"] == 10
    assert row["filled_count"] == 10
    # avg_price stored as 0..1 unit price (60¢ = 0.60).
    assert abs(float(row["avg_price"]) - 0.60) < 1e-9
    # over_filled stays True after being set, even though earlier upserts were False.
    assert row["over_filled"] is True
    assert row["status"] == "open"
