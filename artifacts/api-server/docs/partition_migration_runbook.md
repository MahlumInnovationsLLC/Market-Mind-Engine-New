# `line_snapshots` / `odds_changes` partition cutover runbook

Migration `0061_partition_line_snapshots_odds_changes` was the largest
storage-cost win in the 2026-05-28 performance audit. This runbook is
the operator-facing checklist for completing the cutover.

## What the migration did

- Renamed `public.line_snapshots` → `public.line_snapshots_legacy_pre0061`.
- Renamed `public.odds_changes`   → `public.odds_changes_legacy_pre0061`.
- Created fresh `public.line_snapshots` and `public.odds_changes` as
  monthly RANGE-partitioned tables on `timestamp`.
- Pre-created partitions for **[last 2 months, current month, next month]**.
- Recorded the layout in `public.partitioned_table_registry` so the
  operator dashboard can introspect it.

Live writers (`app/feeds/odds_api.py`) start hitting the new partitioned
tables immediately and transparently — no writer-side change was
required because both new tables keep the exact same column names and
the same on-disk type (the new `odds_changes.timestamp` is `timestamptz`
where the old one was `text`; asyncpg passes ISO-8601 strings either
way and Postgres parses them on insert).

Readers continue to query `line_snapshots` and `odds_changes` and will
see live (post-migration) data only. Historical rows are still in the
`_legacy_pre0061` heaps until you run the backfill.

## Step 1 — Backfill historical rows

```bash
# Dry run: see how many rows would be copied per month.
python -m scripts.backfill_partitioned_tables --dry-run

# Real run: copy both tables, all months.
python -m scripts.backfill_partitioned_tables

# Only copy the last 3 months of one table (smoke test).
python -m scripts.backfill_partitioned_tables \
  --table line_snapshots --max-months 3

# For very large months, chunk the INSERT to bound transaction size.
python -m scripts.backfill_partitioned_tables --batch-rows 250000
```

The script:
- Walks months newest-first so the rows your readers care about land
  first.
- Auto-creates any missing monthly partition before insert.
- `ON CONFLICT DO NOTHING` on `line_snapshots` (its natural-key tuple
  is the new PK); idempotent reruns.
- Records progress in `partitioned_backfill_cursor` so an interrupted
  run resumes where it left off. Pass `--reset` to wipe the cursor.

Expected wall-time on the Supabase prod tier: roughly 30–60 min per
50M-row table per pass (network bound). Run inside a `tmux` /
`screen` session.

## Step 2 — Verify counts match

```sql
SELECT
  (SELECT count(*) FROM line_snapshots_legacy_pre0061) AS legacy_lines,
  (SELECT count(*) FROM line_snapshots)                 AS new_lines,
  (SELECT count(*) FROM odds_changes_legacy_pre0061)   AS legacy_odds,
  (SELECT count(*) FROM odds_changes)                   AS new_odds;
```

The `new_*` counts may legitimately be **larger** than `legacy_*`
because writers have been adding fresh rows the whole time. The
expectation is:

    new_lines >= legacy_lines
    new_odds  >= legacy_odds - (small race window)

If `new_lines` is materially *less* than `legacy_lines`, re-run the
backfill with `--reset` and investigate.

## Step 3 — Disable the old task #757 retention loops

The pre-existing archive-then-delete loop for `line_snapshots` (and the
mirror loop for `odds_changes` if it was wired in) still runs against
the same table names — now the partitioned parents — and will issue
expensive multi-million-row DELETEs that the partition-drop loop is
meant to replace.

Before enabling the new loop in Step 4, **unset** these env vars so
the old loops stay disabled forever (they default to disabled, but
some prod deploys had them explicitly turned on):

```bash
unset AXIOM_LINE_SNAPSHOTS_RETENTION_ENABLED
unset AXIOM_ODDS_CHANGES_RETENTION_ENABLED
# leave the *_PARTITIONED_RETENTION_ENABLED knobs alone — those are
# the new ones we're about to flip on.
```

If you can't unset them (managed env), set them to `0` explicitly.

## Step 4 — Enable the partition-drop retention loop

Once you've verified counts and confirmed the operator dashboard is
showing live data correctly, flip the env knobs to start reclaiming
storage:

```bash
# 30 days is the audit-recommended default; tune per table if your
# backtests need longer windows.
export LINE_SNAPSHOT_RETENTION_DAYS=30
export ODDS_CHANGES_RETENTION_DAYS=30

export AXIOM_LINE_SNAPSHOTS_PARTITIONED_RETENTION_ENABLED=1
export AXIOM_ODDS_CHANGES_PARTITIONED_RETENTION_ENABLED=1
```

After the next pod restart, `app.partitioned_table_retention` will:
- Pre-create the next ~2 months of partitions on each tick.
- DETACH + DROP any partition whose upper bound is older than the
  retention cutoff.
- Log every action (`axiom.partitioned_retention` logger).

The first sweep happens within `AXIOM_PARTITIONED_RETENTION_INTERVAL_SECONDS`
(default `86400` — daily).

## Step 5 — Drop the legacy heaps (after confirmation)

After at least one full retention sweep has run and you can see the
expected drop on the Supabase storage panel, the legacy heaps can be
removed. Do this manually so a bug here can never auto-destroy data:

```sql
DROP TABLE IF EXISTS line_snapshots_legacy_pre0061;
DROP TABLE IF EXISTS odds_changes_legacy_pre0061;
```

If you want to keep one cold-storage backup first, archive via
`scripts/archive_and_prune_backlog.py` and let it push gzip-JSONL into
the Supabase Storage bucket before dropping.

## Rollback

`alembic downgrade -1` will:
- Drop the partitioned tables (and any rows written to them since
  the migration).
- Rename `_legacy_pre0061` back to the original names.

Use only if the cutover has obviously gone wrong; you will lose every
row inserted into the partitioned tables since the migration ran.
