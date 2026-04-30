"""Idempotent backfill: re-derive ``league`` for V1-archive UNKNOWN rows.

Task #740.  V1 wrote literal ``'unknown'`` (or NULL) into
``v1_archive.trade_analyses.league`` for 598,218 of 603,128 rows
because its own per-trade league heuristic failed.  The per-league
calibrator stack therefore never matched on those rows and the
calibrated-Brier metric in
``scripts/backtest_v1_archive.py`` was effectively reading 1% of the
imported archive.

This script walks the unresolved rows in batches, runs the inference
cascade in ``app.v1_archive.league_inference.infer_league`` on each
row, and writes the result back via a single atomic transaction.  An
audit column ``league_inferred_from`` records WHICH rule fired (and
the original V1 ``league`` value) so the decision is reversible.

Idempotent: re-running the script only touches rows that are STILL
unresolved at the moment the script reads them.  A successful prior
run leaves nothing for the second run to do (it'll exit with
``rows_updated=0``).

``--dry-run`` mirrors ``import_v1_history.py``: the SELECT + UPDATE
runs inside a single transaction that is ROLLED BACK at the end.  The
per-rule summary is still printed so the operator can see what WOULD
have happened.

Run::

    python -m scripts.backfill_v1_archive_leagues          # full backfill
    python -m scripts.backfill_v1_archive_leagues --dry-run
    python -m scripts.backfill_v1_archive_leagues --limit 1000

The script reuses the same Supabase DSN that ``alembic`` resolves
through ``app.config`` so it cannot accidentally write to the local
stub Postgres.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Allow running as both ``python scripts/backfill_v1_archive_leagues.py``
# (from the api-server dir) and ``python -m scripts.backfill_v1_archive_leagues``.
_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app.v1_archive.league_inference import (  # noqa: E402
    infer_league,
    is_unresolved,
)


DEFAULT_BATCH_SIZE = 5_000


def _log(msg: str) -> None:
    print(f"[backfill_v1_archive_leagues] {msg}", flush=True)


def _build_dsn() -> str:
    from app.config import get_settings  # local import keeps script CLI-fast

    dsn = get_settings().supabase_db_dsn
    if not dsn:
        raise SystemExit("supabase_db_dsn is empty in settings")
    return dsn


def _parse_analysis_json(raw: Any) -> Any:
    """asyncpg returns ``jsonb`` as already-parsed Python objects, but
    if the column was stored as ``text`` (or returned via psql in some
    edge cases) we still need a JSON parse step.  Return ``None`` on
    parse failure rather than crashing the batch."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return None


def _classify_row(row: dict[str, Any]) -> tuple[str, str, str] | None:
    """Run the inference cascade on a single asyncpg row.

    Returns ``(row_id, new_league, audit_key)`` on success, ``None``
    when no rule fires (row stays UNRESOLVED).
    """
    raw_league = row.get("league")
    if not is_unresolved(raw_league):
        # Defensive: the SELECT filter already excludes resolved rows,
        # but a concurrent writer could have updated the row between
        # SELECT and the in-Python classification step.  Skip rather
        # than overwrite.
        return None
    result = infer_league(
        market_id=row.get("market_id"),
        vertical=row.get("vertical"),
        analysis_json=_parse_analysis_json(row.get("analysis_json")),
        raw_league=raw_league,
    )
    if result is None:
        return None
    new_league, rule_key = result
    # Capture the original V1 value alongside the rule key so a future
    # audit/rollback can be done without re-querying the dump.
    orig_repr = (
        "null" if raw_league is None
        else f"'{str(raw_league).strip().lower()}'"
    )
    audit_key = f"{rule_key} orig={orig_repr}"
    return str(row["id"]), new_league, audit_key


async def _run_backfill(
    dsn: str,
    *,
    dry_run: bool,
    batch_size: int,
    limit: int | None,
) -> dict[str, Any]:
    """Walk unresolved rows in batches and apply the inference cascade.

    All work runs inside a single asyncpg transaction so a ``--dry-run``
    rolls EVERYTHING back, mirroring ``import_v1_history.py``.
    """
    import asyncpg  # local import keeps the module importable without asyncpg

    conn: asyncpg.Connection = await asyncpg.connect(dsn=dsn)
    try:
        # Quick totals up front so the operator sees the starting state.
        total_rows = await conn.fetchval(
            "SELECT count(*) FROM v1_archive.trade_analyses"
        )
        unresolved_before = await conn.fetchval(
            """
            SELECT count(*)
              FROM v1_archive.trade_analyses
             WHERE league IS NULL
                OR upper(trim(league)) IN ('', 'UNKNOWN', 'NONE', 'NULL')
            """
        )
        _log(
            f"starting state: total_rows={total_rows} "
            f"unresolved_before={unresolved_before}"
        )

        per_rule_counter: Counter[str] = Counter()
        per_league_counter: Counter[str] = Counter()
        rows_seen = 0
        rows_updated = 0
        # Capture a few sample inferences so the operator can sanity-
        # check the cascade without re-querying the DB.
        samples: list[dict[str, Any]] = []

        async with conn.transaction():
            # Cursor over the unresolved set; we update in-place inside
            # the same transaction so a single ``--dry-run`` rollback
            # reverses everything.
            select_sql = """
                SELECT id, league, market_id, vertical, analysis_json
                  FROM v1_archive.trade_analyses
                 WHERE league IS NULL
                    OR upper(trim(league)) IN ('', 'UNKNOWN', 'NONE', 'NULL')
                 ORDER BY id
            """
            cursor = conn.cursor(select_sql, prefetch=batch_size)

            update_buffer: list[tuple[str, str, str]] = []

            async for r in cursor:
                rows_seen += 1
                row_dict = dict(r)
                classified = _classify_row(row_dict)
                if classified is not None:
                    row_id, new_league, audit_key = classified
                    update_buffer.append((row_id, new_league, audit_key))
                    per_rule_counter[audit_key.split(" orig=")[0]] += 1
                    per_league_counter[new_league] += 1
                    if len(samples) < 8:
                        samples.append({
                            "id": row_id,
                            "market_id": row_dict.get("market_id"),
                            "vertical": row_dict.get("vertical"),
                            "raw_league": row_dict.get("league"),
                            "new_league": new_league,
                            "rule": audit_key,
                        })

                if len(update_buffer) >= batch_size:
                    await _flush_updates(conn, update_buffer)
                    rows_updated += len(update_buffer)
                    update_buffer.clear()
                    _log(
                        f"  progress: rows_seen={rows_seen} "
                        f"rows_updated={rows_updated}"
                    )

                if limit is not None and rows_seen >= limit:
                    _log(f"  --limit {limit} reached; stopping cursor walk")
                    break

            # Flush trailing buffer.
            if update_buffer:
                await _flush_updates(conn, update_buffer)
                rows_updated += len(update_buffer)
                update_buffer.clear()

            if dry_run:
                # Force the transaction to roll back so a --dry-run
                # leaves the table EXACTLY as we found it.
                raise _DryRunRollback()

        # Reached only when not dry-run AND no exception.
        unresolved_after = await conn.fetchval(
            """
            SELECT count(*)
              FROM v1_archive.trade_analyses
             WHERE league IS NULL
                OR upper(trim(league)) IN ('', 'UNKNOWN', 'NONE', 'NULL')
            """
        )
    except _DryRunRollback:
        # Re-fetch unresolved counts AFTER the rollback so the dry-run
        # summary reflects the table's actual current state, not the
        # in-transaction view.
        unresolved_after = await conn.fetchval(
            """
            SELECT count(*)
              FROM v1_archive.trade_analyses
             WHERE league IS NULL
                OR upper(trim(league)) IN ('', 'UNKNOWN', 'NONE', 'NULL')
            """
        )
    finally:
        await conn.close()

    return {
        "dry_run": dry_run,
        "total_rows": total_rows,
        "unresolved_before": unresolved_before,
        "unresolved_after": unresolved_after,
        "rows_seen": rows_seen,
        "rows_updated": rows_updated,
        "rows_left_unresolved": rows_seen - rows_updated,
        "per_rule": dict(per_rule_counter.most_common()),
        "per_league": dict(per_league_counter.most_common()),
        "samples": samples,
    }


class _DryRunRollback(Exception):
    """Sentinel raised inside the transaction to force a rollback."""


async def _flush_updates(
    conn: Any, updates: list[tuple[str, str, str]]
) -> None:
    """Bulk-apply a buffer of ``(id, new_league, audit_key)`` updates."""
    if not updates:
        return
    # asyncpg's executemany over UPDATE ... WHERE id = $1 is the
    # idiomatic batch path; the v1_archive.trade_analyses PK is
    # ``id text PRIMARY KEY`` so per-row updates are O(rows) total
    # over the unresolved set, well within the time budget.
    #
    # The ``AND (league IS NULL OR upper(trim(league)) IN (...))``
    # guard is a race-safety predicate: v1_archive is treated as a
    # frozen V1 dump and we are the only intentional writer, but if
    # any out-of-band writer slipped a real league value in between
    # our SELECT cursor and this batched UPDATE we must NOT overwrite
    # it. The predicate makes the UPDATE a no-op for any row that is
    # no longer unresolved, preserving the intervening edit. This
    # also keeps re-running the script idempotent under all
    # circumstances, not just under the "no concurrent writer"
    # assumption.
    await conn.executemany(
        """
        UPDATE v1_archive.trade_analyses
           SET league = $2,
               league_inferred_from = $3
         WHERE id = $1
           AND (
                 league IS NULL
              OR upper(trim(league)) IN ('', 'UNKNOWN', 'NONE', 'NULL')
               )
        """,
        updates,
    )


def _print_summary(summary: dict[str, Any]) -> None:
    _log(f"DONE: dry_run={summary['dry_run']}")
    _log(f"  total_rows         : {summary['total_rows']}")
    _log(f"  unresolved_before  : {summary['unresolved_before']}")
    _log(f"  unresolved_after   : {summary['unresolved_after']}")
    _log(f"  rows_seen          : {summary['rows_seen']}")
    _log(f"  rows_updated       : {summary['rows_updated']}")
    _log(f"  rows_left_unresolved: {summary['rows_left_unresolved']}")
    _log("  per_rule:")
    for rule, count in summary["per_rule"].items():
        _log(f"    {count:>8}  {rule}")
    _log("  per_league:")
    for league, count in summary["per_league"].items():
        _log(f"    {count:>8}  {league}")
    if summary["samples"]:
        _log("  samples (first 8 inferences):")
        for s in summary["samples"]:
            _log(
                f"    id={s['id']} market_id={s['market_id']!r} "
                f"vertical={s['vertical']!r} raw_league={s['raw_league']!r} "
                f"-> {s['new_league']} ({s['rule']})"
            )


async def _amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the SELECT+UPDATE inside a transaction and ROLLBACK at the end.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"UPDATE batch size (default {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after walking this many unresolved rows (debug aid).",
    )
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be > 0")
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be > 0")

    dsn = _build_dsn()
    _log(f"target DSN host: {dsn.split('@')[-1].split('/')[0]}")
    _log(f"dry_run={args.dry_run} batch_size={args.batch_size} limit={args.limit}")

    summary = await _run_backfill(
        dsn,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        limit=args.limit,
    )
    _print_summary(summary)
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
