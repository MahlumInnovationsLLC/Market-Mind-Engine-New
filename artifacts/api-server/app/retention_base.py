"""Generic ``start()/stop()/state()`` retention loop primitives (task #757).

The existing per-table retention modules (``crypto_ohlcv_retention``,
``order_fills_retention``, …) each ship ~150 lines of boilerplate that
differs only in:
  * the table name + timestamp column,
  * the env-var prefix + default horizon,
  * whether to archive-then-delete (ML / training tables) or just
    delete (operational / audit logs).

This module factors that boilerplate into two reusable building blocks
so the new task #757 modules can be written as ~30-line shims:

  * :class:`DeleteOnlyRetention` — periodic age-bounded ``DELETE``.
  * :class:`ArchiveThenDeleteRetention` — periodic upload-then-delete
    via :func:`app.archive_helper.archive_table`.

Each instance owns its own asyncio task + state dict, so the existing
``start()/stop()/state()`` module-level shape just becomes a thin
forwarder.  All env-var knobs match the project convention:

    AXIOM_<TABLE>_RETENTION_ENABLED         (default 0 — see below)
    AXIOM_<TABLE>_RETENTION_DAYS            (default per module)
    AXIOM_<TABLE>_CLEANUP_INTERVAL_SECONDS  (default per module)

Loops created via this module default to **disabled** so a fresh
deploy doesn't auto-trigger destructive sweeps before the operator
has reviewed the dry-run.  Existing crypto_ohlcv / order_fills /
free_tier_views / crypto_backtest / sportradar_leaderboards loops
keep their pre-existing default (enabled) and are NOT migrated by
this task.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import archive_helper, db


log = logging.getLogger("axiom.retention_base")


def _coerce_int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _coerce_float(raw: str | None, default: float) -> float:
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _parse_delete_status(status: str) -> int:
    """asyncpg returns ``"DELETE <n>"``; pull <n> back out, default 0."""
    try:
        parts = (status or "").split()
        if len(parts) >= 2 and parts[0].upper() == "DELETE":
            return int(parts[1])
    except (ValueError, AttributeError):
        pass
    return 0


class _BaseRetention:
    """Shared task lifecycle + ``state()`` shape for both flavors."""

    # Subclasses override.
    flavor: str = "base"

    def __init__(
        self,
        *,
        name: str,
        table: str,
        ts_column: str,
        env_prefix: str,
        default_days: int,
        default_interval_seconds: float,
        default_enabled: bool = False,
        min_interval_seconds: float = 300.0,
    ) -> None:
        self.name = name
        self.table = table
        self.ts_column = ts_column
        self.env_prefix = env_prefix
        self.default_days = max(1, int(default_days))
        self.default_interval_seconds = float(default_interval_seconds)
        self.default_enabled = bool(default_enabled)
        self.min_interval_seconds = float(min_interval_seconds)

        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._started_at: float | None = None
        self._last_tick_at: float | None = None
        self._last_error: str | None = None
        self._last_deleted: int = 0
        self._total_deleted: int = 0

    # ------------------------------------------------------------------
    # Env-var knobs
    # ------------------------------------------------------------------

    def _env(self, suffix: str) -> str:
        return f"{self.env_prefix}_{suffix}"

    def retention_days(self) -> int:
        v = _coerce_int(os.environ.get(self._env("RETENTION_DAYS")), self.default_days)
        # Refuse 0 / negative so a typo can't immediately wipe the table.
        return max(1, v)

    def interval_seconds(self) -> float:
        v = _coerce_float(
            os.environ.get(self._env("CLEANUP_INTERVAL_SECONDS")),
            float(self.default_interval_seconds),
        )
        return max(self.min_interval_seconds, v)

    def enabled(self) -> bool:
        raw = os.environ.get(self._env("RETENTION_ENABLED"))
        if raw is None:
            return self.default_enabled
        return (raw or "").strip() != "0"

    def cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=self.retention_days())

    # ------------------------------------------------------------------
    # State / lifecycle
    # ------------------------------------------------------------------

    def base_state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "flavor": self.flavor,
            "table": self.table,
            "ts_column": self.ts_column,
            "running": self._task is not None and not self._task.done(),
            "enabled": self.enabled(),
            "started_at": self._started_at,
            "last_tick_at": self._last_tick_at,
            "last_error": self._last_error,
            "last_deleted": self._last_deleted,
            "total_deleted": self._total_deleted,
            "retention_days": self.retention_days(),
            "interval_seconds": self.interval_seconds(),
        }

    def state(self) -> dict[str, Any]:  # subclasses extend
        return self.base_state()

    async def cleanup_once(self) -> int:
        raise NotImplementedError

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.cleanup_once()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds()
                )
            except asyncio.TimeoutError:
                continue
            else:
                return

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self.enabled():
            log.info("%s retention disabled by env", self.name)
            return
        # Re-create the Event in case start() is called from a fresh
        # event loop (test reuse pattern).
        self._stop = asyncio.Event()
        self._started_at = time.time()
        self._task = asyncio.create_task(
            self._loop(), name=f"{self.name}-retention"
        )
        log.info(
            "%s retention started (table=%s, days=%s, interval=%ss, flavor=%s)",
            self.name, self.table, self.retention_days(),
            self.interval_seconds(), self.flavor,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def _reset_state_for_tests(self) -> None:
        self._task = None
        self._started_at = None
        self._last_tick_at = None
        self._last_error = None
        self._last_deleted = 0
        self._total_deleted = 0


class DeleteOnlyRetention(_BaseRetention):
    """Operational / audit log: pure batched ``DELETE`` past the horizon.

    Used for tables we never want to re-train on (pipeline events,
    settlement events, audit log, billing events, etc.).
    """

    flavor = "delete-only"

    async def cleanup_once(self) -> int:
        days = self.retention_days()
        qtable = archive_helper._quote_ident(self.table)
        qts = archive_helper._quote_ident(self.ts_column)
        try:
            status = await db.execute(
                f"DELETE FROM {qtable} "
                f"WHERE {qts} < now() - ($1::int * INTERVAL '1 day')",
                days,
            )
            deleted = _parse_delete_status(status)
            self._last_deleted = deleted
            self._total_deleted += deleted
            self._last_error = None
            if deleted:
                log.info(
                    "%s retention: deleted %s row(s) older than %s day(s)",
                    self.name, deleted, days,
                )
            return deleted
        except Exception as e:  # noqa: BLE001 — counted, retried next tick
            self._last_error = f"{type(e).__name__}: {e}"
            log.warning(
                "%s retention sweep failed: %s", self.name, self._last_error,
            )
            self._last_deleted = 0
            return 0
        finally:
            self._last_tick_at = time.time()


class ArchiveThenDeleteRetention(_BaseRetention):
    """ML / training data: archive to Supabase Storage then delete.

    Wraps :func:`app.archive_helper.archive_table`.  ``state()`` adds
    archive-specific counters (rows / bytes / last object key) so the
    operator dashboard can show "23 GiB reclaimed last sweep" alongside
    the row counts.
    """

    flavor = "archive"

    def __init__(
        self,
        *,
        pk_columns: tuple[str, ...] = ("id",),
        chunk_size: int = archive_helper.DEFAULT_CHUNK_SIZE,
        max_rows_per_run: int | None = archive_helper.DEFAULT_MAX_ROWS_PER_RUN,
        # ArchiveThenDelete loops sweep less often than delete-only
        # ones because each sweep does a bucket round-trip per chunk.
        default_interval_seconds: float = 6 * 3600,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            default_interval_seconds=default_interval_seconds, **kwargs,
        )
        self.pk_columns = tuple(pk_columns)
        self.chunk_size = int(chunk_size)
        self.max_rows_per_run = max_rows_per_run

        self._last_archived_rows: int = 0
        self._total_archived_rows: int = 0
        self._last_bytes_compressed: int = 0
        self._total_bytes_compressed: int = 0
        self._last_object_key: str | None = None

    def state(self) -> dict[str, Any]:
        base = self.base_state()
        base.update(
            {
                "pk_columns": list(self.pk_columns),
                "chunk_size": self.chunk_size,
                "max_rows_per_run": self.max_rows_per_run,
                "last_archived_rows": self._last_archived_rows,
                "total_archived_rows": self._total_archived_rows,
                "last_bytes_compressed": self._last_bytes_compressed,
                "total_bytes_compressed": self._total_bytes_compressed,
                "last_object_key": self._last_object_key,
            }
        )
        return base

    async def cleanup_once(self) -> int:
        cutoff = self.cutoff()
        try:
            summary = await archive_helper.archive_table(
                table=self.table,
                ts_column=self.ts_column,
                cutoff=cutoff,
                pk_columns=self.pk_columns,
                chunk_size=self.chunk_size,
                max_rows=self.max_rows_per_run,
            )
            if summary.error:
                self._last_error = summary.error
                self._last_deleted = 0
                self._last_archived_rows = 0
                self._last_bytes_compressed = 0
                return 0

            self._last_error = None
            self._last_deleted = summary.rows
            self._total_deleted += summary.rows
            self._last_archived_rows = summary.rows
            self._total_archived_rows += summary.rows
            self._last_bytes_compressed = summary.bytes_compressed
            self._total_bytes_compressed += summary.bytes_compressed
            self._last_object_key = summary.last_object_key
            if summary.rows:
                log.info(
                    "%s retention: archived %s row(s) into %s chunk(s) "
                    "(%s bytes compressed) older than %s",
                    self.name, summary.rows, summary.chunks,
                    summary.bytes_compressed, cutoff.isoformat(),
                )
            return summary.rows
        except Exception as e:  # noqa: BLE001 — counted, retried next tick
            self._last_error = f"{type(e).__name__}: {e}"
            self._last_deleted = 0
            self._last_archived_rows = 0
            self._last_bytes_compressed = 0
            log.warning(
                "%s retention sweep failed: %s", self.name, self._last_error,
            )
            return 0
        finally:
            self._last_tick_at = time.time()

    def _reset_state_for_tests(self) -> None:
        super()._reset_state_for_tests()
        self._last_archived_rows = 0
        self._total_archived_rows = 0
        self._last_bytes_compressed = 0
        self._total_bytes_compressed = 0
        self._last_object_key = None
