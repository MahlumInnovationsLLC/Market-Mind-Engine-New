# Supabase growth retention runbook (task #757)

This runbook covers the per-table retention loops added by task #757
to keep Supabase Postgres storage bounded.  Two tiers:

1. **Archive then delete** — for tables we still want re-trainable
   later (predictions, market_price_ticks, line_snapshots, etc.).
   Default horizon is **90 days**.  Older rows are streamed to the
   Supabase Storage bucket as gzip-compressed JSONL with a JSON
   manifest sidecar before being deleted from Postgres.
2. **Delete only** — for tables nothing downstream needs long-term
   (pipeline_events, settlement_events, audit logs, etc.).  Default
   horizon is **30 days**.  Pure ``DELETE`` past the cutoff.

All loops default **DISABLED**.  This is intentional: a fresh deploy
must not auto-trigger destructive sweeps before the operator has
reviewed the dry-run for the actual production backlog.

## Loop registry

The full inventory lives in
[`app/retention_loops_v757.py`](../app/retention_loops_v757.py) under
``ARCHIVE_MODULES`` and ``DELETE_ONLY_MODULES``.

| Flavor | Tables |
|--------|--------|
| Archive (90d default) | predictions, market_price_ticks, autoresearch_features, tech_evidence, line_snapshots, sportradar_tournament_replays, precip_emos_observations |
| Delete-only (30d default) | pipeline_events, settlement_events, mobile_notification_log, bankroll_events, rc_events, stripe_events, rc_webhook_deliveries, billing_churn_events, admin_audit_log, position_drift_resolutions, order_recovery_attempts, stale_cancel_observations, vertical_halt_history, gatekeeper_setting_history, model_rollback_log |

The pre-existing retention loops (``crypto_ohlcv``, ``order_fills``,
``free_tier_views``, ``crypto_backtest``, ``sportradar_leaderboards``)
predate task #757 and keep their own historical defaults — they are
**not** managed by this registry.

## Env knobs

Each loop reads three env vars, prefixed with ``AXIOM_<TABLE>_``:

| Suffix | Type | Default | Notes |
|--------|------|---------|-------|
| ``RETENTION_ENABLED`` | bool | ``0`` (disabled) | Set to anything other than ``0`` to enable. |
| ``RETENTION_DAYS`` | int | per-tier (90 / 30) | Floored at 1 day so a typo can't wipe the table. |
| ``CLEANUP_INTERVAL_SECONDS`` | float | 21600 (6h) | Floored at 300s. |

For example, to enable the predictions archive loop with a 60-day
horizon and a 12-hour cadence::

    AXIOM_PREDICTIONS_RETENTION_ENABLED=1
    AXIOM_PREDICTIONS_RETENTION_DAYS=60
    AXIOM_PREDICTIONS_CLEANUP_INTERVAL_SECONDS=43200

## Operator workflow

The recommended ordering for turning the loops on for the first time
is:

1. **Dry-run the backlog** to see what *would* move:

       python -m scripts.archive_and_prune_backlog --dry-run

   This iterates every loop, runs ``SELECT count(*) WHERE
   <ts_column> < cutoff`` per table, and prints a per-table backlog
   line.  Nothing is uploaded or deleted.  Per-table dry-run::

       python -m scripts.archive_and_prune_backlog --table predictions --dry-run

2. **Drain the backlog** ahead of enabling the loop, so the
   in-process sweep doesn't have to chew through millions of rows
   one chunk per tick.  Apply the changes only after reviewing
   step 1::

       python -m scripts.archive_and_prune_backlog --apply --max-rows 1000000

   Run repeatedly until ``moved_total=0`` if you'd rather chunk the
   apply across multiple maintenance windows.

3. **Enable the in-process loop** by setting
   ``AXIOM_<TABLE>_RETENTION_ENABLED=1`` for the loop in question
   and restarting the api-server.  The loop picks up at the next
   tick (~6h later by default).

4. **Watch the dashboard.**  The
   ``/api/dashboard/supabase-growth-retention`` endpoint returns
   per-loop ``state()`` enriched with a derived ``health`` (``ok`` /
   ``stale`` / ``error`` / ``disabled``) plus aggregate ``counts``.
   A misbehaving loop also surfaces in the per-loop ``last_error``
   field.

## Storage layout

Archive loops upload to the existing Supabase Storage bucket
configured for ``app.storage`` (typically ``axiom-predict-artifacts``).
Layout, per chunk:

    archives/<table>/dt=YYYY-MM-DD/part-<n>.jsonl.gz
    archives/<table>/dt=YYYY-MM-DD/part-<n>.manifest.json

Where ``YYYY-MM-DD`` is the **UTC date the sweep ran** (not the
cutoff date).  The manifest sidecar records:

* ``table``, ``ts_column``, ``cutoff``
* ``rows``, ``bytes_compressed``, ``bytes_uncompressed``
* ``chunk_index``, ``object_key``
* ``pk_columns``
* ``started_at``, ``finished_at``

Per-chunk transactions: the upload + manifest write happen first,
then the matching ``DELETE``.  An interrupted run leaves at most one
duplicate object for the in-flight chunk, which the next run upserts
over.

## Re-importing an archive

The on-disk format is plain gzip JSONL (one JSON object per line),
keyed by the original column names.  Datetimes are ISO-8601 strings,
``Decimal`` columns are rendered as strings, and raw ``bytes``
columns are wrapped as ``{"__bytes_b64__": "<base64>"}``.

Skeleton restore (PG-bound, replace ``<table>`` and edit the
``INSERT`` to match the schema)::

    import base64, gzip, json, asyncpg
    rows = []
    for line in gzip.open("part-0000.jsonl.gz", "rt"):
        d = json.loads(line)
        for k, v in list(d.items()):
            if isinstance(v, dict) and "__bytes_b64__" in v:
                d[k] = base64.b64decode(v["__bytes_b64__"])
        rows.append(d)
    # …feed `rows` into a parameterised INSERT …

For the common case of "I just want to grep an archived row for
audit purposes," ``zcat archives/<table>/dt=*/part-*.jsonl.gz | jq
'select(.id == "…")'`` is enough.

## Failure modes

* **Bucket upload fails** — the loop logs ``last_error``, no rows
  are deleted, and the next tick retries from the same point.
* **Delete fails after a successful upload** — the next tick
  re-uploads the same chunk (``upsert=True``) and re-attempts the
  delete, so the bucket may briefly contain a duplicate object but
  no row is ever lost.
* **Loop misconfigured** (``DAYS=0``) — the helper floors the
  retention horizon at 1 day so the worst case is a one-day-old
  cutoff, not a full table wipe.
* **Backlog too large for one tick** —
  ``DEFAULT_MAX_ROWS_PER_RUN`` (100k rows) caps each sweep so the
  loop stays bounded; the next tick picks up the remainder.

## Disabling a loop in flight

Set ``AXIOM_<TABLE>_RETENTION_ENABLED=0`` and restart the
api-server.  The lifespan ``stop_all`` hook awaits each loop's
``stop()`` so the in-flight sweep finishes its current chunk
cleanly before the process exits.
