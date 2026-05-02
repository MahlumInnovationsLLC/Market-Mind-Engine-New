"""Periodic archive-then-prune for ``sportradar_tournament_replays`` (task #757).

Sportradar replay summaries appended on every poll-and-recover run;
archive then prune so the replay audit trail is recoverable.

Archive flavor: rows older than the configured horizon are streamed
to the Supabase Storage bucket as gzip-compressed JSONL under
``archives/sportradar_tournament_replays/dt=YYYY-MM-DD/part-<n>.jsonl.gz`` (with a
sibling ``part-<n>.manifest.json`` sidecar) before being deleted from
Postgres.  See :mod:`app.archive_helper` for the chunking +
interruption semantics, and ``docs/retention_runbook.md`` for the
re-import recipe.

Defaults (operator-overridable via env):
  * ``AXIOM_SPORTRADAR_TOURNAMENT_REPLAYS_RETENTION_ENABLED``         (default 0 — disabled)
  * ``AXIOM_SPORTRADAR_TOURNAMENT_REPLAYS_RETENTION_DAYS``            (default 90)
  * ``AXIOM_SPORTRADAR_TOURNAMENT_REPLAYS_CLEANUP_INTERVAL_SECONDS``  (default 21600 — 6h)

The loop is **disabled by default** so a fresh deploy doesn't
auto-trigger the sweep before the operator has reviewed the dry-run
output from ``scripts/archive_and_prune_backlog.py``.
"""
from __future__ import annotations

from typing import Any

from .retention_base import ArchiveThenDeleteRetention


_loop = ArchiveThenDeleteRetention(
    name='sportradar_tournament_replays',
    table='sportradar_tournament_replays',
    ts_column='started_at',
    env_prefix='AXIOM_SPORTRADAR_TOURNAMENT_REPLAYS',
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
