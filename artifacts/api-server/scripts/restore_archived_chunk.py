"""One-shot CLI: re-import an archived chunk back into Postgres.

Task #757 archives rows out of Postgres into Supabase Storage as gzip
JSONL under ``archives/<table>/dt=YYYY-MM-DD/part-<n>.jsonl.gz`` plus a
sibling ``.manifest.json`` sidecar.  ``docs/retention_runbook.md``
documents the manual restore recipe; this script makes the round-trip
a one-liner so audits and backtests don't need a per-table Python
script.

Usage::

    # Single object, dry-run (default).
    python -m scripts.restore_archived_chunk \\
        --object-key archives/predictions/dt=2026-04-22/part-0000.jsonl.gz

    # Every chunk under a date partition (or any prefix), apply.
    python -m scripts.restore_archived_chunk \\
        --prefix archives/predictions/dt=2026-04-22/ --apply

    # Override the destination table when the object lives outside the
    # canonical archives/<table>/... layout.
    python -m scripts.restore_archived_chunk \\
        --object-key path/to/part-0000.jsonl.gz --table predictions --apply

Behaviour:
  * Datetimes (ISO-8601 strings), ``Decimal`` (numeric strings) and raw
    ``bytes`` (``{"__bytes_b64__": "..."}`` envelopes) round-trip
    losslessly via :func:`decode_row` — the inverse of
    :func:`app.archive_helper._json_default`.
  * Inserts honour ``ON CONFLICT DO NOTHING`` so re-running against a
    table that already has the rows is a no-op (no row count growth).
  * ``--dry-run`` (the default) reports per-object row counts and
    sample keys without writing.  ``--apply`` performs the inserts.

``DATABASE_URL`` is the only required input; storage creds come from
the same env :mod:`app.storage` already reads.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

# Allow running as both ``python scripts/restore_archived_chunk.py``
# (from the api-server dir) and ``python -m scripts.restore_archived_chunk``.
_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app import archive_helper, db, storage  # noqa: E402


def _log(msg: str) -> None:
    print(f"[restore_archived_chunk] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Decode helpers — inverse of ``app.archive_helper._json_default``.
# ---------------------------------------------------------------------------

# Postgres data_type values that should be parsed back into ``datetime``
# / ``date`` / ``Decimal`` instead of left as strings.  We rely on
# information_schema.columns.data_type which is stable across PG
# versions.
_TIMESTAMP_TYPES = frozenset({
    "timestamp with time zone",
    "timestamp without time zone",
})
_DATE_TYPES = frozenset({"date"})
_DECIMAL_TYPES = frozenset({"numeric", "decimal"})


def _maybe_bytes(value: Any) -> Any:
    """Unwrap a ``{"__bytes_b64__": "<base64>"}`` envelope."""
    if isinstance(value, dict) and set(value.keys()) == {"__bytes_b64__"}:
        return base64.b64decode(value["__bytes_b64__"])
    return value


def _parse_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    # ``datetime.fromisoformat`` handles the format
    # ``_json_default`` emits (``isoformat()``) on Python 3.11+.
    return datetime.fromisoformat(value)


def _parse_date(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return date.fromisoformat(value)


def _parse_decimal(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    return value


def decode_row(
    row: dict[str, Any],
    column_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return ``row`` with archive envelopes decoded back to Python types.

    ``column_types`` maps column name to ``information_schema.columns.data_type``;
    when supplied, datetime/date/decimal strings are parsed back into
    the matching Python type so asyncpg can bind them as the right
    parameter type.  Without ``column_types`` only the bytes envelope
    is unwrapped (the JSON primitives carry through unchanged), which
    is sufficient for callers that only need the raw payload.
    """
    out: dict[str, Any] = {}
    types = column_types or {}
    for k, v in row.items():
        v = _maybe_bytes(v)
        col_type = types.get(k)
        if col_type in _TIMESTAMP_TYPES:
            v = _parse_datetime(v)
        elif col_type in _DATE_TYPES:
            v = _parse_date(v)
        elif col_type in _DECIMAL_TYPES:
            v = _parse_decimal(v)
        out[k] = v
    return out


def decode_jsonl_gz(payload: bytes) -> list[dict[str, Any]]:
    """Decode a gzip-JSONL chunk into a list of raw row dicts.

    Bytes envelopes are unwrapped; datetimes / decimals are left as
    strings (the caller pairs them with column types via
    :func:`decode_row` before binding to asyncpg).
    """
    text = gzip.decompress(payload).decode("utf-8")
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        rows.append(decode_row(json.loads(line), column_types=None))
    return rows


# ---------------------------------------------------------------------------
# Object resolution
# ---------------------------------------------------------------------------

# Canonical layout: ``archives/<table>/dt=YYYY-MM-DD/part-<n>.jsonl.gz``.
_ARCHIVE_PATH_RE = re.compile(
    r"^archives/(?P<table>[^/]+)/dt=\d{4}-\d{2}-\d{2}/part-\d+\.jsonl\.gz$"
)


def infer_table_from_key(object_key: str) -> str | None:
    """Pull ``<table>`` out of the canonical archive path layout.

    Returns ``None`` when the key doesn't match — the caller must then
    require ``--table`` on the command line.
    """
    m = _ARCHIVE_PATH_RE.match(object_key)
    return m.group("table") if m else None


# Pluggable lister/downloader so tests can drive the script with an
# in-memory bucket (mirrors the pattern used in ``archive_helper``).
ObjectLister = Callable[[str], Awaitable[Sequence[str]]]
ObjectDownloader = Callable[[str], Awaitable[bytes]]


async def _default_lister(prefix: str) -> Sequence[str]:
    """List ``part-*.jsonl.gz`` keys under ``prefix`` (one folder deep).

    Manifest sidecars and other non-data objects are filtered out so a
    plain ``--prefix archives/<table>/dt=.../`` does the right thing.
    """
    folder = prefix.rstrip("/")
    entries = await storage.list_path(folder, limit=1000)
    if len(entries) >= 1000:
        raise RuntimeError(
            f"restore aborted: prefix {prefix!r} returned >=1000 entries "
            "(paging not implemented here — narrow the prefix)"
        )
    out: list[str] = []
    for e in entries:
        if e.is_folder:
            continue
        if not e.name.endswith(".jsonl.gz"):
            continue
        # ``list_path`` returns names relative to the prefix folder.
        out.append(f"{folder}/{e.name}" if folder else e.name)
    return sorted(out)


async def _default_downloader(object_key: str) -> bytes:
    """Stream ``object_key`` to a tmpfile and read it back into memory.

    Reuses ``storage.download_to_path`` so we get the same retry/SSL
    posture as the archive helper.  Chunks are bounded by
    ``DEFAULT_CHUNK_SIZE`` (10k rows) so even a fully packed chunk
    decompresses to a few MiB — comfortably in-memory.
    """
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "chunk.jsonl.gz"
        await storage.download_to_path(object_key, local, force=True)
        return local.read_bytes()


# ---------------------------------------------------------------------------
# DB plumbing
# ---------------------------------------------------------------------------

# Returns ``{column_name: data_type}`` for the destination table.
ColumnTyper = Callable[[str], Awaitable[dict[str, str]]]

# Inserts a batch of rows, returns the number of rows actually inserted
# (i.e. *not* skipped by ``ON CONFLICT DO NOTHING``).  Receives the
# table name, the ordered list of column names (matching the row dicts'
# keys), and the row payloads as a list of value tuples.
RowInserter = Callable[[str, list[str], list[tuple[Any, ...]]], Awaitable[int]]


async def _default_column_typer(table: str) -> dict[str, str]:
    qtable = archive_helper._quote_ident(table)  # validation only
    del qtable  # parameterised below — quote was only to bail on bad names
    rows = await db.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {r["column_name"]: r["data_type"] for r in rows}


def _make_default_inserter() -> RowInserter:
    async def _insert(
        table: str, columns: list[str], values: list[tuple[Any, ...]],
    ) -> int:
        if not values:
            return 0
        qtable = archive_helper._quote_ident(table)
        qcols = ", ".join(archive_helper._quote_ident(c) for c in columns)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
        sql = (
            f"INSERT INTO {qtable} ({qcols}) VALUES ({placeholders}) "
            f"ON CONFLICT DO NOTHING"
        )
        # Use a single transaction + executemany so the whole batch is
        # one round-trip.  asyncpg's executemany doesn't return a row
        # count, so we count via "after - before" on the table size...
        # which would race against concurrent writers.  Instead, run
        # one row at a time and parse the ``INSERT 0 <n>`` status —
        # ``<n>`` is 0 when the conflict path fires and 1 otherwise.
        inserted = 0
        async with db.acquire() as conn, conn.transaction():
            for row_values in values:
                status = await conn.execute(sql, *row_values)
                inserted += _parse_insert_status(status)
        return inserted

    return _insert


def _parse_insert_status(status: str) -> int:
    """asyncpg returns ``"INSERT <oid> <n>"``; pull <n>, default 0."""
    try:
        parts = (status or "").split()
        if len(parts) >= 3 and parts[0].upper() == "INSERT":
            return int(parts[2])
    except (ValueError, AttributeError):
        pass
    return 0


# ---------------------------------------------------------------------------
# Restore driver
# ---------------------------------------------------------------------------

@dataclass
class RestoreObjectResult:
    object_key: str
    rows_in_chunk: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0  # ON CONFLICT DO NOTHING victims
    applied: bool = False
    error: str | None = None


@dataclass
class RestoreRunSummary:
    table: str | None = None
    objects: list[RestoreObjectResult] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(o.rows_in_chunk for o in self.objects)

    @property
    def total_inserted(self) -> int:
        return sum(o.rows_inserted for o in self.objects)

    @property
    def total_skipped(self) -> int:
        return sum(o.rows_skipped for o in self.objects)

    @property
    def n_failed(self) -> int:
        return sum(1 for o in self.objects if o.error)


async def restore_one_object(
    *,
    object_key: str,
    table: str,
    apply: bool,
    column_typer: ColumnTyper,
    downloader: ObjectDownloader,
    inserter: RowInserter,
) -> RestoreObjectResult:
    """Restore a single ``part-<n>.jsonl.gz`` into ``table``."""
    result = RestoreObjectResult(object_key=object_key)
    try:
        payload = await downloader(object_key)
        raw_rows = decode_jsonl_gz(payload)
        result.rows_in_chunk = len(raw_rows)

        if not raw_rows:
            result.applied = apply
            return result

        column_types = await column_typer(table)
        # Column order is taken from the first row so we honour the
        # archive's on-disk ordering; subsequent rows are projected to
        # match.  (Every row in a chunk shares the same set of keys
        # because they came out of one SELECT * — but be defensive.)
        columns = list(raw_rows[0].keys())
        unknown = [c for c in columns if c not in column_types]
        if unknown:
            raise RuntimeError(
                f"chunk references columns not present in {table!r}: "
                f"{sorted(unknown)}"
            )

        decoded = [
            decode_row(r, column_types=column_types) for r in raw_rows
        ]
        values = [tuple(r.get(c) for c in columns) for r in decoded]

        if not apply:
            return result  # dry-run: counted only

        inserted = await inserter(table, columns, values)
        result.rows_inserted = inserted
        result.rows_skipped = result.rows_in_chunk - inserted
        result.applied = True
    except Exception as exc:  # noqa: BLE001 — surfaced via summary
        result.error = f"{type(exc).__name__}: {exc}"
    return result


async def restore_objects(
    *,
    object_keys: Sequence[str],
    table: str,
    apply: bool,
    column_typer: ColumnTyper | None = None,
    downloader: ObjectDownloader | None = None,
    inserter: RowInserter | None = None,
) -> RestoreRunSummary:
    column_typer = column_typer or _default_column_typer
    downloader = downloader or _default_downloader
    inserter = inserter or _make_default_inserter()

    summary = RestoreRunSummary(table=table)
    for key in object_keys:
        _log(f"restore  {key}")
        result = await restore_one_object(
            object_key=key,
            table=table,
            apply=apply,
            column_typer=column_typer,
            downloader=downloader,
            inserter=inserter,
        )
        if result.error:
            _log(f"FAIL     {key}: {result.error}")
        else:
            _log(
                f"        rows={result.rows_in_chunk} "
                f"inserted={result.rows_inserted} "
                f"skipped={result.rows_skipped} applied={result.applied}"
            )
        summary.objects.append(result)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _resolve_object_keys(
    *,
    object_key: str | None,
    prefix: str | None,
    lister: ObjectLister | None = None,
) -> list[str]:
    if object_key and prefix:
        raise SystemExit("--object-key and --prefix are mutually exclusive")
    if not object_key and not prefix:
        raise SystemExit("one of --object-key or --prefix is required")
    if object_key:
        return [object_key]
    lister = lister or _default_lister
    keys = list(await lister(prefix or ""))
    if not keys:
        raise SystemExit(f"no part-*.jsonl.gz objects found under {prefix!r}")
    return keys


def _resolve_table(args: argparse.Namespace, keys: Sequence[str]) -> str:
    if args.table:
        return args.table
    inferred = {infer_table_from_key(k) for k in keys}
    inferred.discard(None)
    if len(inferred) == 1:
        return inferred.pop()  # type: ignore[return-value]
    if not inferred:
        raise SystemExit(
            "could not infer destination table from object keys; pass --table"
        )
    raise SystemExit(
        "object keys span multiple tables "
        f"({sorted(inferred)}); restore one table at a time or pass --table"
    )


async def _amain(args: argparse.Namespace) -> int:
    await db.get_pool()
    try:
        keys = await _resolve_object_keys(
            object_key=args.object_key, prefix=args.prefix,
        )
        table = _resolve_table(args, keys)
        _log(
            f"plan    table={table} objects={len(keys)} "
            f"mode={'apply' if args.apply else 'dry-run'}"
        )
        summary = await restore_objects(
            object_keys=keys, table=table, apply=args.apply,
        )
        _log("")
        _log(
            f"DONE   table={summary.table} objects={len(summary.objects)} "
            f"rows_total={summary.total_rows} "
            f"inserted_total={summary.total_inserted} "
            f"skipped_total={summary.total_skipped} "
            f"failed={summary.n_failed} "
            f"mode={'apply' if args.apply else 'dry-run'}"
        )
        return 1 if summary.n_failed else 0
    finally:
        await db.close_pool()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_argument_group("source")
    src.add_argument(
        "--object-key",
        default=None,
        help="Single object to restore, e.g. "
             "archives/predictions/dt=2026-04-22/part-0000.jsonl.gz",
    )
    src.add_argument(
        "--prefix",
        default=None,
        help="Restore every part-*.jsonl.gz under this prefix, e.g. "
             "archives/predictions/dt=2026-04-22/",
    )
    p.add_argument(
        "--table",
        default=None,
        help="Destination table. Inferred from canonical "
             "archives/<table>/... keys when omitted.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually insert. Default is dry-run.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run flag; the default is also dry-run, kept "
             "for symmetry with --apply.",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
