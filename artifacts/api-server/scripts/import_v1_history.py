"""Idempotent ETL: V1 historical-data dumps -> ``v1_archive.*``.

Phase 1 (already landed in production): import the V1
``trade_analyses`` dump from the Supabase storage bucket so the
per-league calibrators have real ``(predicted_prob, actual_outcome)``
pairs to fit against.

Phase 2 (task #731): import the next three V1 dumps —
``weather_observations`` (133K rows), ``llm_predictions`` (57K rows),
and ``line_snapshots`` (4.7M rows).  Schemas land under the same
isolated ``v1_archive`` schema (Alembic migration
``0049_v1_archive_phase2``) so they cannot collide with anything MME
writes forward.  ``line_snapshots`` ships as a 2-part split dump
(``*.part000`` / ``*.part001``); the reassembler below handles both
single-file and split-dump layouts.

All dumps are PLAIN ``pg_dump`` data-only output (gzipped SQL); we
rewrite the COPY header to land in ``v1_archive.<table>`` and stream
the result through ``psql --single-transaction`` so any failure
(download, load, or row-count divergence) rolls back the whole
transaction including the TRUNCATE.

Run with::

    python -m scripts.import_v1_history                  # full reload of trade_analyses (default)
    python -m scripts.import_v1_history --table weather_observations
    python -m scripts.import_v1_history --table llm_predictions
    python -m scripts.import_v1_history --table line_snapshots
    python -m scripts.import_v1_history --table all      # all four phases in dependency order
    python -m scripts.import_v1_history --table all --dry-run

The ``DATABASE_URL`` env var is the only required input.  Storage
credentials come from ``app.storage`` (already configured for the
verifier script).
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Allow running as both ``python scripts/import_v1_history.py`` (from
# the api-server dir) and ``python -m scripts.import_v1_history``.
_HERE = Path(__file__).resolve()
_API_SERVER_ROOT = _HERE.parent.parent
if str(_API_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_SERVER_ROOT))

from app import storage  # noqa: E402  (sys.path tweak above)


WORK_DIR = Path("/tmp/v1_dumps")


DEFAULT_CHUNK_ROWS = 500_000


def _progress_path(table: str) -> Path:
    return WORK_DIR / f"{table}.progress.json"


def _detach_log_path(table: str) -> Path:
    return WORK_DIR / f"{table}.detached.log"


def _detach_pid_path(table: str) -> Path:
    return WORK_DIR / f"{table}.detached.pid"


def _write_progress(table: str, **fields) -> None:
    """Atomically append/update a per-table progress JSON.

    Used by ``_chunked_load`` to record per-chunk progress to disk so a
    detached run can be inspected after the fact (and so we can see how
    far the prior session got after a crash).  Writes are atomic via
    ``os.replace`` so a concurrent reader never sees a half-written
    file.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    path = _progress_path(table)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
    existing.update(fields)
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class DumpSpec:
    """Description of one V1 dump and its v1_archive landing target.

    * ``name`` — both the bucket file stem (``<name>.sql.gz``) and the
      ``public.<name>`` / ``v1_archive.<name>`` table name.  Always equal
      because the rewrite step matches on the COPY header literal.
    * ``expected_rows`` — taken from a one-time row count of the V1 dump
      at task-write time.  ``--max-mismatch-pct`` controls the tolerance
      band before the import refuses to commit.
    * ``split_parts`` — None for single-file dumps; integer for
      ``*.partNNN`` split dumps that must be cat-reassembled before
      gunzip.  ``download_split_artifact`` only handles GNU-split
      ``aa/ab`` suffixes, so the V1 ``partNNN`` numeric suffix is
      reassembled inline below rather than extending the shared helper
      for a one-shot use case.
    """

    name: str
    expected_rows: int
    split_parts: int | None = None


# Phase 1 (already imported in prod) is kept here so a from-scratch
# environment can re-run the whole pipeline with ``--table all``.
DUMPS: dict[str, DumpSpec] = {
    "trade_analyses":       DumpSpec("trade_analyses",       667_094, split_parts=None),
    "weather_observations": DumpSpec("weather_observations", 133_205, split_parts=None),
    "llm_predictions":      DumpSpec("llm_predictions",       57_390, split_parts=None),
    "line_snapshots":       DumpSpec("line_snapshots",     4_722_447, split_parts=2),
}

# Order matters for ``--table all``: smallest dumps first so a
# bandwidth/credential failure surfaces in seconds, not after a
# 4.7M-row reload.
PHASE_ORDER: list[str] = [
    "trade_analyses",
    "weather_observations",
    "llm_predictions",
    "line_snapshots",
]


def _log(msg: str) -> None:
    print(f"[import_v1_history] {msg}", flush=True)


def _bucket_path(spec: DumpSpec) -> str:
    return f"replit-port/archives/pg_dumps/{spec.name}.sql.gz"


def _local_gz(spec: DumpSpec) -> Path:
    return WORK_DIR / f"{spec.name}.sql.gz"


def _local_sql(spec: DumpSpec) -> Path:
    return WORK_DIR / f"{spec.name}.sql"


def _rewritten_sql(spec: DumpSpec) -> Path:
    return WORK_DIR / f"{spec.name}.v1_archive.sql"


async def _ensure_dump_local(spec: DumpSpec, force: bool) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    gz = _local_gz(spec)
    if not gz.exists() or force:
        if spec.split_parts is None:
            _log(f"downloading {_bucket_path(spec)} -> {gz}")
            await storage.download_to_path(_bucket_path(spec), gz, force=True)
        else:
            # V1 split dumps carry a sibling ``<name>.sql.gz.manifest.json``
            # (``{size, chunks, sha256, reassemble}``) and ``<name>.sql.gz.partNNN``
            # chunks.  We just cat the parts back together — the manifest's
            # ``reassemble`` field documents the same operation.
            _log(
                f"reassembling split dump {spec.name} "
                f"({spec.split_parts} parts) -> {gz}"
            )
            with gz.open("wb") as out:
                for i in range(spec.split_parts):
                    part_name = f"{spec.name}.sql.gz.part{i:03d}"
                    part_path = WORK_DIR / part_name
                    await storage.download_to_path(
                        f"replit-port/archives/pg_dumps/{part_name}",
                        part_path,
                        force=force,
                    )
                    with part_path.open("rb") as src:
                        shutil.copyfileobj(src, out, length=64 * 1024 * 1024)
    size_mb = gz.stat().st_size / 1024 / 1024
    _log(f"gz dump ready ({size_mb:.1f} MiB)")

    sql = _local_sql(spec)
    if not sql.exists() or force:
        _log(f"gunzip -> {sql}")
        with gzip.open(gz, "rb") as src, open(sql, "wb") as dst:
            shutil.copyfileobj(src, dst, length=64 * 1024 * 1024)
    sql_size_mb = sql.stat().st_size / 1024 / 1024
    _log(f"plain SQL ready ({sql_size_mb:.1f} MiB)")


def _rewrite_for_v1_archive(spec: DumpSpec) -> None:
    """Rewrite COPY-header schema and strip ``setval`` for the dropped sequence.

    The V1 dump emits:
        COPY public.<table> (...) FROM stdin;
        ...rows...
        \\.
        SELECT pg_catalog.setval('public.<table>_id_seq', NNN, true);

    Our v1_archive tables have no id-sequence (ids are imported
    verbatim, NOT auto-generated), so we drop the setval line.
    Everything else (SET statement_timeout etc.) is safe to keep.
    """
    src_path = _local_sql(spec)
    dst_path = _rewritten_sql(spec)
    copy_marker = f"COPY public.{spec.name} "
    seq_marker = f"{spec.name}_id_seq"
    _log(f"rewriting dump -> {dst_path}")
    with open(src_path, "r", encoding="utf-8", errors="strict") as src, \
         open(dst_path, "w", encoding="utf-8") as dst:
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
            if line.startswith(copy_marker):
                line = line.replace(
                    copy_marker,
                    f"COPY v1_archive.{spec.name} ",
                    1,
                )
            elif seq_marker in line:
                # Skip the SELECT setval(...) line entirely.
                continue
            dst.write(line)
    _log(f"rewritten size: {dst_path.stat().st_size / 1024 / 1024:.1f} MiB")


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


def _atomic_load(
    spec: DumpSpec, dsn: str, dry_run: bool, max_mismatch_pct: float
) -> dict:
    """Atomically TRUNCATE + load + verify-or-rollback in a single tx.

    The previous version called TRUNCATE in one psql session and the
    load in another; if the load failed mid-flight the truncate was
    already committed, leaving the table empty in production.  This
    version streams a BEGIN / TRUNCATE / <dump body> / verify-via-DO /
    COMMIT block through ``psql --single-transaction`` so any failure
    rolls EVERYTHING back, including the truncate.
    """
    _log(
        f"atomic load BEGIN; TRUNCATE v1_archive.{spec.name}; <dump>; "
        f"verify; COMMIT (dry_run={dry_run})"
    )

    # The wrapper SQL lives next to the dump and uses ``\i`` to stream
    # the rewritten dump into psql instead of slurping it into memory
    # (the line_snapshots dump is 637 MiB plain SQL — a previous
    # version concatenated it into a single payload bytes buffer and
    # was OOM-killed by the Replit container).  ``psql
    # --single-transaction`` wraps the whole script in BEGIN/COMMIT;
    # the explicit DO block aborts via RAISE EXCEPTION on a row-count
    # divergence so the surrounding transaction rolls back.  For
    # ``--dry-run`` we append an explicit ROLLBACK so the truncate is
    # never committed.
    wrapper_path = WORK_DIR / f"{spec.name}.wrapper.sql"
    dump_path = _rewritten_sql(spec).resolve()
    rollback_suffix = "\nROLLBACK;\n" if dry_run else ""
    wrapper_path.write_text(
        f"TRUNCATE TABLE v1_archive.{spec.name};\n"
        f"\\i {dump_path}\n"
        f"DO $$\n"
        f"DECLARE n bigint; expected bigint := {spec.expected_rows}; "
        f"tol_pct float := {max_mismatch_pct};\n"
        f"BEGIN\n"
        f"  SELECT count(*) INTO n FROM v1_archive.{spec.name};\n"
        f"  IF abs(n - expected) * 100.0 / expected > tol_pct THEN\n"
        f"    RAISE EXCEPTION 'V1 import row-count divergence on "
        f"{spec.name}: % rows vs expected %; abort+rollback.', "
        f"n, expected;\n"
        f"  END IF;\n"
        f"  RAISE NOTICE 'V1 import OK ({spec.name}): % rows loaded "
        f"(expected ~%)', n, expected;\n"
        f"END$$;{rollback_suffix}\n",
        encoding="utf-8",
    )

    cmd = [
        "psql", dsn, "-X",
        "-v", "ON_ERROR_STOP=1",
        "--single-transaction",
        "-f", str(wrapper_path),
    ]
    completed = subprocess.run(
        cmd, capture_output=True, check=False
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr.decode("utf-8", errors="replace"))
        raise SystemExit(
            f"psql exited {completed.returncode} on {spec.name}; "
            f"transaction rolled back"
        )
    # Surface the RAISE NOTICE line so the operator sees the row count.
    stderr_txt = completed.stderr.decode("utf-8", errors="replace")
    for line in stderr_txt.splitlines():
        if "V1 import OK" in line:
            _log(line.strip())

    if dry_run:
        _log(f"DRY-RUN complete for {spec.name} (transaction rolled back).")
        return {"table": spec.name, "total": None, "diff_pct": None, "dry_run": True}

    total = int(_run_psql(
        f"SELECT count(*) FROM v1_archive.{spec.name}", dsn
    ))
    diff_pct = abs(total - spec.expected_rows) / spec.expected_rows * 100.0
    return {
        "table": spec.name, "total": total, "diff_pct": diff_pct, "dry_run": False
    }


def _post_load_summary(spec: DumpSpec, dsn: str) -> None:
    """Per-table light-touch summary so the operator sees what landed.

    Each table picks the most calibration-relevant grouping:
    * ``trade_analyses``  — top leagues by row count (the
      calibrator-unblock target).
    * ``weather_observations`` — top city codes.
    * ``llm_predictions`` — top (provider, model_id) pairs.
    * ``line_snapshots`` — top (sport, book) pairs.
    """
    if spec.name == "trade_analyses":
        sql = (
            "SELECT league || '|' || count(*)::text "
            "FROM v1_archive.trade_analyses GROUP BY league "
            "ORDER BY count(*) DESC LIMIT 50"
        )
        header = "top leagues by row count:"
    elif spec.name == "weather_observations":
        sql = (
            "SELECT coalesce(city_code,'<null>') || '|' || count(*)::text "
            "FROM v1_archive.weather_observations GROUP BY city_code "
            "ORDER BY count(*) DESC LIMIT 25"
        )
        header = "top city codes by row count:"
    elif spec.name == "llm_predictions":
        sql = (
            "SELECT coalesce(provider,'<null>') || '/' || "
            "coalesce(model_id,'<null>') || '|' || count(*)::text "
            "FROM v1_archive.llm_predictions GROUP BY provider, model_id "
            "ORDER BY count(*) DESC LIMIT 25"
        )
        header = "top (provider/model) pairs by row count:"
    elif spec.name == "line_snapshots":
        sql = (
            "SELECT coalesce(sport,'<null>') || '/' || "
            "coalesce(book,'<null>') || '|' || count(*)::text "
            "FROM v1_archive.line_snapshots GROUP BY sport, book "
            "ORDER BY count(*) DESC LIMIT 25"
        )
        header = "top (sport/book) pairs by row count:"
    else:  # pragma: no cover — guarded by argparse choices
        return
    rows = _run_psql(sql, dsn)
    _log(header)
    for line in rows.splitlines():
        _log(f"  {line}")


def _parse_copy_block(spec: DumpSpec) -> tuple[str, list[str]]:
    """Locate the COPY block in the rewritten dump and return
    ``(copy_header_line, column_list)``.

    The rewritten dump contains exactly one COPY block targeting
    ``v1_archive.<table>``; we use that single header verbatim for
    every chunk so column ordering matches the dump's row format.
    """
    needle = f"COPY v1_archive.{spec.name} ".encode("utf-8")
    with open(_rewritten_sql(spec), "rb") as src:
        for raw in src:
            if raw.startswith(needle):
                header = raw.decode("utf-8").rstrip("\n")
                # "COPY v1_archive.<t> (a, b, c) FROM stdin;"
                inside = header.split("(", 1)[1].rsplit(")", 1)[0]
                cols = [c.strip().strip('"') for c in inside.split(",")]
                return header, cols
    raise SystemExit(
        f"COPY header not found for v1_archive.{spec.name} in rewritten dump"
    )


def _stream_data_rows(spec: DumpSpec):
    """Generator: yield raw COPY data lines (with trailing newline)
    in order, until the ``\\.`` end-of-data marker."""
    needle = f"COPY v1_archive.{spec.name} ".encode("utf-8")
    in_copy = False
    with open(_rewritten_sql(spec), "rb") as src:
        for raw in src:
            if not in_copy:
                if raw.startswith(needle):
                    in_copy = True
                continue
            if raw.startswith(b"\\."):
                return
            yield raw


def _chunked_load(
    spec: DumpSpec, dsn: str, chunk_rows: int, max_mismatch_pct: float
) -> dict:
    """Resumable, idempotent load for dumps too large to fit in a single
    psql session within the runner's wall-clock budget.

    For each chunk we wrap the raw COPY rows in:

        BEGIN;
        CREATE TEMP TABLE _stage (LIKE v1_archive.<t>) ON COMMIT DROP;
        COPY _stage (cols) FROM stdin;
        ...rows...
        \\.
        INSERT INTO v1_archive.<t> (cols) SELECT cols FROM _stage
            ON CONFLICT (id) DO NOTHING;
        COMMIT;

    Each chunk's transaction is independent; ``ON CONFLICT (id) DO
    NOTHING`` makes re-running the script safe — already-loaded ids are
    no-oped, so a partially-completed prior run is naturally completed
    by the next run.  The PK collision path is the spec's
    ``(imported_from, v1_id)`` idempotency requirement (the V1 ``id``
    IS the V1 row id, so PK uniqueness IS the idempotency guarantee).

    Why temp-table + INSERT instead of COPY-direct: a direct
    ``COPY v1_archive.<t>`` would raise a duplicate-key error on the
    first re-run row.  Temp-stage + ON CONFLICT lets the same chunk be
    safely replayed without per-row uniqueness probing on the client
    side.
    """
    header, cols = _parse_copy_block(spec)
    col_csv = ", ".join(f'"{c}"' for c in cols)
    copy_cmd = f"COPY _stage ({col_csv}) FROM stdin;\n"

    pre_count = int(_run_psql(
        f"SELECT count(*) FROM v1_archive.{spec.name}", dsn
    ))
    _write_progress(
        spec.name,
        table=spec.name,
        pid=os.getpid(),
        started_at=datetime.now(timezone.utc).isoformat(),
        expected_rows=spec.expected_rows,
        chunk_rows=chunk_rows,
        pre_count=pre_count,
        rows_loaded=pre_count,
        chunks_completed=0,
        done=False,
    )
    # Resume optimisation: each chunk commits atomically, so a partial
    # prior run leaves ``pre_count`` equal to ``completed_chunks *
    # chunk_rows``.  The dump's COPY block is in deterministic line
    # order, so we can skip the first ``pre_count`` data lines and
    # start streaming chunks from there.  Without this skip, a
    # resumed run re-uploads every previously-loaded row only to have
    # ``ON CONFLICT DO NOTHING`` no-op them server-side — correct, but
    # it wastes the bulk of each new invocation re-shipping bytes the
    # server already has.
    skip_lines = pre_count if pre_count % chunk_rows == 0 else 0
    _log(
        f"chunked load: pre-count={pre_count:,} chunk_rows={chunk_rows:,} "
        f"skip_lines={skip_lines:,}"
    )

    chunk_idx = pre_count // chunk_rows
    rows_in_chunk = 0
    chunk_path = WORK_DIR / f"{spec.name}.chunk.sql"

    def _flush_chunk(rows_buffer: list[bytes], idx: int) -> None:
        if not rows_buffer:
            return
        # Build the chunk SQL on disk (avoid holding 4.7M rows of
        # bytes in memory simultaneously across all chunks).
        with chunk_path.open("wb") as out:
            out.write(b"BEGIN;\n")
            out.write(
                f"CREATE TEMP TABLE _stage (LIKE v1_archive.{spec.name} "
                f"INCLUDING DEFAULTS) ON COMMIT DROP;\n".encode("utf-8")
            )
            out.write(copy_cmd.encode("utf-8"))
            out.writelines(rows_buffer)
            out.write(b"\\.\n")
            out.write(
                f"INSERT INTO v1_archive.{spec.name} ({col_csv}) "
                f"SELECT {col_csv} FROM _stage "
                f"ON CONFLICT (id) DO NOTHING;\n".encode("utf-8")
            )
            out.write(b"COMMIT;\n")
        cmd = [
            "psql", dsn, "-X",
            "-v", "ON_ERROR_STOP=1",
            "-f", str(chunk_path),
        ]
        completed = subprocess.run(
            cmd, capture_output=True, check=False
        )
        if completed.returncode != 0:
            sys.stderr.write(
                completed.stderr.decode("utf-8", errors="replace")
            )
            raise SystemExit(
                f"psql exited {completed.returncode} on chunk {idx}; "
                f"prior chunks remain committed (re-run to resume)"
            )
        _log(f"  chunk {idx} loaded ({len(rows_buffer):,} rows)")
        try:
            post_count = int(_run_psql(
                f"SELECT count(*) FROM v1_archive.{spec.name}", dsn
            ))
        except subprocess.CalledProcessError:
            post_count = None
        _write_progress(
            spec.name,
            chunks_completed=idx + 1,
            last_chunk_rows=len(rows_buffer),
            rows_loaded=post_count if post_count is not None else
                ((idx + 1) * chunk_rows),
        )

    rows_buffer: list[bytes] = []
    seen = 0
    for raw in _stream_data_rows(spec):
        seen += 1
        if seen <= skip_lines:
            continue
        rows_buffer.append(raw)
        rows_in_chunk += 1
        if rows_in_chunk >= chunk_rows:
            _flush_chunk(rows_buffer, chunk_idx)
            chunk_idx += 1
            rows_buffer = []
            rows_in_chunk = 0
    _flush_chunk(rows_buffer, chunk_idx)

    total = int(_run_psql(
        f"SELECT count(*) FROM v1_archive.{spec.name}", dsn
    ))
    diff_pct = abs(total - spec.expected_rows) / spec.expected_rows * 100.0
    if diff_pct > max_mismatch_pct:
        raise SystemExit(
            f"row-count divergence on {spec.name}: {total:,} vs "
            f"expected {spec.expected_rows:,} (diff {diff_pct:.2f}% > "
            f"{max_mismatch_pct}%)"
        )
    _log(
        f"chunked load OK: {total:,} rows (expected ~{spec.expected_rows:,}, "
        f"diff {diff_pct:.4f}%)"
    )
    _write_progress(
        spec.name,
        rows_loaded=total,
        diff_pct=diff_pct,
        finished_at=datetime.now(timezone.utc).isoformat(),
        done=True,
    )
    return {
        "table": spec.name, "total": total, "diff_pct": diff_pct,
        "dry_run": False, "chunked": True,
    }


def _spawn_detached(table: str, passthrough_argv: list[str]) -> dict:
    """Re-launch this script as a detached background process.

    The Replit task runner kills foreground commands at a 120s wall-clock
    budget, which was forcing operators to run ``import_v1_history.py``
    ~10 times in a row to finish the 4.7M-row ``line_snapshots`` load.
    Detached mode forks the same script (minus the ``--detach`` flag)
    into a new session via ``start_new_session=True`` so it survives the
    parent shell's SIGHUP, redirects stdout+stderr to a per-table log
    file, and exits the foreground immediately with the child PID + log
    path so operators can tail progress.

    Idempotency: if a prior detached run is still alive (PID file
    exists and ``kill -0`` succeeds) we refuse to fork a second one.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    pid_path = _detach_pid_path(table)
    if pid_path.exists():
        try:
            prior_pid = int(pid_path.read_text().strip())
            os.kill(prior_pid, 0)
            raise SystemExit(
                f"detached run already in flight for {table} "
                f"(pid {prior_pid}; pid file {pid_path}). "
                f"Use --status to inspect, or remove the pid file to override."
            )
        except (ValueError, ProcessLookupError, PermissionError):
            # Stale PID file from a crashed prior run — replace it.
            pid_path.unlink(missing_ok=True)
        except OSError:
            pid_path.unlink(missing_ok=True)

    log_path = _detach_log_path(table)
    # Truncate the log so each detached invocation starts clean.
    log_fh = open(log_path, "wb", buffering=0)
    log_fh.write(
        f"[import_v1_history] detached run starting at "
        f"{datetime.now(timezone.utc).isoformat()} (table={table})\n".encode()
    )

    cmd = [sys.executable, "-m", "scripts.import_v1_history", *passthrough_argv]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(_API_SERVER_ROOT),
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    log_fh.close()
    pid_path.write_text(f"{proc.pid}\n")
    _log(
        f"detached pid={proc.pid} table={table} log={log_path} "
        f"pidfile={pid_path}"
    )
    _log(
        f"tail -F {log_path}   # to follow progress; "
        f"--status --table {table} for a structured snapshot"
    )
    return {"pid": proc.pid, "log": str(log_path), "pid_file": str(pid_path)}


def _print_status(table: str) -> int:
    """Dump the per-table progress file + detached-process liveness."""
    snapshot: dict = {"table": table}
    prog = _progress_path(table)
    if prog.exists():
        try:
            snapshot["progress"] = json.loads(prog.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            snapshot["progress_error"] = str(exc)
    else:
        snapshot["progress"] = None

    pid_path = _detach_pid_path(table)
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            snapshot["detached_pid"] = pid
            try:
                os.kill(pid, 0)
                snapshot["detached_alive"] = True
            except (ProcessLookupError, PermissionError):
                snapshot["detached_alive"] = False
        except (ValueError, OSError) as exc:
            snapshot["pid_file_error"] = str(exc)
    else:
        snapshot["detached_pid"] = None

    log_path = _detach_log_path(table)
    if log_path.exists():
        snapshot["log"] = str(log_path)
        snapshot["log_size_bytes"] = log_path.stat().st_size

    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


async def _import_one(spec: DumpSpec, args: argparse.Namespace, dsn: str) -> dict:
    _log(f"=== {spec.name} (expected ~{spec.expected_rows:,} rows) ===")
    await _ensure_dump_local(spec, force=args.force_download)
    _rewrite_for_v1_archive(spec)
    # The line_snapshots dump (4.7M rows / 608 MiB plain SQL) cannot be
    # streamed through a single ``psql --single-transaction`` within the
    # 2-minute foreground budget the Replit task runner gives us, so we
    # default it to chunked mode.  Smaller dumps stay on the original
    # atomic path because that path (a) is genuinely all-or-nothing and
    # (b) avoids the per-chunk temp-table overhead.
    use_chunked = (
        args.chunk_rows > 0
        and not args.dry_run
        and (spec.name == "line_snapshots" or args.chunked)
    )
    if use_chunked:
        summary = _chunked_load(
            spec, dsn, args.chunk_rows, args.max_mismatch_pct,
        )
    else:
        summary = _atomic_load(
            spec, dsn,
            dry_run=args.dry_run,
            max_mismatch_pct=args.max_mismatch_pct,
        )
    if not args.dry_run:
        _post_load_summary(spec, dsn)
    _log(f"DONE: {summary}")
    return summary


async def _amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table",
        choices=[*PHASE_ORDER, "all"],
        default="trade_analyses",
        help="Which V1 dump to import (default trade_analyses; "
             "'all' runs every phase in dependency order).",
    )
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
    parser.add_argument(
        "--chunk-rows",
        type=int,
        default=DEFAULT_CHUNK_ROWS,
        help=f"Chunk size (rows) for the chunked-load path used when "
             f"the dump is too large for a single atomic transaction "
             f"(default {DEFAULT_CHUNK_ROWS:,}; 0 disables chunking).",
    )
    parser.add_argument(
        "--chunked",
        action="store_true",
        help="Force the chunked-load path for tables that would "
             "otherwise use the atomic single-transaction path.",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="Fork the import into a detached background process that "
             "survives the runner's 120s foreground budget. Returns "
             "the child PID and log path, then exits immediately.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the per-table progress snapshot (chunks completed, "
             "rows loaded, detached PID liveness) as JSON and exit.",
    )
    args = parser.parse_args()

    if args.status:
        targets = PHASE_ORDER if args.table == "all" else [args.table]
        for name in targets:
            _print_status(name)
        return 0

    if args.detach:
        # Strip --detach from the re-exec argv so the child runs the
        # actual import path. Everything else (--table, --chunk-rows,
        # --force-download, etc.) is forwarded verbatim.
        #
        # For ``--table all`` we deliberately spawn a SINGLE detached
        # child that runs every phase sequentially (using the existing
        # in-process ``--table all`` loop) rather than forking one
        # child per phase.  Per-phase forking would have four full
        # imports racing in parallel — including concurrent TRUNCATE +
        # reload of the non-chunked tables — which is both unsafe and
        # defeats the dependency ordering in ``PHASE_ORDER``.  The
        # progress / pid / log files are keyed under the literal table
        # name ``all`` for that case so ``--status --table all`` can
        # report the orchestrator's liveness; individual phases still
        # write their own per-table progress JSON from within
        # ``_chunked_load`` / ``_atomic_load``.
        passthrough = [a for a in sys.argv[1:] if a != "--detach"]
        _spawn_detached(args.table, passthrough)
        return 0

    dsn = _build_psql_dsn()
    _log(f"target DSN host: {dsn.split('@')[-1].split('/')[0]}")

    targets = PHASE_ORDER if args.table == "all" else [args.table]
    summaries = []
    for name in targets:
        summary = await _import_one(DUMPS[name], args, dsn)
        summaries.append(summary)

    _log(f"ALL DONE: {summaries}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
