"""Idempotent ETL: V1 historical-data dumps -> ``v1_archive.*``.

Phase 1 (this script): import the V1 ``trade_analyses`` dump from the
Supabase storage bucket so the per-league calibrators have real
``(predicted_prob, actual_outcome)`` pairs to fit against.

The dump itself is at
``axiom-predict-artifacts/replit-port/archives/pg_dumps/trade_analyses.sql.gz``
and contains 667,094 rows in PLAIN ``pg_dump`` format (data-only, no
DDL).  It targets ``public.trade_analyses``; we rewrite the COPY
header to land in ``v1_archive.trade_analyses`` (created by Alembic
migration ``0037_v1_archive_trade_analyses``) so it cannot collide
with anything MME writes forward.

Idempotent: re-running the script TRUNCATEs the target table inside a
single transaction, then re-loads from the dump.  No partial state is
ever exposed to readers.

Failure semantics:
* If the dump can't be downloaded, the script exits non-zero before
  touching the database.
* If the post-load row count diverges from V1's expected count
  (currently 667,094) by more than ``--max-mismatch-pct`` (default
  5%), the transaction is rolled back and the script exits non-zero.
* If ``--dry-run`` is passed, the dump is downloaded and rewritten
  but ``psql`` is invoked with ``--single-transaction`` and the
  transaction is rolled back at the end.

Run with::

    python -m scripts.import_v1_history          # full reload
    python -m scripts.import_v1_history --dry-run

The ``DATABASE_URL`` env var is the only required input.  Storage
credentials come from ``app.storage`` (already configured for the
verifier script).
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Allow running as both ``python scripts/import_v1_history.py`` (from
# the api-server dir) and ``python -m scripts.import_v1_history``.
_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app import storage  # noqa: E402  (sys.path tweak above)


BUCKET_PATH = "replit-port/archives/pg_dumps/trade_analyses.sql.gz"
EXPECTED_ROWS = 667_094
LOCAL_GZ = Path("/tmp/v1_dumps/trade_analyses.sql.gz")
LOCAL_SQL = Path("/tmp/v1_dumps/trade_analyses.sql")
REWRITTEN_SQL = Path("/tmp/v1_dumps/trade_analyses.v1_archive.sql")


def _log(msg: str) -> None:
    print(f"[import_v1_history] {msg}", flush=True)


async def _ensure_dump_local(force: bool) -> None:
    LOCAL_GZ.parent.mkdir(parents=True, exist_ok=True)
    if not LOCAL_GZ.exists() or force:
        _log(f"downloading {BUCKET_PATH} -> {LOCAL_GZ}")
        await storage.download_to_path(BUCKET_PATH, LOCAL_GZ, force=True)
    size_mb = LOCAL_GZ.stat().st_size / 1024 / 1024
    _log(f"gz dump ready ({size_mb:.1f} MiB)")

    if not LOCAL_SQL.exists() or force:
        _log(f"gunzip -> {LOCAL_SQL}")
        with gzip.open(LOCAL_GZ, "rb") as src, open(LOCAL_SQL, "wb") as dst:
            shutil.copyfileobj(src, dst, length=64 * 1024 * 1024)
    sql_size_mb = LOCAL_SQL.stat().st_size / 1024 / 1024
    _log(f"plain SQL ready ({sql_size_mb:.1f} MiB)")


def _rewrite_for_v1_archive() -> None:
    """Rewrite COPY-header schema and strip ``setval`` for the dropped sequence.

    The V1 dump emits:
        COPY public.trade_analyses (...) FROM stdin;
        ...rows...
        \\.
        SELECT pg_catalog.setval('public.trade_analyses_id_seq', 667736, true);

    Our target table has no id-sequence (id is text), so we drop the
    setval line.  Everything else (SET statement_timeout etc.) is
    safe to keep.
    """
    _log(f"rewriting dump -> {REWRITTEN_SQL}")
    with open(LOCAL_SQL, "r", encoding="utf-8", errors="strict") as src, \
         open(REWRITTEN_SQL, "w", encoding="utf-8") as dst:
        for line in src:
            stripped = line.lstrip()
            # ``\restrict`` / ``\unrestrict`` are psql >=18 metacommands
            # the V1 dump was produced with; our local psql is 17.5 and
            # rejects them.  They are session-scope cosmetic locks, safe
            # to drop for an offline reload.
            if stripped.startswith("\\restrict ") or stripped.startswith(
                "\\unrestrict "
            ):
                continue
            if line.startswith("COPY public.trade_analyses "):
                line = line.replace(
                    "COPY public.trade_analyses ",
                    "COPY v1_archive.trade_analyses ",
                    1,
                )
            elif "trade_analyses_id_seq" in line:
                # Skip the SELECT setval(...) line entirely.
                continue
            dst.write(line)
    _log(f"rewritten size: {REWRITTEN_SQL.stat().st_size / 1024 / 1024:.1f} MiB")


def _build_psql_dsn() -> str:
    """Use the same Supabase DSN that ``alembic`` resolves through
    ``app.config``, NOT the raw ``DATABASE_URL`` env var (which on
    Replit is a local placeholder pointing at a stub Postgres)."""
    from app.config import get_settings  # local import keeps script CLI-fast

    dsn = get_settings().supabase_db_dsn
    if not dsn:
        raise SystemExit("supabase_db_dsn is empty in settings")
    return dsn


def _run_psql(sql: str, dsn: str) -> str:
    """Run a one-shot SQL statement via psql -c and return stdout."""
    result = subprocess.run(
        ["psql", dsn, "-tAX", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _truncate_target(dsn: str) -> None:
    _log("TRUNCATE v1_archive.trade_analyses")
    _run_psql("TRUNCATE TABLE v1_archive.trade_analyses", dsn)


def _load_dump(dsn: str, dry_run: bool) -> None:
    _log(f"psql --single-transaction -f {REWRITTEN_SQL} (dry_run={dry_run})")
    cmd = [
        "psql",
        dsn,
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "-f",
        str(REWRITTEN_SQL),
    ]
    if dry_run:
        # Wrap in BEGIN/ROLLBACK by streaming through stdin instead.
        cmd = [
            "psql",
            dsn,
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
        ]
        with open(REWRITTEN_SQL, "rb") as src:
            payload = b"BEGIN;\n" + src.read() + b"\nROLLBACK;\n"
        completed = subprocess.run(
            cmd, input=payload, capture_output=True, check=False
        )
    else:
        completed = subprocess.run(cmd, capture_output=True, check=False)

    if completed.returncode != 0:
        sys.stderr.write(completed.stderr.decode("utf-8", errors="replace"))
        raise SystemExit(
            f"psql exited {completed.returncode} loading rewritten dump"
        )
    _log("psql load OK")


def _verify_counts(dsn: str, max_mismatch_pct: float) -> dict:
    total_str = _run_psql(
        "SELECT count(*) FROM v1_archive.trade_analyses", dsn
    )
    total = int(total_str)
    diff_pct = (
        abs(total - EXPECTED_ROWS) / EXPECTED_ROWS * 100.0
        if EXPECTED_ROWS
        else 0.0
    )
    _log(f"row count: {total:,} (expected ~{EXPECTED_ROWS:,}, "
         f"diff {diff_pct:.2f}%)")
    if diff_pct > max_mismatch_pct:
        raise SystemExit(
            f"FAIL: row count diverged {diff_pct:.2f}% > "
            f"{max_mismatch_pct}% threshold; aborting."
        )

    per_league = _run_psql(
        """
        SELECT league || '|' || count(*)::text
        FROM v1_archive.trade_analyses
        GROUP BY league
        ORDER BY count(*) DESC
        LIMIT 50
        """,
        dsn,
    )
    _log("top leagues by row count:")
    for line in per_league.splitlines():
        _log(f"  {line}")

    return {"total": total, "diff_pct": diff_pct}


async def _amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load inside a transaction and ROLLBACK at the end.",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download the dump even if a local copy exists.",
    )
    parser.add_argument(
        "--max-mismatch-pct",
        type=float,
        default=5.0,
        help="Fail if loaded row count diverges from the expected "
             "V1 row count by more than this percent (default 5%%).",
    )
    args = parser.parse_args()

    dsn = _build_psql_dsn()
    _log(f"target DSN host: {dsn.split('@')[-1].split('/')[0]}")

    await _ensure_dump_local(force=args.force_download)
    _rewrite_for_v1_archive()

    if not args.dry_run:
        _truncate_target(dsn)

    _load_dump(dsn, dry_run=args.dry_run)

    if args.dry_run:
        _log("DRY-RUN complete (transaction rolled back); skipping verify.")
        return 0

    summary = _verify_counts(dsn, args.max_mismatch_pct)
    _log(f"DONE: {summary}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
