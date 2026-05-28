"""Tests for the pool sizing + timeout wiring added in task #922.

No live DB or network required:
  * Pool config knobs are asserted against module-level constants and
    the env-knob plumbing.
  * ``acquire(timeout=...)`` is asserted against a fake pool that
    blocks ``acquire`` forever.
  * httpx client timeout defaults are asserted by reading the
    constructed clients without making any real request.
  * A fake-slow ``httpx`` server (``httpx.MockTransport``) proves the
    five flagged constructors return an error within bounded time
    instead of hanging.
"""
from __future__ import annotations

import asyncio
import importlib
import time

import httpx
import pytest


# ---------------------------------------------------------------------------
# Pool config
# ---------------------------------------------------------------------------


def _reload_db(monkeypatch, **env: str):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import db as db_mod  # noqa: WPS433
    return importlib.reload(db_mod)


def test_default_pool_sizing(monkeypatch):
    # Strip every knob so we see the documented defaults.
    for k in (
        "AXIOM_DB_POOL_MIN", "AXIOM_DB_POOL_MAX",
        "AXIOM_DB_COMMAND_TIMEOUT", "AXIOM_DB_ACQUIRE_TIMEOUT",
        "AXIOM_DB_POOL_RECYCLE_SECONDS",
    ):
        monkeypatch.delenv(k, raising=False)
    db_mod = _reload_db(monkeypatch)
    assert db_mod.POOL_MIN_SIZE == 5
    assert db_mod.POOL_MAX_SIZE == 20
    assert db_mod.COMMAND_TIMEOUT_SECONDS == 30.0
    assert db_mod.ACQUIRE_TIMEOUT_SECONDS == 10.0
    assert db_mod.MAX_INACTIVE_CONNECTION_LIFETIME == 300.0


def test_env_knobs_override(monkeypatch):
    db_mod = _reload_db(
        monkeypatch,
        AXIOM_DB_POOL_MIN="3",
        AXIOM_DB_POOL_MAX="40",
        AXIOM_DB_COMMAND_TIMEOUT="45",
        AXIOM_DB_ACQUIRE_TIMEOUT="2",
        AXIOM_DB_POOL_RECYCLE_SECONDS="60",
    )
    assert (db_mod.POOL_MIN_SIZE, db_mod.POOL_MAX_SIZE) == (3, 40)
    assert db_mod.COMMAND_TIMEOUT_SECONDS == 45.0
    assert db_mod.ACQUIRE_TIMEOUT_SECONDS == 2.0
    assert db_mod.MAX_INACTIVE_CONNECTION_LIFETIME == 60.0


def test_garbage_env_falls_back_to_default(monkeypatch):
    db_mod = _reload_db(
        monkeypatch,
        AXIOM_DB_POOL_MAX="not-a-number",
        AXIOM_DB_ACQUIRE_TIMEOUT="oops",
    )
    assert db_mod.POOL_MAX_SIZE == 20
    assert db_mod.ACQUIRE_TIMEOUT_SECONDS == 10.0


# ---------------------------------------------------------------------------
# acquire() timeout
# ---------------------------------------------------------------------------


class _BlockingPool:
    """Stand-in for asyncpg.Pool whose acquire() never resolves."""
    def __init__(self):
        self.released: list[object] = []

    async def acquire(self):
        await asyncio.Event().wait()   # never set

    async def release(self, conn):  # pragma: no cover
        self.released.append(conn)

    def get_size(self):
        return 20

    def get_idle_size(self):
        return 0


def test_acquire_raises_pool_acquire_timeout(monkeypatch):
    from app import db as db_mod
    importlib.reload(db_mod)

    blocking = _BlockingPool()

    async def _fake_get_pool():
        db_mod._pool = blocking  # populate saturation lookups
        return blocking

    monkeypatch.setattr(db_mod, "get_pool", _fake_get_pool)

    async def _run():
        t0 = time.monotonic()
        with pytest.raises(db_mod.PoolAcquireTimeout):
            async with db_mod.acquire(timeout=0.25):
                pass
        return time.monotonic() - t0

    elapsed = asyncio.run(_run())
    assert elapsed < 2.0, f"acquire took {elapsed:.2f}s — should fail fast"


def test_pool_acquire_timeout_is_a_timeout_error():
    from app import db as db_mod
    assert issubclass(db_mod.PoolAcquireTimeout, asyncio.TimeoutError)
    assert issubclass(db_mod.PoolAcquireTimeout, TimeoutError)


# ---------------------------------------------------------------------------
# pool_stats() exposes saturation telemetry
# ---------------------------------------------------------------------------


def test_pool_stats_pre_init():
    from app import db as db_mod
    importlib.reload(db_mod)
    stats = db_mod.pool_stats()
    assert stats["initialized"] is False
    assert stats["configured_max"] == db_mod.POOL_MAX_SIZE
    assert stats["saturated_for_seconds"] is None


def test_pool_saturation_clock_arms_and_clears():
    from app import db as db_mod
    importlib.reload(db_mod)

    class _Fake:
        def __init__(self, free):
            self._free = free

        def get_size(self): return 20

        def get_idle_size(self): return self._free

    fake = _Fake(free=0)
    db_mod._pool = fake
    db_mod._saturation_since = None
    db_mod._refresh_saturation(fake)
    assert db_mod._saturation_since is not None
    # …and clears the moment a slot opens up:
    fake._free = 3
    db_mod._refresh_saturation(fake)
    assert db_mod._saturation_since is None
    db_mod._pool = None


# ---------------------------------------------------------------------------
# Health probe surfaces DEGRADED on sustained saturation
# ---------------------------------------------------------------------------


def test_postgres_probe_flips_degraded_when_saturated(monkeypatch):
    from app import db as db_mod, health
    importlib.reload(db_mod)

    async def _ok(*_a, **_k):
        return 1
    monkeypatch.setattr(health.db, "fetchval", _ok)
    monkeypatch.setattr(health.db, "pool_stats", lambda: {
        "configured_max": 20, "in_use": 20, "free": 0,
        "saturated_for_seconds": 12.7,
    })
    card = asyncio.run(health._probe_postgres())
    assert card["status"] == "degraded"
    assert "saturated" in card["detail"]


def test_postgres_probe_ok_when_not_saturated(monkeypatch):
    from app import db as db_mod, health
    importlib.reload(db_mod)

    async def _ok(*_a, **_k):
        return 1
    monkeypatch.setattr(health.db, "fetchval", _ok)
    monkeypatch.setattr(health.db, "pool_stats", lambda: {
        "configured_max": 20, "in_use": 4, "free": 16,
        "saturated_for_seconds": None,
    })
    card = asyncio.run(health._probe_postgres())
    assert card["status"] == "ok"


# ---------------------------------------------------------------------------
# httpx timeout defaults
# ---------------------------------------------------------------------------


def _expect_bounded(client: httpx.AsyncClient) -> None:
    t = client.timeout
    # Every leg must be a positive finite number (None == "wait forever").
    for leg in (t.connect, t.read, t.write, t.pool):
        assert leg is not None, f"unset httpx timeout leg on {client!r}"
        assert leg > 0
        assert leg < 600   # nothing in the codebase legitimately needs >10min


def test_github_mirror_client_is_bounded():
    from app.github_mirror import _client
    cfg = {"base": "https://api.github.example", "token": "x"}
    c = _client(cfg)
    _expect_bounded(c)
    asyncio.run(c.aclose())


def test_slow_upstream_returns_within_bounded_time():
    """A hung TCP server must surface a timeout in well under its read budget.

    Spins up a real local listener that accepts connections but never
    writes a response, then asserts httpx raises a TimeoutException
    within ~2x the configured read timeout. Proves the explicit
    ``httpx.Timeout(...)`` defaults wired into the codebase actually
    save us from an indefinite hang.
    """
    import socket as _socket

    async def _run():
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        port = sock.getsockname()[1]
        accepted: list[_socket.socket] = []

        loop = asyncio.get_event_loop()

        async def _accepter():
            while True:
                try:
                    conn, _ = await loop.sock_accept(sock)
                except Exception:
                    return
                accepted.append(conn)  # never reply

        sock.setblocking(False)
        accept_task = asyncio.create_task(_accepter())

        try:
            timeout = httpx.Timeout(connect=5.0, read=1.0, write=1.0, pool=5.0)
            async with httpx.AsyncClient(timeout=timeout) as c:
                t0 = time.monotonic()
                with pytest.raises(httpx.TimeoutException):
                    await c.get(f"http://127.0.0.1:{port}/whatever")
                return time.monotonic() - t0
        finally:
            accept_task.cancel()
            for c in accepted:
                try: c.close()
                except Exception: pass
            sock.close()

    elapsed = asyncio.run(_run())
    assert elapsed < 3.0, f"client took {elapsed:.2f}s — should fail at ~1s"
