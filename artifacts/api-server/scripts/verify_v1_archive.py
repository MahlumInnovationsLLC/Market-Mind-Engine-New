"""Smoke-check that the V1 historical import landed correctly.

Counterpart to ``verify_v155_artifacts.py``: exits 0 when the
``v1_archive.trade_analyses`` table exists, has the expected shape,
and contains enough rows to actually unblock the per-league
calibrators.  Exits non-zero if any of those checks fail.

Acceptance gates (all must pass for a PASS verdict):

1. Schema ``v1_archive`` exists and contains ``trade_analyses``.
2. The 14 V1 source columns plus the 2 provenance columns
   (``imported_from``, ``imported_at``) are all present.
3. The total row count is within 5% of the V1 dump's expected
   ``667,094``.
4. After applying the calibrator's ``REFIT_WINDOW_DAYS=90`` and
   ``MIN_REFIT_SAMPLES=10`` thresholds against
   ``v1_archive.trade_analyses``, at least 3 SUPPORTED_LEAGUES end
   up with a sufficient sample size (i.e. the V1 data could
   meaningfully fit calibrators for them once the calibrator query
   is extended to consume ``v1_archive``).

Usage::

    python -m scripts.verify_v1_archive
    python scripts/verify_v1_archive.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

import asyncpg  # noqa: E402  (sys.path tweak above)

from app.config import get_settings  # noqa: E402


EXPECTED_ROWS = 667_094
ROW_COUNT_TOLERANCE_PCT = 5.0
REFIT_WINDOW_DAYS = 90
MIN_REFIT_SAMPLES = 10
MIN_LEAGUES_ELIGIBLE = 3
EXPECTED_COLUMNS = {
    "id", "trade_id", "market_id", "league", "market_type",
    "predicted_prob", "actual_outcome", "edge_at_entry", "pnl",
    "analysis_json", "improvement_suggestion", "grade",
    "created_at", "vertical", "imported_from", "imported_at",
}


def _log(level: str, msg: str) -> None:
    print(f"[{level}] {msg}", flush=True)


async def _check_schema_exists(conn: asyncpg.Connection) -> bool:
    schema_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'v1_archive')"
    )
    if not schema_exists:
        _log("FAIL", "schema 'v1_archive' does not exist")
        return False
    _log("OK", "schema 'v1_archive' exists")

    table_exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'v1_archive' AND c.relname = 'trade_analyses'
        )
        """
    )
    if not table_exists:
        _log("FAIL", "table 'v1_archive.trade_analyses' does not exist")
        return False
    _log("OK", "table 'v1_archive.trade_analyses' exists")
    return True


async def _check_columns(conn: asyncpg.Connection) -> bool:
    rows = await conn.fetch(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = 'v1_archive'
           AND table_name   = 'trade_analyses'
        """
    )
    actual = {r["column_name"] for r in rows}
    missing = EXPECTED_COLUMNS - actual
    if missing:
        _log("FAIL", f"missing columns: {sorted(missing)}")
        return False
    _log("OK", f"all {len(EXPECTED_COLUMNS)} expected columns present")
    return True


async def _check_row_count(conn: asyncpg.Connection) -> bool:
    total = int(await conn.fetchval(
        "SELECT count(*) FROM v1_archive.trade_analyses"
    ))
    diff_pct = (
        abs(total - EXPECTED_ROWS) / EXPECTED_ROWS * 100.0
        if EXPECTED_ROWS else 0.0
    )
    if diff_pct > ROW_COUNT_TOLERANCE_PCT:
        _log("FAIL",
             f"row count {total:,} diverges {diff_pct:.2f}% > "
             f"{ROW_COUNT_TOLERANCE_PCT}% from expected {EXPECTED_ROWS:,}")
        return False
    _log("OK", f"row count {total:,} (expected ~{EXPECTED_ROWS:,}, "
               f"diff {diff_pct:.2f}%)")
    return True


async def _check_calibrator_eligibility(conn: asyncpg.Connection) -> bool:
    rows = await conn.fetch(
        f"""
        SELECT upper(league) AS league, count(*) AS n
          FROM v1_archive.trade_analyses
         WHERE created_at > now() - interval '{REFIT_WINDOW_DAYS} days'
           AND predicted_prob IS NOT NULL
           AND actual_outcome IS NOT NULL
           AND league IS NOT NULL
           AND upper(league) <> 'UNKNOWN'
         GROUP BY upper(league)
        HAVING count(*) >= {MIN_REFIT_SAMPLES}
         ORDER BY count(*) DESC
        """
    )
    eligible = [(r["league"], int(r["n"])) for r in rows]
    if len(eligible) < MIN_LEAGUES_ELIGIBLE:
        _log("FAIL",
             f"only {len(eligible)} leagues eligible "
             f"(need >= {MIN_LEAGUES_ELIGIBLE}): {eligible}")
        return False
    _log("OK",
         f"{len(eligible)} leagues eligible for calibrator refit "
         f"once query is extended to v1_archive:")
    for lg, n in eligible:
        _log("OK", f"    {lg:8} n={n:,}")
    return True


async def _amain() -> int:
    dsn = get_settings().supabase_db_dsn
    if not dsn:
        _log("FAIL", "supabase_db_dsn is empty in settings")
        return 1
    _log("INFO", f"target DSN host: {dsn.split('@')[-1].split('/')[0]}")

    conn = await asyncpg.connect(dsn=dsn)
    try:
        checks = [
            await _check_schema_exists(conn),
            await _check_columns(conn),
            await _check_row_count(conn),
            await _check_calibrator_eligibility(conn),
        ]
    finally:
        await conn.close()

    if all(checks):
        _log("PASS", "v1_archive smoke check passed")
        return 0
    _log("FAIL", "v1_archive smoke check FAILED")
    return 1


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
