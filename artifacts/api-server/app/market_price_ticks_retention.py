"""Periodic archive-then-prune for ``market_price_ticks`` (task #757).

Kalshi candlestick cache.  Refreshed continuously by the tick-cache
loop; old buckets are dead weight on disk but still useful as
training data, so archive then prune.

Archive flavor: rows older than the configured horizon are streamed
to the Supabase Storage bucket as gzip-compressed JSONL under
``archives/market_price_ticks/dt=YYYY-MM-DD/part-<n>.jsonl.gz`` (with a
sibling ``part-<n>.manifest.json`` sidecar) before being deleted from
Postgres.  See :mod:`app.archive_helper` for the chunking +
interruption semantics, and ``docs/retention_runbook.md`` for the
re-import recipe.

Defaults (operator-overridable via env):
  * ``AXIOM_MARKET_PRICE_TICKS_RETENTION_ENABLED``         (default 0 — disabled)
  * ``AXIOM_MARKET_PRICE_TICKS_RETENTION_DAYS``            (default 90)
  * ``AXIOM_MARKET_PRICE_TICKS_CLEANUP_INTERVAL_SECONDS``  (default 21600 — 6h)

The loop is **disabled by default** so a fresh deploy doesn't
auto-trigger the sweep before the operator has reviewed the dry-run
output from ``scripts/archive_and_prune_backlog.py``.
"""
from __future__ import annotations

from typing import Any

from .retention_base import ArchiveThenDeleteRetention


_loop = ArchiveThenDeleteRetention(
    name='market_price_ticks',
    table='market_price_ticks',
    ts_column='bucket_ts',
    env_prefix='AXIOM_MARKET_PRICE_TICKS',
    default_days=90,
    pk_columns=('market_ticker', 'period_interval', 'bucket_ts'),
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
