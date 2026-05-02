"""Periodic delete-only retention for ``stale_cancel_observations`` (task #757).

Stale-cancel observation log.

Pure ``DELETE`` past the horizon — no archive, since this table was
never intended to be re-trained on.

Defaults (operator-overridable via env):
  * ``AXIOM_STALE_CANCEL_OBSERVATIONS_RETENTION_ENABLED``         (default 0 — disabled)
  * ``AXIOM_STALE_CANCEL_OBSERVATIONS_RETENTION_DAYS``            (default 30)
  * ``AXIOM_STALE_CANCEL_OBSERVATIONS_CLEANUP_INTERVAL_SECONDS``  (default 21600 — 6h)

The loop is **disabled by default** so a fresh deploy doesn't
auto-trigger the sweep before the operator has reviewed the dry-run
output from ``scripts/archive_and_prune_backlog.py``.
"""
from __future__ import annotations

from typing import Any

from .retention_base import DeleteOnlyRetention


_loop = DeleteOnlyRetention(
    name='stale_cancel_observations',
    table='stale_cancel_observations',
    ts_column='observed_at',
    env_prefix='AXIOM_STALE_CANCEL_OBSERVATIONS',
    default_days=30,
    default_interval_seconds=6 * 3600,
)


async def start() -> None:
    await _loop.start()


async def stop() -> None:
    await _loop.stop()


async def cleanup_once() -> int:
    return await _loop.cleanup_once()


def state() -> dict[str, Any]:
    return _loop.state()


def _reset_state_for_tests() -> None:
    _loop._reset_state_for_tests()
