"""Supabase Postgres connection pool (asyncpg).

Pool is sized conservatively (min 2, max 10) due to Supabase IPv4 connection
limits. Increase when running under the IPv4 add-on with sufficient quota.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from .config import get_settings


log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool
        settings = get_settings()
        log.info("Opening asyncpg pool to Supabase Postgres")
        _pool = await asyncpg.create_pool(
            dsn=settings.supabase_db_dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
            ssl="require",
        )
        return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        log.info("Closing asyncpg pool")
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def fetchval(query: str, *args):
    async with acquire() as c:
        return await c.fetchval(query, *args)


async def fetch(query: str, *args):
    async with acquire() as c:
        return await c.fetch(query, *args)


async def fetchrow(query: str, *args):
    async with acquire() as c:
        return await c.fetchrow(query, *args)
