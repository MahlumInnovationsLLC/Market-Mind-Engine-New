"""End-to-end integration test for the tech-AI evidence cache (task #221).

Spins up an isolated Postgres data dir via `initdb` + `pg_ctl`, applies the
schema from `migrations/versions/0007_tech_evidence.py`, then exercises the
real resolver + predictor against the live cache:

  * Inserts a handful of synthetic rows into `tech_evidence` for symbol GPT
    and asserts `predict_tech_ai_binary("KXGPT-26JUN-T1")` returns a
    probability shifted ABOVE the 0.5 prior in the expected direction.
  * Inserts one row OUTSIDE the lookback cutoff and asserts the resolver
    drops it (so the predictor falls back closer to the prior than when
    the row is included).

Skips when no `pg_ctl` / `initdb` are on PATH so the suite stays portable;
runs in CI/dev where Postgres is available.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
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

    tmp = Path(tempfile.mkdtemp(prefix="axiom_pg_tech_"))
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


# Source the schema directly from the real migration module so this test
# exercises the actual migration SQL — if the migration drifts (column
# type / index name / unique key), the upsert + resolver SQL paths here
# fail too. Imported lazily inside the test bodies (after sys.path is
# set up by conftest) via _load_migration_sql().


def _load_migration_sql() -> str:
    """Return the raw DDL string from `migrations/versions/0007_tech_evidence.py`.

    Loaded by file path because the `migrations/` tree is not a Python
    package on the api-server import path.
    """
    import importlib.util

    mig_path = (
        Path(__file__).resolve().parent.parent
        / "migrations" / "versions" / "0007_tech_evidence.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_tech_evidence_migration_0007", mig_path,
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod._SQL  # type: ignore[attr-defined]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


async def _insert_row(
    pool, *,
    source: str, signal_id: str, symbol: str, kind: str,
    observed_at: datetime, weight: float, payload: dict,
) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """
            INSERT INTO tech_evidence
                (source, signal_id, symbol, kind, observed_at, weight, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT (source, signal_id) DO UPDATE
              SET symbol = EXCLUDED.symbol,
                  kind = EXCLUDED.kind,
                  observed_at = EXCLUDED.observed_at,
                  weight = EXCLUDED.weight,
                  payload = EXCLUDED.payload
            """,
            source, signal_id, symbol, kind, observed_at, weight,
            json.dumps(payload),
        )


def test_predictor_against_live_evidence_cache(pg_dsn, monkeypatch):
    """Synthetic evidence inserted directly into `tech_evidence` for GPT
    should shift the predictor's probability ABOVE the 0.5 prior on an
    "above-threshold" KXGPT market."""
    from app import db as app_db
    from app.tech_ai import predictor as tp

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

        await _execute(_load_migration_sql())

        now = datetime.now(timezone.utc)
        # Three supportive rows for GPT, all inside the default 30-day window.
        await _insert_row(
            pool,
            source="github_releases", signal_id="openai/gpt-runtime@v1",
            symbol="GPT", kind="release",
            observed_at=now - timedelta(days=2),
            weight=1.0, payload={"repo": "openai/gpt-runtime"},
        )
        await _insert_row(
            pool,
            source="hf_trending", signal_id="openai/gpt-mini",
            symbol="GPT", kind="trending",
            observed_at=now - timedelta(days=4),
            weight=1.0, payload={"downloads": 10_000},
        )
        await _insert_row(
            pool,
            source="mlperf", signal_id="mlperf-2026-r1-gpt",
            symbol="GPT", kind="benchmark",
            observed_at=now - timedelta(days=7),
            weight=1.0, payload={"score": 0.91},
        )
        # A row for a DIFFERENT symbol — must not contribute.
        await _insert_row(
            pool,
            source="github_releases", signal_id="nv/cuda@v13",
            symbol="NVDA", kind="release",
            observed_at=now - timedelta(days=1),
            weight=1.0, payload={},
        )

        pred = await tp.predict_tech_ai_binary("KXGPT-26JUN-T1")
        await pool.close()
        return pred

    pred = _run(go())

    # Direction is "above"; supportive release/trending/benchmark evidence
    # should push YES probability strictly above the 0.5 prior.
    assert pred.probability > 0.5, pred
    # Cold-path prior is exactly 0.5 — assert we moved meaningfully off it.
    assert pred.probability >= 0.6, pred
    # Three contributing rows from the GPT inserts (NVDA row scoped out by SQL).
    assert pred.raw["evidence_count"] == 3
    assert pred.raw["contributing_rows"] == 3
    assert pred.model_name == "tech_ai_bayesian"


def test_lookback_window_drops_rows_outside_cutoff(pg_dsn, monkeypatch):
    """A row dated outside the lookback cutoff must be dropped by the
    resolver query — the predictor's evidence_count should reflect only
    the in-window rows."""
    from app import db as app_db
    from app.tech_ai import predictor as tp

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

        # Reuse table from the first test; clear GPT rows for isolation.
        await _execute(_load_migration_sql())
        await _execute("DELETE FROM tech_evidence WHERE symbol = 'GPT'")

        now = datetime.now(timezone.utc)
        # One in-window row, one ancient row well outside a 14-day cutoff.
        await _insert_row(
            pool,
            source="github_releases", signal_id="openai/recent",
            symbol="GPT", kind="release",
            observed_at=now - timedelta(days=3),
            weight=1.0, payload={},
        )
        await _insert_row(
            pool,
            source="github_releases", signal_id="openai/ancient",
            symbol="GPT", kind="release",
            observed_at=now - timedelta(days=120),
            weight=1.0, payload={},
        )

        pred_short = await tp.predict_tech_ai_binary(
            "KXGPT-26JUN-T1", lookback_days=14,
        )
        pred_long = await tp.predict_tech_ai_binary(
            "KXGPT-26JUN-T1", lookback_days=365,
        )
        await pool.close()
        return pred_short, pred_long

    pred_short, pred_long = _run(go())

    # 14-day window: ancient row dropped → exactly 1 evidence row.
    assert pred_short.raw["evidence_count"] == 1, pred_short.raw
    # 365-day window: both rows visible → 2 evidence rows.
    assert pred_long.raw["evidence_count"] == 2, pred_long.raw
    # Both predictions still shifted off the prior in the supportive direction.
    assert pred_short.probability > 0.5
    assert pred_long.probability > 0.5
