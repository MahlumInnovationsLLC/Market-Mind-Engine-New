"""Shared "archive then prune" helper for the retention loops (task #757).

Given a table name, a timestamp column, and a cutoff timestamp, this
helper streams rows older than the cutoff out of Postgres in chunks,
writes each chunk as a gzip-compressed JSONL file into the existing
Supabase Storage bucket under

    archives/<table>/dt=YYYY-MM-DD/part-<n>.jsonl.gz

…and then deletes the matching rows.  The upload-then-delete pair for
each chunk is its own transaction, so a process kill or DB blip
mid-run leaves a consistent picture: every byte that hit the bucket
is also gone from Postgres, and every row still in Postgres has not
yet been claimed by an archive object.

A small JSON sidecar (``part-<n>.manifest.json``) is written next to
each chunk recording the row count, byte sizes, cutoff, and the
chunk's row PKs so an operator can audit later — see
``docs/retention_runbook.md`` for the re-import recipe.

The helper is intentionally framework-agnostic: it accepts a "fetch /
delete" pair through dependency-injected callables so tests can drive
it against an in-memory fake without ever touching ``app.db`` or
``app.storage``.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Sequence

from . import db, storage


log = logging.getLogger("axiom.archive_helper")


# Default upload chunk size.  10k rows × the typical narrow event row
# is well under 10 MiB compressed and well under one Supabase Storage
# request's hard cap, while still keeping the per-tick overhead low.
DEFAULT_CHUNK_SIZE = 10_000

# Hard cap on rows archived per ``archive_table`` invocation so a
# pathological backlog can't lock the loop on a single tick — a
# follow-up tick will pick up the rest.
DEFAULT_MAX_ROWS_PER_RUN = 100_000


# ---------------------------------------------------------------------------
# Pluggable IO contracts
# ---------------------------------------------------------------------------

# Returns the list of rows to archive next.  The implementation is
# expected to ORDER BY the timestamp column ASCENDING and LIMIT to
# ``chunk_size`` so the "oldest first" invariant holds.  Each row is
# returned as a mapping (asyncpg.Record satisfies this).
RowFetcher = Callable[[int], Awaitable[Sequence[Any]]]

# Deletes the rows we just archived.  Receives the iterable of primary
# keys we extracted from the fetched chunk and returns the row count
# the DB reports.
RowDeleter = Callable[[Sequence[Any]], Awaitable[int]]

# Persists ``payload`` to ``object_key`` and returns the upload result
# dict.  Defaults to ``app.storage.upload_bytes``.
Uploader = Callable[[str, bytes], Awaitable[dict[str, Any]]]


@dataclass
class ArchiveRunSummary:
    """Outcome of a single ``archive_table`` invocation."""

    table: str
    rows: int = 0
    chunks: int = 0
    bytes_compressed: int = 0
    bytes_uncompressed: int = 0
    object_keys: list[str] = field(default_factory=list)
    last_object_key: str | None = None
    cutoff: str | None = None
    duration_s: float = 0.0
    error: str | None = None


def _row_to_jsonable(row: Any) -> dict[str, Any]:
    """Best-effort conversion of an asyncpg-style record to a JSON dict.

    ``asyncpg.Record`` exposes a ``dict()``-like view via ``dict(row)``;
    plain dicts pass through.  Datetimes / dates / Decimals are
    serialised via ``default=_default`` below.
    """
    if hasattr(row, "keys") and hasattr(row, "__getitem__"):
        return {k: row[k] for k in row.keys()}
    if isinstance(row, dict):
        return dict(row)
    raise TypeError(f"unsupported row type for archive: {type(row).__name__}")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        # Raw bytes columns get base64-armored so the JSONL round-trip
        # is lossless; the operator restore script uses base64.b64decode.
        import base64

        return {"__bytes_b64__": base64.b64encode(obj).decode("ascii")}
    # asyncpg returns Decimals for numeric columns; Decimal isn't
    # JSON-serialisable but a string round-trips cleanly.
    if hasattr(obj, "__class__") and obj.__class__.__name__ == "Decimal":
        return str(obj)
    raise TypeError(f"not JSON-serialisable: {type(obj).__name__}")


def encode_chunk(rows: Sequence[Any]) -> tuple[bytes, int]:
    """Encode rows as gzip-compressed JSONL.

    Returns ``(compressed_bytes, uncompressed_size)``.  Pulled out so
    tests can exercise the format without any network or DB.
    """
    buf = bytearray()
    for row in rows:
        d = _row_to_jsonable(row)
        line = json.dumps(d, default=_json_default, separators=(",", ":"))
        buf.extend(line.encode("utf-8"))
        buf.append(0x0A)  # \n
    uncompressed_size = len(buf)
    compressed = gzip.compress(bytes(buf), compresslevel=6)
    return compressed, uncompressed_size


def _archive_prefix(table: str, when: datetime | None = None) -> str:
    """``archives/<table>/dt=YYYY-MM-DD`` (UTC date partition)."""
    when = when or datetime.now(timezone.utc)
    return f"archives/{table}/dt={when.strftime('%Y-%m-%d')}"


async def _default_uploader(key: str, payload: bytes) -> dict[str, Any]:
    return await storage.upload_bytes(
        key, payload, content_type="application/gzip", upsert=True,
    )


async def _default_manifest_writer(key: str, payload: bytes) -> dict[str, Any]:
    return await storage.upload_bytes(
        key, payload, content_type="application/json", upsert=True,
    )


async def archive_table(
    *,
    table: str,
    ts_column: str,
    cutoff: datetime,
    pk_columns: Sequence[str] = ("id",),
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_rows: int | None = DEFAULT_MAX_ROWS_PER_RUN,
    fetcher: RowFetcher | None = None,
    deleter: RowDeleter | None = None,
    uploader: Uploader | None = None,
    manifest_writer: Uploader | None = None,
    now: Callable[[], datetime] | None = None,
    progress_callback: Callable[["ArchiveRunSummary"], None] | None = None,
) -> ArchiveRunSummary:
    """Archive then delete rows older than ``cutoff`` from ``table``.

    Behaviour:
      1. Fetch the next ``chunk_size`` rows (oldest first) where
         ``<ts_column> < cutoff``.
      2. If empty, stop.
      3. Encode as gzip JSONL, upload to
         ``archives/<table>/dt=YYYY-MM-DD/part-<n>.jsonl.gz``.
      4. Write a ``part-<n>.manifest.json`` sidecar.
      5. DELETE the rows by primary-key tuple.
      6. Repeat until either no more rows match or ``max_rows`` is hit.

    Each archive operation is its own transaction (the upload happens
    first, then the DELETE).  An interrupted run therefore leaves at
    most one duplicate object for the in-flight chunk; the next run
    re-uploads it (``upsert=True``) and proceeds.

    For pre-flight "what *would* move" reporting, callers should use
    a SELECT count(*) against the same cutoff instead — see
    ``scripts/archive_and_prune_backlog.py`` for the operator-facing
    wrapper that does both halves.

    Returns an :class:`ArchiveRunSummary` describing the run.
    """
    if not pk_columns:
        raise ValueError("pk_columns must be non-empty")

    if fetcher is None:
        fetcher = _make_default_fetcher(table, ts_column, cutoff, pk_columns)
    if deleter is None:
        deleter = _make_default_deleter(table, pk_columns)
    if uploader is None:
        uploader = _default_uploader
    if manifest_writer is None:
        manifest_writer = _default_manifest_writer

    summary = ArchiveRunSummary(
        table=table, cutoff=cutoff.isoformat()
    )

    started = time.monotonic()
    now_fn = now or (lambda: datetime.now(timezone.utc))
    started_at = now_fn()

    prefix = _archive_prefix(table, started_at)
    chunk_index = 0
    remaining_budget: int | None = max_rows

    try:
        while True:
            # Cap this chunk so we never overshoot the run-wide budget.
            this_chunk = (
                chunk_size if remaining_budget is None
                else min(chunk_size, max(0, remaining_budget))
            )
            if this_chunk == 0:
                break

            rows = await fetcher(this_chunk)
            if not rows:
                break

            compressed, uncompressed = encode_chunk(rows)

            object_key = f"{prefix}/part-{chunk_index:04d}.jsonl.gz"
            manifest_key = f"{prefix}/part-{chunk_index:04d}.manifest.json"

            # Upload data first; only after success do we delete the
            # rows.  An exception here aborts the run with a clean
            # rollback (no rows lost).
            await uploader(object_key, compressed)

            manifest = {
                "table": table,
                "ts_column": ts_column,
                "cutoff": cutoff.isoformat(),
                "rows": len(rows),
                "bytes_compressed": len(compressed),
                "bytes_uncompressed": uncompressed,
                "chunk_index": chunk_index,
                "object_key": object_key,
                "pk_columns": list(pk_columns),
                "started_at": started_at.isoformat(),
                "finished_at": now_fn().isoformat(),
            }
            await manifest_writer(
                manifest_key,
                json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
            )

            pks = [_extract_pk(row, pk_columns) for row in rows]
            deleted = await deleter(pks)
            if deleted != len(rows):
                # Should not happen with a healthy DB; log so a
                # missing-row condition is visible without aborting
                # the run.
                log.warning(
                    "archive_helper: %s chunk %s archived %s rows but "
                    "DB reported %s deleted",
                    table, chunk_index, len(rows), deleted,
                )

            summary.rows += len(rows)
            summary.chunks += 1
            summary.bytes_compressed += len(compressed)
            summary.bytes_uncompressed += uncompressed
            summary.object_keys.append(object_key)
            summary.last_object_key = object_key

            if progress_callback is not None:
                try:
                    progress_callback(summary)
                except Exception as cb_err:  # noqa: BLE001 — observability hook
                    log.warning(
                        "archive_helper: progress callback raised: %s", cb_err,
                    )

            if remaining_budget is not None:
                remaining_budget -= len(rows)
                if remaining_budget <= 0:
                    break

            # Short-circuit when the table returned a partial chunk —
            # we've consumed the tail and the next fetch would return
            # zero rows anyway.
            if len(rows) < this_chunk:
                break

            chunk_index += 1
    except Exception as exc:  # noqa: BLE001 — surfaced to caller via summary
        summary.error = f"{type(exc).__name__}: {exc}"
        log.warning("archive_helper: %s aborted: %s", table, summary.error)
    finally:
        summary.duration_s = time.monotonic() - started

    return summary


_IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(name: str) -> str:
    """Safely quote a SQL identifier.

    Per-table modules pass bare column names (``"created_at"``,
    ``"timestamp"``, ``"event_id"``).  Anything that isn't a plain
    ``[A-Za-z_][A-Za-z0-9_]*`` identifier is wrapped in double quotes —
    most importantly ``timestamp``, which is a typed-literal keyword in
    Postgres and breaks bare in ORDER BY clauses.  Embedded ``"`` are
    doubled per the SQL spec.
    """
    if _IDENT_RE.match(name) and name.lower() not in _RESERVED_IDENTS:
        return name
    return '"' + name.replace('"', '""') + '"'


# Postgres reserved / non-reserved-but-needs-quoting words we run into
# in archived tables.  Keep this list tiny and targeted; we'd rather
# over-quote a few names than miss one and break a sweep.
_RESERVED_IDENTS = frozenset({"timestamp", "user", "order", "table"})


def _extract_pk(row: Any, pk_columns: Sequence[str]) -> tuple[Any, ...]:
    """Read the PK tuple from a row.

    asyncpg keys records by *unquoted* column names, so we must NOT
    pass a quoted identifier (``'"timestamp"'``) here — strip the
    quotes back out for the row lookup.
    """
    return tuple(row[_unquote_ident(col)] for col in pk_columns)


def _unquote_ident(name: str) -> str:
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return name[1:-1].replace('""', '"')
    return name


def _make_default_fetcher(
    table: str, ts_column: str, cutoff: datetime, pk_columns: Sequence[str],
) -> RowFetcher:
    """Default fetcher that pages through ``app.db``.

    Selects the oldest ``limit`` rows whose ``ts_column`` is strictly
    older than ``cutoff``.  We always re-issue the WHERE clause rather
    than tracking a cursor so a partially deleted table self-heals.
    """
    # Identifiers are quoted; values stay parameterised.
    qtable = _quote_ident(table)
    qts = _quote_ident(ts_column)
    select_sql = (
        f"SELECT * FROM {qtable} "
        f"WHERE {qts} < $1 "
        f"ORDER BY {qts} ASC "
        f"LIMIT $2"
    )

    async def _fetch(limit: int) -> Sequence[Any]:
        return await db.fetch(select_sql, cutoff, limit)

    return _fetch


def _make_default_deleter(
    table: str, pk_columns: Sequence[str],
) -> RowDeleter:
    """Default deleter that removes rows by primary-key tuple."""

    qtable = _quote_ident(table)

    if len(pk_columns) == 1:
        qcol = _quote_ident(pk_columns[0])
        delete_sql = f"DELETE FROM {qtable} WHERE {qcol} = ANY($1::text[])"

        async def _delete(pks: Sequence[Any]) -> int:
            # Cast every key to string so we can ride a single
            # parameter regardless of underlying type (bigint, text,
            # uuid).  asyncpg will route via ``::text[]``.
            keys = [str(_unwrap_pk(pk)) for pk in pks]
            status = await db.execute(delete_sql, keys)
            return _parse_delete_status(status)

        return _delete

    # Composite PK: delete one row at a time inside a single
    # transaction.
    where = " AND ".join(
        f"{_quote_ident(col)} = ${i + 1}" for i, col in enumerate(pk_columns)
    )
    delete_sql = f"DELETE FROM {qtable} WHERE {where}"

    async def _delete_composite(pks: Sequence[Any]) -> int:
        deleted = 0
        async with db.acquire() as conn, conn.transaction():
            for pk in pks:
                status = await conn.execute(delete_sql, *pk)
                deleted += _parse_delete_status(status)
        return deleted

    return _delete_composite


def _unwrap_pk(pk: Any) -> Any:
    """Tuple-of-one PKs come back as 1-tuples from ``_extract_pk``."""
    if isinstance(pk, tuple) and len(pk) == 1:
        return pk[0]
    return pk


def _parse_delete_status(status: str) -> int:
    """asyncpg returns ``"DELETE <n>"``; pull <n> back out, default 0."""
    try:
        parts = (status or "").split()
        if len(parts) >= 2 and parts[0].upper() == "DELETE":
            return int(parts[1])
    except (ValueError, AttributeError):
        pass
    return 0
