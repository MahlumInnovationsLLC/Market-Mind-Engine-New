"""One-shot CLI: drain the task #757 retention backlog.

When the operator turns on the new retention loops on a long-lived
deployment for the first time, the in-process loops will work through
the backlog at one chunk per tick — that's the right behaviour at
steady state but it can take days to reclaim a multi-million-row
backlog.

This script does the same archive-then-delete sweep the in-process
loops do, just synchronously, and over a longer per-table budget so a
manual sweep can drain the backlog in a single run.

Usage::

    python -m scripts.archive_and_prune_backlog --dry-run
    python -m scripts.archive_and_prune_backlog --table predictions
    python -m scripts.archive_and_prune_backlog --max-rows 1000000

Without ``--table`` the script iterates *every* loop registered in
:mod:`app.retention_loops_v757` (both archive and delete-only).  With
``--table`` it runs just the matching one.

``--dry-run`` is the default — pass ``--apply`` to actually upload /
delete.  Without ``--apply`` the script:

* For archive loops: counts rows per table that *would* move and
  reports the size, but uploads nothing and deletes nothing.
* For delete-only loops: counts rows per table that *would* be
  deleted via a ``SELECT count(*)`` and deletes nothing.

``--max-rows`` (default unlimited for archive loops, applied per
table) caps the per-table sweep so the operator can do incremental
drain runs.

``DATABASE_URL`` is the only required input; storage creds come from
the same place :mod:`app.storage` already reads them.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# Allow running as both ``python scripts/archive_and_prune_backlog.py``
# (from the api-server dir) and ``python -m scripts.archive_and_prune_backlog``.
_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app import (  # noqa: E402
    archive_helper,
    db,
    retention_loops_v757,
)
from app.retention_base import (  # noqa: E402
    ArchiveThenDeleteRetention,
    DeleteOnlyRetention,
)


def _log(msg: str) -> None:
    print(f"[archive_and_prune_backlog] {msg}", flush=True)


def _resolve_loops(
    table_filter: str | None,
) -> list[Any]:
    modules = list(retention_loops_v757.ALL_MODULES)
    if not table_filter:
        return modules
    out = []
    wanted = table_filter.strip().lower()
    for module in modules:
        snap = module.state()
        if (snap.get("table") or "").lower() == wanted or (
            snap.get("name") or ""
        ).lower() == wanted:
            out.append(module)
    if not out:
        raise SystemExit(
            f"no retention loop matches --table {table_filter!r}; "
            f"known tables: "
            + ", ".join(sorted(m.state().get("table", "?") for m in modules))
        )
    return out


def _underlying(module: Any) -> Any:
    """Reach the per-loop ``_BaseRetention`` instance."""
    return module._loop  # noqa: SLF001 — module-level shim, intentional


async def _backlog_count(loop: Any) -> int:
    """Count rows currently older than the loop's resolved cutoff."""
    qtable = archive_helper._quote_ident(loop.table)
    qts = archive_helper._quote_ident(loop.ts_column)
    cutoff = loop.cutoff()
    return int(
        await db.fetchval(
            f"SELECT count(*) FROM {qtable} WHERE {qts} < $1", cutoff
        )
        or 0
    )


async def _run_archive(
    module: Any,
    *,
    apply: bool,
    max_rows: int | None,
) -> dict[str, Any]:
    loop: ArchiveThenDeleteRetention = _underlying(module)
    cutoff = loop.cutoff()
    backlog = await _backlog_count(loop)
    _log(
        f"archive  {loop.name:<40} backlog={backlog:>10} "
        f"cutoff={cutoff.isoformat()}"
    )
    if not apply:
        return {
            "name": loop.name,
            "table": loop.table,
            "flavor": "archive",
            "backlog": backlog,
            "moved": 0,
            "applied": False,
        }
    summary = await archive_helper.archive_table(
        table=loop.table,
        ts_column=loop.ts_column,
        cutoff=cutoff,
        pk_columns=loop.pk_columns,
        chunk_size=loop.chunk_size,
        max_rows=max_rows if max_rows is not None else loop.max_rows_per_run,
    )
    _log(
        f"archive  {loop.name:<40} moved={summary.rows} chunks={summary.chunks} "
        f"bytes_compressed={summary.bytes_compressed} error={summary.error!r}"
    )
    return {
        "name": loop.name,
        "table": loop.table,
        "flavor": "archive",
        "backlog": backlog,
        "moved": summary.rows,
        "chunks": summary.chunks,
        "bytes_compressed": summary.bytes_compressed,
        "error": summary.error,
        "applied": True,
    }


async def _run_delete_only(
    module: Any,
    *,
    apply: bool,
    max_rows: int | None,  # noqa: ARG001 — unused for delete-only sweeps
) -> dict[str, Any]:
    loop: DeleteOnlyRetention = _underlying(module)
    backlog = await _backlog_count(loop)
    cutoff = loop.cutoff()
    _log(
        f"delete   {loop.name:<40} backlog={backlog:>10} "
        f"cutoff={cutoff.isoformat()}"
    )
    if not apply:
        return {
            "name": loop.name,
            "table": loop.table,
            "flavor": "delete-only",
            "backlog": backlog,
            "moved": 0,
            "applied": False,
        }
    deleted = await loop.cleanup_once()
    _log(f"delete   {loop.name:<40} deleted={deleted}")
    return {
        "name": loop.name,
        "table": loop.table,
        "flavor": "delete-only",
        "backlog": backlog,
        "moved": deleted,
        "applied": True,
    }


async def _amain(args: argparse.Namespace) -> int:
    await db.get_pool()
    try:
        modules = _resolve_loops(args.table)
        results: list[dict[str, Any]] = []
        for module in modules:
            loop = _underlying(module)
            try:
                if isinstance(loop, ArchiveThenDeleteRetention):
                    result = await _run_archive(
                        module, apply=args.apply, max_rows=args.max_rows,
                    )
                elif isinstance(loop, DeleteOnlyRetention):
                    result = await _run_delete_only(
                        module, apply=args.apply, max_rows=args.max_rows,
                    )
                else:
                    _log(f"skip     {loop.name}: unknown loop class")
                    continue
            except Exception as e:  # noqa: BLE001 — counted, surfaced, continue
                _log(f"FAIL     {loop.name}: {type(e).__name__}: {e}")
                results.append(
                    {
                        "name": loop.name,
                        "table": loop.table,
                        "flavor": getattr(loop, "flavor", "?"),
                        "error": f"{type(e).__name__}: {e}",
                        "applied": False,
                    }
                )
                continue
            results.append(result)

        # Tally
        total_backlog = sum(int(r.get("backlog", 0) or 0) for r in results)
        total_moved = sum(int(r.get("moved", 0) or 0) for r in results)
        n_failed = sum(1 for r in results if r.get("error"))
        _log("")
        _log(
            f"DONE   loops={len(results)} backlog_total={total_backlog} "
            f"moved_total={total_moved} failed={n_failed} "
            f"mode={'apply' if args.apply else 'dry-run'}"
        )
        return 1 if n_failed else 0
    finally:
        await db.close_pool()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--table",
        default=None,
        help="Restrict to a single table (or loop name). Omit to iterate all.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually upload + delete. Default is dry-run.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run flag; the default is also dry-run, kept "
             "for symmetry with --apply.",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Cap rows moved per table; omit for the helper's default.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if args.dry_run and args.apply:
        raise SystemExit("--dry-run and --apply are mutually exclusive")
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
