"""Supabase Postgres connection pool (asyncpg).

Sizing (task #922)
------------------
Defaults: ``min_size=5, max_size=20``.  Supabase's pgbouncer in the
recommended *transaction* mode caps client-side concurrency at the
project's pool quota (15 on Free, 200 on Pro Small, 400+ on Pro/Team).
Twenty is comfortable on Pro Small and still works on Free as long as
the operator hasn't also opened a long-lived SQL editor session.
Override with ``AXIOM_DB_POOL_MIN`` / ``AXIOM_DB_POOL_MAX``.

Idle connections are recycled every ``AXIOM_DB_POOL_RECYCLE_SECONDS``
(default 300) so pgbouncer's server-side reapers don't tear them down
mid-query, which was a source of "connection reset" noise.

Every helper in this module wraps ``pool.acquire()`` in
``asyncio.wait_for(..., timeout=acquire_timeout)`` — a hung pool now
raises ``PoolAcquireTimeout`` (a ``TimeoutError`` subclass) instead of
deadlocking forever. Per-query work is still bounded by
``command_timeout`` on the pool itself.

Pool saturation telemetry (read by ``app.health``) is exposed via
:func:`pool_stats` and the running ``_saturation_since`` clock — when
``freesize() == 0`` for more than 5s the health probe flips DEGRADED.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from .config import get_settings


log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("invalid int for %s=%r; using default %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("invalid float for %s=%r; using default %s", name, raw, default)
        return default


# Public knobs (also used by tests asserting the live shape).
POOL_MIN_SIZE = _env_int("AXIOM_DB_POOL_MIN", 5)
POOL_MAX_SIZE = _env_int("AXIOM_DB_POOL_MAX", 20)
COMMAND_TIMEOUT_SECONDS = _env_float("AXIOM_DB_COMMAND_TIMEOUT", 30.0)
ACQUIRE_TIMEOUT_SECONDS = _env_float("AXIOM_DB_ACQUIRE_TIMEOUT", 10.0)
MAX_INACTIVE_CONNECTION_LIFETIME = _env_float(
    "AXIOM_DB_POOL_RECYCLE_SECONDS", 300.0
)


class PoolAcquireTimeout(asyncio.TimeoutError):
    """Raised when ``pool.acquire()`` blocks past ``acquire_timeout``.

    Inherits ``TimeoutError`` so callers that already catch
    ``asyncio.TimeoutError`` / ``TimeoutError`` keep working.
    """


# --- saturation tracking -------------------------------------------------
# Wall-clock time at which the pool was first observed with zero free
# slots in the current contiguous saturation window. None when the pool
# currently has at least one free slot. Read by app.health.
_saturation_since: float | None = None


def _refresh_saturation(pool: asyncpg.Pool) -> None:
    """Update _saturation_since based on the pool's current freesize.

    Called from every helper *after* the connection has been released
    back to the pool so the count reflects steady state, not the in-
    flight query.
    """
    global _saturation_since
    try:
        free = pool.get_idle_size()  # asyncpg >= 0.27
    except Exception:
        try:
            free = pool._queue.qsize()  # type: ignore[attr-defined]
        except Exception:
            return
    if free <= 0:
        if _saturation_since is None:
            _saturation_since = time.time()
    else:
        _saturation_since = None


def pool_stats() -> dict[str, Any]:
    """Snapshot of pool sizing + current saturation state (no DB calls)."""
    p = _pool
    stats: dict[str, Any] = {
        "configured_min": POOL_MIN_SIZE,
        "configured_max": POOL_MAX_SIZE,
        "command_timeout": COMMAND_TIMEOUT_SECONDS,
        "acquire_timeout": ACQUIRE_TIMEOUT_SECONDS,
        "recycle_seconds": MAX_INACTIVE_CONNECTION_LIFETIME,
        "initialized": p is not None,
        "size": None,
        "free": None,
        "in_use": None,
        "saturated_for_seconds": None,
    }
    if p is None:
        return stats
    try:
        size = p.get_size()
        free = p.get_idle_size()
        stats["size"] = size
        stats["free"] = free
        stats["in_use"] = max(0, size - free)
    except Exception:
        pass
    if _saturation_since is not None:
        stats["saturated_for_seconds"] = round(time.time() - _saturation_since, 2)
    return stats


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        settings = get_settings()
        log.info(
            "Opening asyncpg pool to Supabase Postgres "
            "(min=%s max=%s command_timeout=%ss acquire_timeout=%ss "
            "recycle=%ss)",
            POOL_MIN_SIZE, POOL_MAX_SIZE, COMMAND_TIMEOUT_SECONDS,
            ACQUIRE_TIMEOUT_SECONDS, MAX_INACTIVE_CONNECTION_LIFETIME,
        )
        _pool = await asyncpg.create_pool(
            dsn=settings.supabase_db_dsn,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            command_timeout=COMMAND_TIMEOUT_SECONDS,
            max_inactive_connection_lifetime=MAX_INACTIVE_CONNECTION_LIFETIME,
            ssl="require",
        )
        return _pool


async def close_pool() -> None:
    global _pool, _saturation_since
    if _pool is not None:
        log.info("Closing asyncpg pool")
        await _pool.close()
        _pool = None
    _saturation_since = None


@asynccontextmanager
async def acquire(
    *, timeout: float | None = None,
) -> AsyncIterator[asyncpg.Connection]:
    """Borrow a connection from the pool with a bounded wait.

    A hung pool used to make this hang forever; now it raises
    :class:`PoolAcquireTimeout` after ``timeout`` seconds (default
    :data:`ACQUIRE_TIMEOUT_SECONDS`).
    """
    pool = await get_pool()
    t = ACQUIRE_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        conn = await asyncio.wait_for(pool.acquire(), timeout=t)
    except asyncio.TimeoutError as e:
        raise PoolAcquireTimeout(
            f"asyncpg pool acquire timed out after {t}s "
            f"(stats={pool_stats()})"
        ) from e
    try:
        yield conn
    finally:
        try:
            await pool.release(conn)
        finally:
            _refresh_saturation(pool)


async def fetchval(query: str, *args, timeout: float | None = None):
    async with acquire(timeout=timeout) as c:
        return await c.fetchval(query, *args)


async def fetch(query: str, *args, timeout: float | None = None):
    async with acquire(timeout=timeout) as c:
        return await c.fetch(query, *args)


async def fetchrow(query: str, *args, timeout: float | None = None):
    async with acquire(timeout=timeout) as c:
        return await c.fetchrow(query, *args)


async def execute(query: str, *args, timeout: float | None = None) -> str:
    async with acquire(timeout=timeout) as c:
        return await c.execute(query, *args)


async def executemany(query: str, args_list, *, timeout: float | None = None) -> None:
    async with acquire(timeout=timeout) as c:
        await c.executemany(query, args_list)
