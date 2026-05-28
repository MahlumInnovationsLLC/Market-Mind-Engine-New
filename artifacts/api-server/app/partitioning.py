"""Monthly range-partition helpers shared by migration 0061 + the
runtime retention loop (:mod:`app.partitioned_table_retention`).

The functions in this module are deliberately pure SQL builders so the
unit tests can verify the generated statements without spinning up
Postgres. The actual ``CREATE TABLE`` / ``DETACH PARTITION`` / ``DROP
TABLE`` execution happens inside the caller's own asyncpg connection so
it shares the caller's transaction semantics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


# ----------------------------------------------------------------------
# Identifier whitelist
# ----------------------------------------------------------------------

# Parent table names are passed through to ``f"{parent}_{suffix}"`` and
# therefore must be safe. We restrict to ``[a-z][a-z0-9_]*`` so a
# typo / hostile call can't smuggle DDL.
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _check_ident(name: str) -> str:
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


# ----------------------------------------------------------------------
# Month math
# ----------------------------------------------------------------------


def month_floor(dt: datetime) -> datetime:
    """Truncate *dt* down to the first instant of its UTC month."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)


def add_months(dt: datetime, months: int) -> datetime:
    """Return *dt* shifted by *months* (positive or negative)."""
    base = month_floor(dt)
    total = base.year * 12 + (base.month - 1) + months
    y, m = divmod(total, 12)
    return datetime(y, m + 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class MonthBounds:
    """Inclusive-start / exclusive-end bounds for a monthly partition."""
    year: int
    month: int

    @property
    def suffix(self) -> str:
        return f"{self.year:04d}_{self.month:02d}"

    @property
    def start(self) -> datetime:
        return datetime(self.year, self.month, 1, tzinfo=timezone.utc)

    @property
    def end(self) -> datetime:
        if self.month == 12:
            return datetime(self.year + 1, 1, 1, tzinfo=timezone.utc)
        return datetime(self.year, self.month + 1, 1, tzinfo=timezone.utc)


def month_bounds(dt: datetime) -> MonthBounds:
    base = month_floor(dt)
    return MonthBounds(base.year, base.month)


def months_between(start: datetime, end: datetime) -> list[MonthBounds]:
    """Inclusive list of every month touching the [start, end] range.

    Returns an empty list when ``end < start``.
    """
    if end < start:
        return []
    cur = month_floor(start)
    stop = month_floor(end)
    out: list[MonthBounds] = []
    while cur <= stop:
        out.append(month_bounds(cur))
        cur = add_months(cur, 1)
    return out


# ----------------------------------------------------------------------
# SQL builders
# ----------------------------------------------------------------------


def partition_name(parent: str, bounds: MonthBounds) -> str:
    _check_ident(parent)
    return f"{parent}_{bounds.suffix}"


def create_partition_sql(parent: str, bounds: MonthBounds) -> str:
    """Return idempotent ``CREATE TABLE ... PARTITION OF ...`` SQL.

    The migration uses the same pattern for its seed partitions; this
    function is reused at runtime to pre-create future partitions before
    inserts cross the month boundary.
    """
    _check_ident(parent)
    child = partition_name(parent, bounds)
    return (
        f"CREATE TABLE IF NOT EXISTS {child}\n"
        f"    PARTITION OF {parent}\n"
        f"    FOR VALUES FROM ('{bounds.start.isoformat()}') "
        f"TO ('{bounds.end.isoformat()}');"
    )


def drop_partition_sql(parent: str, bounds: MonthBounds) -> str:
    """Return the two statements needed to safely drop a monthly partition.

    DETACH first so a long-running SELECT against the parent doesn't hold
    a lock that blocks the DROP — the DETACH ... CONCURRENTLY variant
    can't run inside a transaction, so we use the plain form and rely on
    the loop's short tick.
    """
    _check_ident(parent)
    child = partition_name(parent, bounds)
    return (
        f"ALTER TABLE {parent} DETACH PARTITION {child};\n"
        f"DROP TABLE IF EXISTS {child};"
    )


def list_partitions_sql(parent: str) -> str:
    """Return SQL that lists all monthly child partitions of *parent*.

    Used by the retention loop to find candidates for dropping. The
    result rows expose ``(child_name, year, month)`` so the caller can
    apply the cutoff without re-parsing.
    """
    _check_ident(parent)
    return f"""
SELECT
    c.relname AS child,
    substring(c.relname FROM '_(\\d{{4}})_\\d{{2}}$')::int AS year,
    substring(c.relname FROM '_\\d{{4}}_(\\d{{2}})$')::int AS month
  FROM pg_inherits i
  JOIN pg_class c       ON c.oid       = i.inhrelid
  JOIN pg_class parent  ON parent.oid  = i.inhparent
 WHERE parent.relname = '{parent}'
   AND c.relname ~ '_\\d{{4}}_\\d{{2}}$'
 ORDER BY c.relname
""".strip()


# ----------------------------------------------------------------------
# Planning helpers (pure)
# ----------------------------------------------------------------------


def partitions_to_drop(
    existing: Iterable[MonthBounds], *, cutoff: datetime,
) -> list[MonthBounds]:
    """From *existing* partitions, return the subset whose ``end`` is
    strictly before *cutoff* (so dropping them never removes a row that
    a 30-day retention horizon should keep).
    """
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return [b for b in existing if b.end <= cutoff]


def future_partitions_needed(
    *, now: datetime, months_ahead: int, existing_suffixes: Iterable[str],
) -> list[MonthBounds]:
    """Months between *now* and now+months_ahead that are missing.

    Always includes the current month so a freshly-deployed pod can
    self-heal if the migration's seed window has already rolled off.
    """
    have = set(existing_suffixes)
    out: list[MonthBounds] = []
    for i in range(0, months_ahead + 1):
        b = month_bounds(add_months(now, i))
        if b.suffix not in have:
            out.append(b)
    return out
