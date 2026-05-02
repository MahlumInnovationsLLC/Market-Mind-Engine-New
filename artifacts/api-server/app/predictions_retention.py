"""Periodic archive-then-prune for ``predictions`` (task #757).

Per-cycle predictions append-with-upsert table.  Most rows are
short-lived (one per Kalshi market we evaluated) but the row count is
the candidate-set size on every cycle, so without a TTL the table
grows linearly with traffic.  Archive then prune so backtests can
still mine the historical set later.

Archive flavor: rows older than the configured horizon are streamed
to the Supabase Storage bucket as gzip-compressed JSONL under
``archives/predictions/dt=YYYY-MM-DD/part-<n>.jsonl.gz`` (with a
sibling ``part-<n>.manifest.json`` sidecar) before being deleted from
Postgres.  See :mod:`app.archive_helper` for the chunking +
interruption semantics, and ``docs/retention_runbook.md`` for the
re-import recipe.

Defaults (operator-overridable via env):
  * ``AXIOM_PREDICTIONS_RETENTION_ENABLED``         (default 0 — disabled)
  * ``AXIOM_PREDICTIONS_RETENTION_DAYS``            (default 90)
  * ``AXIOM_PREDICTIONS_CLEANUP_INTERVAL_SECONDS``  (default 21600 — 6h)

The loop is **disabled by default** so a fresh deploy doesn't
auto-trigger the sweep before the operator has reviewed the dry-run
output from ``scripts/archive_and_prune_backlog.py``.
"""
from __future__ import annotations

from typing import Any

from .retention_base import ArchiveThenDeleteRetention


_loop = ArchiveThenDeleteRetention(
    name='predictions',
    table='predictions',
    ts_column='created_at',
    env_prefix='AXIOM_PREDICTIONS',
    default_days=90,
    pk_columns=('id',),
)


async def start() -> None:
    await _loop.start()


async def stop() -> None:
    await _loop.stop()


async def cleanup_once() -> int:
    return await _loop.cleanup_once()


def state() -> dict[str, Any]:
    return _loop.state()


def cutoff_iso() -> str:
    return _loop.cutoff().isoformat()


def _reset_state_for_tests() -> None:
    _loop._reset_state_for_tests()
