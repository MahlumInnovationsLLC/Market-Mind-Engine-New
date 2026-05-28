"""One-shot copy of legacy heap data into the partitioned tables (task #921).

Migration ``0061_partition_line_snapshots_odds_changes`` renames the
pre-existing heaps to ``<name>_legacy_pre0061`` and creates fresh
partitioned tables under the original names so live writers continue
without code changes. This script copies historical rows from each
legacy heap into the appropriate monthly partitions.

Design points:
  * Walks per-month batches in reverse chronological order so the most
    recent data — the rows readers care about — lands first.
  * Calls :func:`app.partitioning.create_partition_sql` to ensure each
    target partition exists before inserting (handles months older
    than the migration's seed window).
  * ``ON CONFLICT DO NOTHING`` on ``line_snapshots`` (its natural-key
    unique tuple is the new PK) so re-runs are idempotent.
  * ``odds_changes`` has no unique constraint; reruns insert duplicates.
    The script defends against this by recording its progress in
    ``partitioned_backfill_cursor`` and refusing to re-process a month
    it has already finished — pass ``--reset`` to force.
  * Reports pre/post row counts so the operator can verify before
    dropping the legacy heap.

Usage:
    python -m scripts.backfill_partitioned_tables \\
        [--table line_snapshots|odds_changes|all] \\
        [--batch-rows 20000] [--max-months N] [--dry-run] [--reset]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Iterable

from app import db, partitioning


log = logging.getLogger("axiom.backfill_partitioned_tables")


_LEGACY: dict[str, str] = {
    "line_snapshots": "line_snapshots_legacy_pre0061",
    "odds_changes":   "odds_changes_legacy_pre0061",
}

# Column shape of each table (without ``id`` — see migration 0061).
_COLUMNS: dict[str, tuple[str, ...]] = {
    "line_snapshots": (
        "game_id", "sport", "book", "market_type", "line_value",
        "odds_value", "home_team", "away_team", "is_opening", "outcome",
        "timestamp",
    ),
    "odds_changes": (
        "game_id", "book", "market_type", "line", "odds", "timestamp",
    ),
}

# ON CONFLICT target (line_snapshots only — odds_changes has no UQ).
_CONFLICT_TARGET: dict[str, str] = {
    "line_snapshots": '(game_id, book, market_type, outcome, "timestamp")',
}


async def _ensure_cursor_table() -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS partitioned_backfill_cursor (
            parent_table  text        NOT NULL,
            month_suffix  text        NOT NULL,
            rows_copied   bigint      NOT NULL,
            finished_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (parent_table, month_suffix)
        )
    """)


async def _legacy_present(legacy: str) -> bool:
    row = await db.fetchrow(
        "SELECT 1 FROM pg_class WHERE relname = $1", legacy,
    )
    return row is not None


async def _months_in_legacy(legacy: str) -> list[partitioning.MonthBounds]:
    row = await db.fetchrow(
        f'SELECT MIN("timestamp")::timestamptz AS mn, '
        f'       MAX("timestamp")::timestamptz AS mx FROM {legacy}'
    )
    if not row or row["mn"] is None or row["mx"] is None:
        return []
    return partitioning.months_between(row["mn"], row["mx"])


async def _already_done(parent: str, suffix: str) -> bool:
    row = await db.fetchrow(
        "SELECT 1 FROM partitioned_backfill_cursor "
        "WHERE parent_table = $1 AND month_suffix = $2",
        parent, suffix,
    )
    return row is not None


async def _record_cursor(parent: str, suffix: str, rows: int) -> None:
    await db.execute(
        "INSERT INTO partitioned_backfill_cursor "
        "(parent_table, month_suffix, rows_copied) "
        "VALUES ($1, $2, $3) "
        "ON CONFLICT (parent_table, month_suffix) DO UPDATE "
        "  SET rows_copied = EXCLUDED.rows_copied, finished_at = now()",
        parent, suffix, rows,
    )


async def _ensure_partition(parent: str, b: partitioning.MonthBounds) -> None:
    await db.execute(partitioning.create_partition_sql(parent, b))


async def _copy_month(
    parent: str,
    legacy: str,
    bounds: partitioning.MonthBounds,
    *,
    batch_rows: int,
    dry_run: bool,
) -> int:
    cols = _COLUMNS[parent]
    col_csv = ", ".join(f'"{c}"' for c in cols)
    conflict = _CONFLICT_TARGET.get(parent)
    on_conflict = f"ON CONFLICT {conflict} DO NOTHING" if conflict else ""

    if parent == "odds_changes":
        # The legacy table's `timestamp` is TEXT; cast on read so the
        # partition routing key matches the new TIMESTAMPTZ partition.
        select_cols = (
            'game_id, book, market_type, line, odds, '
            '"timestamp"::timestamptz AS "timestamp"'
        )
        ts_pred = '("timestamp"::timestamptz)'
    else:
        select_cols = col_csv
        ts_pred = '"timestamp"'

    where = (
        f'{ts_pred} >= $1::timestamptz AND {ts_pred} < $2::timestamptz'
    )

    pre = await db.fetchrow(
        f'SELECT COUNT(*) AS n FROM {legacy} WHERE {where}',
        bounds.start, bounds.end,
    )
    n_src = int(pre["n"] or 0) if pre else 0
    if n_src == 0:
        return 0

    log.info(
        "backfill %s [%s]: %s rows in legacy", parent, bounds.suffix, n_src,
    )
    if dry_run:
        return n_src

    # We use a single set-based INSERT ... SELECT per month — Postgres
    # handles batching internally and the partition router picks the
    # right child. For *very* large months (>5M rows), the operator
    # can pass a smaller --batch-rows to chunk via OFFSET; we keep
    # the default simple because most months land < 5M.
    total_copied = 0
    if batch_rows <= 0:
        status = await db.execute(
            f'INSERT INTO {parent} ({col_csv}) '
            f'SELECT {select_cols} FROM {legacy} WHERE {where} '
            f'{on_conflict}',
            bounds.start, bounds.end,
        )
        try:
            total_copied = int((status or "INSERT 0 0").split()[-1])
        except Exception:
            total_copied = 0
    else:
        offset = 0
        while True:
            status = await db.execute(
                f'INSERT INTO {parent} ({col_csv}) '
                f'SELECT {select_cols} FROM {legacy} '
                f'WHERE {where} '
                f'ORDER BY "timestamp" '
                f'OFFSET $3 LIMIT $4 '
                f'{on_conflict}',
                bounds.start, bounds.end, offset, batch_rows,
            )
            try:
                chunk = int((status or "INSERT 0 0").split()[-1])
            except Exception:
                chunk = 0
            total_copied += chunk
            if chunk < batch_rows:
                break
            offset += batch_rows
            log.info(
                "  …%s [%s]: %s rows copied so far",
                parent, bounds.suffix, total_copied,
            )
    log.info(
        "backfill %s [%s]: %s rows copied", parent, bounds.suffix, total_copied,
    )
    return total_copied


async def _verify_counts(parent: str, legacy: str) -> None:
    src = await db.fetchrow(f"SELECT COUNT(*) AS n FROM {legacy}")
    dst = await db.fetchrow(f"SELECT COUNT(*) AS n FROM {parent}")
    sn = int(src["n"] or 0) if src else 0
    dn = int(dst["n"] or 0) if dst else 0
    log.info(
        "verify %s: legacy=%s partitioned=%s delta=%s",
        parent, sn, dn, dn - sn,
    )


async def _run(
    tables: Iterable[str],
    *,
    batch_rows: int,
    max_months: int | None,
    dry_run: bool,
    reset: bool,
) -> None:
    await _ensure_cursor_table()
    if reset and not dry_run:
        await db.execute(
            "DELETE FROM partitioned_backfill_cursor "
            "WHERE parent_table = ANY($1::text[])",
            list(tables),
        )

    for parent in tables:
        legacy = _LEGACY.get(parent)
        if legacy is None:
            log.warning("unknown table %s, skipping", parent)
            continue
        if not await _legacy_present(legacy):
            log.warning(
                "legacy table %s not present — nothing to backfill into %s",
                legacy, parent,
            )
            continue
        months = await _months_in_legacy(legacy)
        if not months:
            log.info("%s: legacy heap empty", parent)
            continue
        # Reverse: most recent first.
        months = list(reversed(months))
        if max_months:
            months = months[:max_months]

        for b in months:
            if await _already_done(parent, b.suffix):
                log.info(
                    "%s [%s]: already in cursor, skipping (pass --reset to force)",
                    parent, b.suffix,
                )
                continue
            await _ensure_partition(parent, b)
            n = await _copy_month(
                parent, legacy, b, batch_rows=batch_rows, dry_run=dry_run,
            )
            if not dry_run:
                await _record_cursor(parent, b.suffix, n)

        if not dry_run:
            await _verify_counts(parent, legacy)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--table", choices=["line_snapshots", "odds_changes", "all"], default="all")
    p.add_argument(
        "--batch-rows", type=int, default=0,
        help="if >0, chunk each month's INSERT ... SELECT into batches of N",
    )
    p.add_argument(
        "--max-months", type=int, default=None,
        help="limit copy to the N most recent months (smoke test)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--reset", action="store_true",
        help="clear the cursor table before running (forces full re-copy)",
    )
    return p


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_parser().parse_args()
    tables = ("line_snapshots", "odds_changes") if args.table == "all" else (args.table,)
    try:
        asyncio.run(_run(
            tables,
            batch_rows=args.batch_rows,
            max_months=args.max_months,
            dry_run=args.dry_run,
            reset=args.reset,
        ))
    except KeyboardInterrupt:
        log.warning("interrupted by user")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
