"""Periodic archive-then-prune for ``autoresearch_features`` (task #757).

AutoResearch nightly feature mining output.  Long-tail rows in shadow
status accumulate forever; archive then prune so the promote / reject
decision history is recoverable.

Archive flavor: rows older than the configured horizon are streamed
to the Supabase Storage bucket as gzip-compressed JSONL under
``archives/autoresearch_features/dt=YYYY-MM-DD/part-<n>.jsonl.gz`` (with a
sibling ``part-<n>.manifest.json`` sidecar) before being deleted from
Postgres.  See :mod:`app.archive_helper` for the chunking +
interruption semantics, and ``docs/retention_runbook.md`` for the
re-import recipe.

Defaults (operator-overridable via env):
  * ``AXIOM_AUTORESEARCH_FEATURES_RETENTION_ENABLED``         (default 0 — disabled)
  * ``AXIOM_AUTORESEARCH_FEATURES_RETENTION_DAYS``            (default 90)
  * ``AXIOM_AUTORESEARCH_FEATURES_CLEANUP_INTERVAL_SECONDS``  (default 21600 — 6h)

The loop is **disabled by default** so a fresh deploy doesn't
auto-trigger the sweep before the operator has reviewed the dry-run
output from ``scripts/archive_and_prune_backlog.py``.
"""
from __future__ import annotations

from typing import Any

from .retention_base import ArchiveThenDeleteRetention


_loop = ArchiveThenDeleteRetention(
    name='autoresearch_features',
    table='autoresearch_features',
    ts_column='created_at',
    env_prefix='AXIOM_AUTORESEARCH_FEATURES',
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
