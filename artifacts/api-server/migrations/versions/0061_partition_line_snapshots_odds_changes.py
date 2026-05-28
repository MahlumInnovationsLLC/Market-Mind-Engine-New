"""Partition ``line_snapshots`` and ``odds_changes`` by month on ``timestamp``.

Both tables hold ~100M rows each and dominate Supabase storage + INSERT
wall-time (see ``.local/audit/PERFORMANCE_RELIABILITY_AUDIT_2026-05-28.md``).
Converting them to monthly RANGE partitions lets the retention sweep
DROP whole partitions in O(1) metadata time instead of running a
multi-million-row ``DELETE`` against an indexed heap.

Strategy (safe, idempotent):
1. Rename the existing heap to ``<name>_legacy_pre0061`` — pure metadata,
   sub-second even on 100M rows. New writes will land on the partitioned
   table created next under the original name.
2. Create the partitioned parent (same column shape, PK changed to the
   natural unique tuple because RANGE-partitioned PKs must include the
   partition key; the ``id`` column is dropped — no caller reads it).
3. Pre-create monthly partitions for [last 2 months, current, next 1].
   The daily retention loop in :mod:`app.partitioned_table_retention`
   pre-creates further future partitions ahead of the boundary.
4. Recreate the per-table indexes on the partitioned parent so the
   reader query plans match the legacy layout.

The legacy heap is left in place for the operator-driven backfill
(``scripts/backfill_partitioned_tables.py``) to copy into the new
partitions. After verification it can be DROPped manually per
``docs/partition_migration_runbook.md``.

Revision ID: 0061_partition_line_snapshots_odds_changes
Revises: 0060_edge_terminal_llm_resolver_cache
"""
from __future__ import annotations

from datetime import datetime, timezone

from alembic import op


revision = "0061_partition_line_snapshots_odds_changes"
down_revision = "0060_edge_terminal_llm_resolver_cache"
branch_labels = None
depends_on = None


def _month_bounds(year: int, month: int) -> tuple[str, str, str]:
    """Return (suffix, start_iso, end_iso) for the given (year, month).

    Suffix format ``YYYY_MM`` is shared with :mod:`app.partitioning`.
    """
    if month == 12:
        end_y, end_m = year + 1, 1
    else:
        end_y, end_m = year, month + 1
    start = f"{year:04d}-{month:02d}-01"
    end = f"{end_y:04d}-{end_m:02d}-01"
    return (f"{year:04d}_{month:02d}", start, end)


def _seed_months(anchor: datetime, *, back: int, forward: int) -> list[tuple[str, str, str]]:
    """Months we materialize in this migration: [anchor-back .. anchor+forward].

    The runtime loop in :mod:`app.partitioned_table_retention` keeps
    extending the future edge; this seed only has to cover the window
    between deploy and the loop's first tick.
    """
    out: list[tuple[str, str, str]] = []
    y, m = anchor.year, anchor.month
    # walk back `back` months
    for _ in range(back):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    for _ in range(back + forward + 1):
        out.append(_month_bounds(y, m))
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


def _build_sql_up() -> str:
    # Anchor at migration RUNTIME so the partition window adapts to whenever
    # this is applied, not when the file was authored.
    now = datetime.now(timezone.utc)
    months = _seed_months(now, back=2, forward=1)

    line_partitions = "\n".join(
        f"""CREATE TABLE IF NOT EXISTS line_snapshots_{suf}
    PARTITION OF line_snapshots
    FOR VALUES FROM ('{start} 00:00:00+00') TO ('{end} 00:00:00+00');"""
        for suf, start, end in months
    )
    odds_partitions = "\n".join(
        f"""CREATE TABLE IF NOT EXISTS odds_changes_{suf}
    PARTITION OF odds_changes
    FOR VALUES FROM ('{start} 00:00:00+00') TO ('{end} 00:00:00+00');"""
        for suf, start, end in months
    )

    return f"""
-- Fail fast if the live writer is holding an AccessShare lock on the
-- legacy heap — the RENAME needs AccessExclusive. Without this the
-- migration can wait forever and trip the boot watchdog. If we fail,
-- alembic retries on the next pod restart; idempotent guards below
-- make a partial-run a no-op.
SET LOCAL lock_timeout = '30s';
SET LOCAL statement_timeout = '600s';

-- ============================================================
-- line_snapshots: rename heap, create partitioned parent
-- ============================================================
DO $do$
BEGIN
    -- Only rename if the live table is still a plain heap (relkind 'r').
    -- If a prior run already created the partitioned parent (relkind 'p'),
    -- this whole block becomes a no-op so the migration is idempotent.
    IF EXISTS (
        SELECT 1
          FROM pg_class
         WHERE relname = 'line_snapshots'
           AND relkind = 'r'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_class WHERE relname = 'line_snapshots_legacy_pre0061'
    ) THEN
        ALTER TABLE line_snapshots RENAME TO line_snapshots_legacy_pre0061;
    END IF;
END
$do$;

CREATE TABLE IF NOT EXISTS line_snapshots (
    game_id      varchar           NOT NULL,
    sport        varchar           NOT NULL,
    book         varchar           NOT NULL DEFAULT 'consensus',
    market_type  varchar           NOT NULL,
    line_value   double precision,
    odds_value   double precision,
    home_team    varchar,
    away_team    varchar,
    is_opening   boolean           DEFAULT false,
    outcome      varchar,
    "timestamp"  timestamptz       NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, book, market_type, outcome, "timestamp")
) PARTITION BY RANGE ("timestamp");

-- Lookup index used by margin_regressor + best_line_lookup. Created on
-- the partitioned parent so each partition inherits its own local copy.
CREATE INDEX IF NOT EXISTS idx_line_snapshots_game
    ON line_snapshots (game_id);
CREATE INDEX IF NOT EXISTS idx_line_snapshots_game_type
    ON line_snapshots (game_id, market_type);

{line_partitions}

-- ============================================================
-- odds_changes: rename heap, create partitioned parent
-- ============================================================
-- Existing column was TEXT; the partitioned version uses timestamptz so
-- range partitioning works. asyncpg's writer already passes ISO strings
-- which Postgres parses transparently on insert.
DO $do$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM pg_class
         WHERE relname = 'odds_changes'
           AND relkind = 'r'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_class WHERE relname = 'odds_changes_legacy_pre0061'
    ) THEN
        ALTER TABLE odds_changes RENAME TO odds_changes_legacy_pre0061;
    END IF;
END
$do$;

CREATE TABLE IF NOT EXISTS odds_changes (
    game_id      text         NOT NULL,
    book         text         NOT NULL,
    market_type  text         NOT NULL,
    line         text,
    odds         real,
    "timestamp"  timestamptz  NOT NULL DEFAULT now()
) PARTITION BY RANGE ("timestamp");

CREATE INDEX IF NOT EXISTS idx_odds_changes_lookup
    ON odds_changes (game_id, book, market_type, "timestamp");

{odds_partitions}

-- ============================================================
-- Bookkeeping table so the operator dashboard + tests can introspect
-- which migration installed the partitioned layout.
-- ============================================================
CREATE TABLE IF NOT EXISTS partitioned_table_registry (
    parent_table     text PRIMARY KEY,
    partition_key    text NOT NULL,
    partition_cadence text NOT NULL,
    retention_env    text NOT NULL,
    default_days     int  NOT NULL,
    legacy_table     text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
INSERT INTO partitioned_table_registry
    (parent_table, partition_key, partition_cadence, retention_env, default_days, legacy_table)
VALUES
    ('line_snapshots', 'timestamp', 'monthly', 'LINE_SNAPSHOT_RETENTION_DAYS', 30, 'line_snapshots_legacy_pre0061'),
    ('odds_changes',  'timestamp', 'monthly', 'ODDS_CHANGES_RETENTION_DAYS',  30, 'odds_changes_legacy_pre0061')
ON CONFLICT (parent_table) DO NOTHING;
"""


_SQL_DOWN = """
-- Best-effort restore of the legacy heap. Any rows written to the
-- partitioned table between upgrade and downgrade are NOT migrated
-- back; the runbook calls this out explicitly.
DROP TABLE IF EXISTS partitioned_table_registry;

DO $do$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class
         WHERE relname = 'line_snapshots' AND relkind = 'p'
    ) THEN
        DROP TABLE line_snapshots CASCADE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class WHERE relname = 'line_snapshots_legacy_pre0061'
    ) THEN
        ALTER TABLE line_snapshots_legacy_pre0061 RENAME TO line_snapshots;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_class
         WHERE relname = 'odds_changes' AND relkind = 'p'
    ) THEN
        DROP TABLE odds_changes CASCADE;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class WHERE relname = 'odds_changes_legacy_pre0061'
    ) THEN
        ALTER TABLE odds_changes_legacy_pre0061 RENAME TO odds_changes;
    END IF;
END
$do$;
"""


def upgrade() -> None:
    op.execute(_build_sql_up())


def downgrade() -> None:
    op.execute(_SQL_DOWN)
