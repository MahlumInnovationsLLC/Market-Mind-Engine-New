"""Daily retention sweep for the partitioned ``line_snapshots`` and
``odds_changes`` tables (task #921).

The loop is intentionally cheaper than the older
``ArchiveThenDeleteRetention`` flavor: every tick it walks the
existing monthly partitions, drops anything whose upper bound is
older than the retention cutoff, and pre-creates the next month
ahead of the boundary so a 23:59 insert never trips into an
uncovered range.

Disabled by default — matches the task #757 pattern so a fresh
deploy doesn't reclaim space until the operator has run the
backfill and verified counts (see
``docs/partition_migration_runbook.md``).

Env knobs (per parent table):

    LINE_SNAPSHOT_RETENTION_DAYS        (default 30)
    AXIOM_LINE_SNAPSHOTS_PARTITIONED_RETENTION_ENABLED   (default 0)

    ODDS_CHANGES_RETENTION_DAYS         (default 30)
    AXIOM_ODDS_CHANGES_PARTITIONED_RETENTION_ENABLED     (default 0)

    AXIOM_PARTITIONED_RETENTION_INTERVAL_SECONDS (default 86400 — daily)
    AXIOM_PARTITIONED_RETENTION_MONTHS_AHEAD     (default 2)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, partitioning


log = logging.getLogger("axiom.partitioned_retention")


def _coerce_int(raw: str | None, default: int) -> int:
    try:
        return int(float(raw)) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _coerce_float(raw: str | None, default: float) -> float:
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


class PartitionedTableRetention:
    """Per-parent-table retention controller.

    One instance per (parent table, retention env) pair. Owns its own
    asyncio task + state dict.
    """

    flavor = "partitioned-drop"

    def __init__(
        self,
        *,
        name: str,
        parent_table: str,
        retention_env: str,
        default_days: int = 30,
        enabled_env: str | None = None,
    ) -> None:
        self.name = name
        self.parent_table = parent_table
        self.retention_env = retention_env
        self.default_days = max(1, int(default_days))
        self.enabled_env = (
            enabled_env
            or f"AXIOM_{parent_table.upper()}_PARTITIONED_RETENTION_ENABLED"
        )

        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._started_at: float | None = None
        self._last_tick_at: float | None = None
        self._last_error: str | None = None
        self._last_dropped: list[str] = []
        self._last_created: list[str] = []
        self._total_dropped: int = 0
        self._total_created: int = 0

    # ---- env knobs --------------------------------------------------

    def retention_days(self) -> int:
        return max(1, _coerce_int(os.environ.get(self.retention_env), self.default_days))

    def interval_seconds(self) -> float:
        return max(
            300.0,
            _coerce_float(
                os.environ.get("AXIOM_PARTITIONED_RETENTION_INTERVAL_SECONDS"),
                86400.0,
            ),
        )

    def months_ahead(self) -> int:
        return max(
            1,
            _coerce_int(
                os.environ.get("AXIOM_PARTITIONED_RETENTION_MONTHS_AHEAD"),
                2,
            ),
        )

    def enabled(self) -> bool:
        raw = os.environ.get(self.enabled_env)
        if raw is None:
            return False
        return (raw or "").strip() != "0"

    def cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=self.retention_days())

    # ---- lifecycle --------------------------------------------------

    def state(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "flavor": self.flavor,
            "table": self.parent_table,
            "running": self._task is not None and not self._task.done(),
            "enabled": self.enabled(),
            "retention_days": self.retention_days(),
            "interval_seconds": self.interval_seconds(),
            "months_ahead": self.months_ahead(),
            "started_at": self._started_at,
            "last_tick_at": self._last_tick_at,
            "last_error": self._last_error,
            "last_dropped": list(self._last_dropped),
            "last_created": list(self._last_created),
            "total_dropped": self._total_dropped,
            "total_created": self._total_created,
            "retention_env": self.retention_env,
            "enabled_env": self.enabled_env,
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self.enabled():
            log.info("%s partition retention disabled by env", self.name)
            return
        self._stop = asyncio.Event()
        self._started_at = time.time()
        self._task = asyncio.create_task(
            self._loop(), name=f"{self.name}-partition-retention",
        )
        log.info(
            "%s partition retention started (table=%s, days=%s, interval=%ss)",
            self.name, self.parent_table, self.retention_days(),
            self.interval_seconds(),
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

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.cleanup_once()
            except Exception as e:  # noqa: BLE001
                self._last_error = f"{type(e).__name__}: {e}"
                log.warning("%s partition retention tick failed: %s", self.name, e)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds(),
                )
            except asyncio.TimeoutError:
                continue
            else:
                return

    # ---- core sweep -------------------------------------------------

    async def _list_partitions(self) -> list[partitioning.MonthBounds]:
        rows = await db.fetch(partitioning.list_partitions_sql(self.parent_table))
        out: list[partitioning.MonthBounds] = []
        for r in rows:
            try:
                out.append(partitioning.MonthBounds(int(r["year"]), int(r["month"])))
            except Exception:
                continue
        return out

    async def cleanup_once(self) -> dict[str, list[str]]:
        """One retention tick: pre-create future months, drop old ones.

        Returns ``{"created": [...], "dropped": [...]}`` so the operator
        dashboard can show "+2 / -3" deltas without parsing logs.
        """
        self._last_tick_at = time.time()
        now = datetime.now(timezone.utc)
        cutoff = self.cutoff()
        existing = await self._list_partitions()
        existing_suffixes = {b.suffix for b in existing}

        created: list[str] = []
        for b in partitioning.future_partitions_needed(
            now=now,
            months_ahead=self.months_ahead(),
            existing_suffixes=existing_suffixes,
        ):
            sql = partitioning.create_partition_sql(self.parent_table, b)
            try:
                await db.execute(sql)
                created.append(partitioning.partition_name(self.parent_table, b))
                log.info(
                    "%s partition retention: created partition %s",
                    self.name, partitioning.partition_name(self.parent_table, b),
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "%s partition retention: create %s failed: %s",
                    self.name, b.suffix, e,
                )

        dropped: list[str] = []
        for b in partitioning.partitions_to_drop(existing, cutoff=cutoff):
            child = partitioning.partition_name(self.parent_table, b)
            sql = partitioning.drop_partition_sql(self.parent_table, b)
            try:
                # Run as two statements: DETACH first (so any in-flight
                # SELECTs on the parent release their share lock fast),
                # then DROP.
                for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                    await db.execute(stmt)
                dropped.append(child)
                log.info(
                    "%s partition retention: dropped %s (cutoff=%s)",
                    self.name, child, cutoff.isoformat(),
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "%s partition retention: drop %s failed: %s",
                    self.name, child, e,
                )

        self._last_created = created
        self._last_dropped = dropped
        self._total_created += len(created)
        self._total_dropped += len(dropped)
        self._last_error = None
        return {"created": created, "dropped": dropped}

    def _reset_state_for_tests(self) -> None:
        self._task = None
        self._started_at = None
        self._last_tick_at = None
        self._last_error = None
        self._last_dropped = []
        self._last_created = []
        self._total_dropped = 0
        self._total_created = 0


# ----------------------------------------------------------------------
# Module-level instances + lifecycle shims (match task #757 surface)
# ----------------------------------------------------------------------


_LINE_SNAPSHOTS = PartitionedTableRetention(
    name="line_snapshots_partitioned",
    parent_table="line_snapshots",
    retention_env="LINE_SNAPSHOT_RETENTION_DAYS",
    default_days=30,
)

_ODDS_CHANGES = PartitionedTableRetention(
    name="odds_changes_partitioned",
    parent_table="odds_changes",
    retention_env="ODDS_CHANGES_RETENTION_DAYS",
    default_days=30,
)


INSTANCES: tuple[PartitionedTableRetention, ...] = (_LINE_SNAPSHOTS, _ODDS_CHANGES)


async def start_all() -> None:
    for inst in INSTANCES:
        try:
            await inst.start()
        except Exception as e:  # noqa: BLE001
            log.warning("%s partition retention start failed: %s", inst.name, e)


async def stop_all() -> None:
    for inst in INSTANCES:
        try:
            await inst.stop()
        except Exception as e:  # noqa: BLE001
            log.warning("%s partition retention stop error: %s", inst.name, e)


def states() -> list[dict[str, Any]]:
    return [inst.state() for inst in INSTANCES]


# Single-shot helpers for ad-hoc invocation (e.g. from a maintenance shell)
async def cleanup_line_snapshots_once() -> dict[str, list[str]]:
    return await _LINE_SNAPSHOTS.cleanup_once()


async def cleanup_odds_changes_once() -> dict[str, list[str]]:
    return await _ODDS_CHANGES.cleanup_once()


def _reset_state_for_tests() -> None:
    for inst in INSTANCES:
        inst._reset_state_for_tests()
