"""Tests for the cycle-start stale-resting-order sweep (task #270).

The orchestrator cancels resting orders older than
``AXIOM_RESTING_ORDER_MAX_AGE_SECS`` (with per-vertical override support)
before evaluating new candidates. Cancel failures must be swallowed so a
flaky venue cannot park the orchestrator, and the ``stale_resting_cancelled``
counter must surface on ``/api/health/snapshot``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.kalshi import lifecycle as kalshi_lifecycle
from app.orchestrator import cycle as cycle_mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    kalshi_lifecycle._reset_stale_resting_state()
    monkeypatch.delenv("AXIOM_RESTING_ORDER_MAX_AGE_SECS", raising=False)
    yield
    kalshi_lifecycle._reset_stale_resting_state()


def _make_record(order_id: str, age_secs: float, vertical: str = "sports"):
    req = SimpleNamespace(
        vertical=vertical,
        limit_price_cents=50,
    )
    return SimpleNamespace(
        order_id=order_id,
        request=req,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_secs),
    )


def _install_resting(monkeypatch, records, cancel_side_effect=None):
    """Monkeypatch list_resting_orders + cancel_order to use `records`."""
    cancelled: list[str] = []
    store = {r.order_id: r for r in records}

    async def _fake_list():
        return list(store.values())

    async def _fake_cancel(order_id: str):
        if cancel_side_effect is not None:
            effect = cancel_side_effect(order_id)
            if isinstance(effect, Exception):
                raise effect
        store.pop(order_id, None)
        cancelled.append(order_id)
        return {"order_id": order_id, "state": "canceled"}

    monkeypatch.setattr(kalshi_lifecycle, "list_resting_orders", _fake_list)
    monkeypatch.setattr(kalshi_lifecycle, "cancel_order", _fake_cancel)
    return cancelled


# ---------------------------------------------------------------------------
# _resolve_resting_max_age precedence
# ---------------------------------------------------------------------------

def test_resolve_age_default(monkeypatch):
    monkeypatch.delenv("AXIOM_RESTING_ORDER_MAX_AGE_SECS", raising=False)
    assert cycle_mod._resolve_resting_max_age("sports", {}) == float(
        cycle_mod.DEFAULT_RESTING_ORDER_MAX_AGE_SECS
    )


def test_resolve_age_env(monkeypatch):
    monkeypatch.setenv("AXIOM_RESTING_ORDER_MAX_AGE_SECS", "90")
    assert cycle_mod._resolve_resting_max_age("sports", {}) == 90.0


def test_resolve_age_global_override_beats_env(monkeypatch):
    monkeypatch.setenv("AXIOM_RESTING_ORDER_MAX_AGE_SECS", "90")
    ov = {"resting_order_max_age_secs": 45}
    assert cycle_mod._resolve_resting_max_age("sports", ov) == 45.0


def test_resolve_age_per_vertical_override_beats_global():
    ov = {
        "resting_order_max_age_secs": 45,
        "resting_order_max_age_secs.sports": 300,
    }
    assert cycle_mod._resolve_resting_max_age("sports", ov) == 300.0
    # Other verticals still see the global value.
    assert cycle_mod._resolve_resting_max_age("crypto", ov) == 45.0


# ---------------------------------------------------------------------------
# Sweep behavior
# ---------------------------------------------------------------------------

def test_sweep_clean_cycle(monkeypatch):
    """No resting orders -> sweep is a no-op but still counts the scan."""
    cancelled = _install_resting(monkeypatch, [])
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", {}))
    assert stats == {"scanned": 0, "cancelled": 0, "failed": 0, "max_age": 30}
    assert cancelled == []
    state = kalshi_lifecycle.stale_resting_cancelled_state()
    assert state["scans"] == 1
    assert state["cancelled"] == 0


def test_sweep_cancels_stale_keeps_fresh(monkeypatch):
    """One stale, one fresh -> only the stale order is cancelled."""
    stale = _make_record("stale-1", age_secs=120)
    fresh = _make_record("fresh-1", age_secs=5)
    cancelled = _install_resting(monkeypatch, [stale, fresh])
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", {}))
    assert cancelled == ["stale-1"]
    assert stats["scanned"] == 2
    assert stats["cancelled"] == 1
    assert stats["failed"] == 0
    state = kalshi_lifecycle.stale_resting_cancelled_state()
    assert state["cancelled"] == 1
    assert state["cancel_failed"] == 0


def test_sweep_swallows_cancel_failure(monkeypatch):
    """A flaky venue raises on cancel -> counter records failure, cycle continues."""
    stale_a = _make_record("flaky-a", age_secs=120)
    stale_b = _make_record("ok-b", age_secs=120)

    def _effect(order_id: str):
        if order_id == "flaky-a":
            return RuntimeError("HTTP 503 from venue")
        return None

    cancelled = _install_resting(monkeypatch, [stale_a, stale_b], cancel_side_effect=_effect)
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", {}))
    # ok-b still got cancelled — flaky-a's failure must not short-circuit the sweep.
    assert cancelled == ["ok-b"]
    assert stats["scanned"] == 2
    assert stats["cancelled"] == 1
    assert stats["failed"] == 1
    state = kalshi_lifecycle.stale_resting_cancelled_state()
    assert state["cancelled"] == 1
    assert state["cancel_failed"] == 1
    assert "flaky-a" in (state["last_error"] or "")


def test_sweep_respects_per_vertical_override(monkeypatch):
    """Per-vertical TTL override keeps older orders resting in that vertical."""
    old = _make_record("old-sports", age_secs=180)  # 3 minutes old
    cancelled = _install_resting(monkeypatch, [old])
    # Per-vertical override: sports orders may live up to 10 minutes.
    overrides = {"resting_order_max_age_secs.sports": 600}
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", overrides))
    assert cancelled == []
    assert stats["scanned"] == 1
    assert stats["cancelled"] == 0
    assert stats["max_age"] == 600


def test_sweep_only_touches_matching_vertical(monkeypatch):
    """Orders belonging to another vertical are skipped by this vertical's sweep."""
    sports_stale = _make_record("sports-stale", age_secs=120, vertical="sports")
    crypto_stale = _make_record("crypto-stale", age_secs=120, vertical="crypto")
    cancelled = _install_resting(monkeypatch, [sports_stale, crypto_stale])
    stats = _run(cycle_mod._cancel_stale_resting_orders("sports", {}))
    assert cancelled == ["sports-stale"]
    assert stats["scanned"] == 1  # crypto order was filtered out before counting


def test_sweep_disabled_when_max_age_zero(monkeypatch):
    """max_age <= 0 disables the sweep (opt-out)."""
    stale = _make_record("stale", age_secs=9999)
    cancelled = _install_resting(monkeypatch, [stale])
    stats = _run(cycle_mod._cancel_stale_resting_orders(
        "sports", {"resting_order_max_age_secs": 0}
    ))
    assert cancelled == []
    assert stats["cancelled"] == 0
    # Scan still recorded so operators see the sweep ran.
    assert kalshi_lifecycle.stale_resting_cancelled_state()["scans"] == 1


# ---------------------------------------------------------------------------
# Sweep runs even when the cycle is halted
# ---------------------------------------------------------------------------

def test_sweep_runs_before_halt_early_return(monkeypatch):
    """When trading_mode=halt the cycle skips placement, but existing
    resting orders must still be cancelled — otherwise halting the loop
    leaves stale orders live against an arbitrarily old thesis."""
    from app.orchestrator import cycle as cycle_mod_local
    from app import db as db_mod

    stale = _make_record("halt-stale", age_secs=120)
    cancelled = _install_resting(monkeypatch, [stale])

    # Stub every DB path the halted branch touches — most call sites
    # already wrap DB access in try/except so silent failures are safe,
    # but we want the test to exercise the real control flow.
    async def _noop_fetch(*a, **kw): return []
    async def _noop_fetchval(*a, **kw): return None
    async def _noop_execute(*a, **kw): return None
    monkeypatch.setattr(db_mod, "fetch", _noop_fetch)
    monkeypatch.setattr(db_mod, "fetchval", _noop_fetchval)
    monkeypatch.setattr(db_mod, "execute", _noop_execute)

    # trading_mode=halt forces the early return — the sweep must still run.
    settings = SimpleNamespace(
        trading_mode="halt",
        cycle_interval_seconds=60,
        max_fresh_debates_per_cycle=0,
        openai_api_key=None,
        anthropic_api_key=None,
    )
    monkeypatch.setattr(cycle_mod_local, "get_settings", lambda: settings)

    async def _noop_get_agg(): return "balanced"
    monkeypatch.setattr(cycle_mod_local, "get_aggression_mode", _noop_get_agg)

    monkeypatch.setattr(
        cycle_mod_local.predictor, "active_model_info",
        lambda: {"model_name": "test"},
    )

    result = _run(cycle_mod_local.run_cycle("sports"))
    assert any("trading_mode=halt" in e for e in result.errors), result.errors
    # The sweep must have cancelled the stale order before the halt return.
    assert cancelled == ["halt-stale"]
    state = kalshi_lifecycle.stale_resting_cancelled_state()
    assert state["scans"] == 1
    assert state["cancelled"] == 1


# ---------------------------------------------------------------------------
# Health snapshot exposure
# ---------------------------------------------------------------------------

def test_health_snapshot_exposes_counter():
    from app import health as health_mod
    kalshi_lifecycle._reset_stale_resting_state()
    kalshi_lifecycle.record_stale_resting_scan()
    kalshi_lifecycle.record_stale_resting_cancelled("abc")
    card = health_mod._probe_stale_resting_cancelled()
    assert card["key"] == "stale_resting_cancelled"
    assert card["category"] == "passive"
    assert card["status"] == "ok"
    assert card["extras"]["stale_resting_cancelled"] == 1
    assert card["extras"]["cancelled"] == 1


def test_health_snapshot_degrades_on_failure():
    from app import health as health_mod
    kalshi_lifecycle._reset_stale_resting_state()
    kalshi_lifecycle.record_stale_resting_cancel_failed("xyz", "HTTP 503")
    card = health_mod._probe_stale_resting_cancelled()
    assert card["status"] == "degraded"
    assert "HTTP 503" in card["detail"]


# ---------------------------------------------------------------------------
# End-to-end through run_cycle against a REAL Postgres instance.
#
# The unit-level tests above stub `compute_baseline` and `db.fetch` directly,
# so a future schema change to `stale_cancel_observations` (column rename,
# dropped index, type drift) would not be caught — the stub keeps returning
# the old shape. These tests stand up an isolated Postgres via initdb +
# pg_ctl (same pattern as test_position_rollup_sql.py), apply the real
# 0033 migration's schema, pre-seed N rows in `stale_cancel_observations`,
# then trigger one more cycle through `run_cycle` and assert:
#
#   1. The alert helper is (or isn't) dispatched with the right counts.
#   2. A new observation row is persisted for the current cycle, proving
#      the SQL contract for record_observation + the aggregation query
#      both line up with the live schema.
#
# Skips when no local Postgres binaries are on PATH (e.g. minimal sandbox).
# ---------------------------------------------------------------------------

import shutil as _shutil
import socket as _socket
import subprocess as _subprocess
import tempfile as _tempfile
import time as _time
from pathlib import Path as _Path

asyncpg = pytest.importorskip("asyncpg")


# Mirror of the 0033 migration. Kept inline (rather than running alembic
# upgrade against the temp DB) so the test stays self-contained and the
# migration's column shape is asserted against by the real INSERT/SELECT.
# A drift between this block and `migrations/versions/0033_*` is the
# explicit signal we're trying to catch — the test will fail loudly.
_STALE_CANCEL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stale_cancel_observations (
    id          bigserial PRIMARY KEY,
    observed_at timestamptz NOT NULL DEFAULT now(),
    vertical    varchar(64) NOT NULL,
    cycle_id    bigint,
    scanned     integer NOT NULL DEFAULT 0,
    cancelled   integer NOT NULL DEFAULT 0,
    failed      integer NOT NULL DEFAULT 0,
    max_age     integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stale_cancel_obs_vertical_time
    ON stale_cancel_observations (vertical, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_stale_cancel_obs_observed_at
    ON stale_cancel_observations (observed_at);

-- trading_cycles: minimal subset run_cycle uses (INSERT ... RETURNING id
-- at start, UPDATE ... WHERE id at close). Using a real table here means
-- the cycle_id we read back into the alert telemetry is a real bigserial,
-- not a fake constant — closer to production semantics.
CREATE TABLE IF NOT EXISTS trading_cycles (
    id                bigserial PRIMARY KEY,
    started_at        timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz,
    vertical          text NOT NULL,
    picks_evaluated   integer NOT NULL DEFAULT 0,
    trades_placed     integer NOT NULL DEFAULT 0,
    errors            text,
    settings_snapshot jsonb
);
"""


def _free_port_for_pg() -> int:
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def pg_dsn():
    """Module-scoped temp Postgres for the e2e tests.

    Skips the suite cleanly when the sandbox lacks `initdb`/`pg_ctl`.
    """
    initdb = _shutil.which("initdb")
    pg_ctl = _shutil.which("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("no local Postgres binaries on PATH")

    tmp = _Path(_tempfile.mkdtemp(prefix="axiom_stale_cancel_pg_"))
    data = tmp / "data"
    sock = tmp / "sock"
    sock.mkdir()
    log = tmp / "pg.log"
    port = _free_port_for_pg()

    _subprocess.check_call(
        [initdb, "-D", str(data), "-A", "trust", "-U", "postgres"],
        stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
    )
    opts = f"-p {port} -k {sock} -h 127.0.0.1"
    _subprocess.check_call(
        [pg_ctl, "-D", str(data), "-l", str(log), "-o", opts, "-w", "start"],
        stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
    )

    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    for _ in range(50):
        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            _time.sleep(0.1)

    try:
        yield dsn
    finally:
        _subprocess.call(
            [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
            stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
        )
        _shutil.rmtree(tmp, ignore_errors=True)


def _wire_real_db(monkeypatch, pool):
    """Re-point `app.db` helpers at the temp Postgres pool.

    Only the four async helpers used by the cycle tail need pointing —
    `get_pool`/`acquire`/etc. aren't touched by this code path.
    """
    from app import db as app_db

    async def _fetchval(q, *a):
        async with pool.acquire() as c:
            return await c.fetchval(q, *a)

    async def _fetch(q, *a):
        async with pool.acquire() as c:
            return await c.fetch(q, *a)

    async def _execute(q, *a):
        async with pool.acquire() as c:
            return await c.execute(q, *a)

    async def _fetchrow(q, *a):
        async with pool.acquire() as c:
            return await c.fetchrow(q, *a)

    monkeypatch.setattr(app_db, "fetchval", _fetchval)
    monkeypatch.setattr(app_db, "fetch", _fetch)
    monkeypatch.setattr(app_db, "execute", _execute)
    monkeypatch.setattr(app_db, "fetchrow", _fetchrow)


def _stub_cycle_settings_for_halt(monkeypatch):
    """Halt-mode short-circuits the cycle right after the sweep + anomaly
    + record_observation block, so we don't stand up the full risk
    pipeline. Same pattern used by `test_sweep_runs_before_halt_early_return`.
    """
    from app.orchestrator import cycle as cycle_mod_local

    settings = SimpleNamespace(
        trading_mode="halt",
        cycle_interval_seconds=60,
        max_fresh_debates_per_cycle=0,
        openai_api_key=None,
        anthropic_api_key=None,
    )
    monkeypatch.setattr(cycle_mod_local, "get_settings", lambda: settings)

    async def _noop_get_agg():
        return "balanced"

    monkeypatch.setattr(cycle_mod_local, "get_aggression_mode", _noop_get_agg)
    monkeypatch.setattr(
        cycle_mod_local.predictor, "active_model_info",
        lambda: {"model_name": "test"},
    )


def _capture_alert_dispatch(monkeypatch):
    """Replace the two stale-cancel alert helpers with capturing stubs and
    force `dedupe_or_skip` to always claim. Returns the calls dict."""
    from app.notifications import alerts as alerts_mod

    calls: dict[str, list[dict]] = {"failure": [], "volume": []}

    async def _fake_dedupe(alert_type, key, *, ttl_seconds):
        return True

    monkeypatch.setattr(alerts_mod, "dedupe_or_skip", _fake_dedupe)
    monkeypatch.setattr(
        alerts_mod, "stale_cancel_failure_spike",
        lambda **kw: (calls["failure"].append(kw), {"ok": True})[1],
    )
    monkeypatch.setattr(
        alerts_mod, "stale_cancel_volume_anomaly",
        lambda **kw: (calls["volume"].append(kw), {"ok": True})[1],
    )
    return calls


async def _seed_prior_observations(pool, vertical: str, failed_per_cycle: list[int]):
    """Insert N rows into `stale_cancel_observations` representing prior
    cycles, oldest first. Uses an explicit `observed_at` so the rolling
    window deterministically includes them regardless of test latency."""
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with pool.acquire() as c:
        for i, failed in enumerate(failed_per_cycle):
            await c.execute(
                """
                INSERT INTO stale_cancel_observations
                       (observed_at, vertical, cycle_id,
                        scanned, cancelled, failed, max_age)
                VALUES ($1, $2, $3, 0, 0, $4, 30)
                """,
                base + timedelta(seconds=i),
                vertical,
                # Distinct cycle_ids so the evaluator's `exclude_cycle_id`
                # filter (which excludes the *current* cycle) doesn't
                # accidentally drop these.
                10_000 + i,
                int(failed),
            )


def test_run_cycle_e2e_fires_failure_spike_after_two_prior_failures(
    pg_dsn, monkeypatch,
):
    """Seed two prior cycles whose `failed` count is at the per-cycle
    floor, then run a third cycle whose sweep also fails. The augmented
    strict-tail check sees [3, 3] over the failure_window of 2 and the
    failure-spike alert is dispatched with the augmented 24h counts.
    Also asserts the third cycle's row was actually persisted to
    `stale_cancel_observations` — proving the INSERT contract matches
    the migration's schema."""
    from app.orchestrator import cycle as cycle_mod_local
    from app.orchestrator import stale_cancel_baseline as sc_baseline

    # Reset the day-throttled GC mark for deterministic behavior.
    monkeypatch.setattr(sc_baseline, "_LAST_GC_TS", 0.0, raising=False)

    alert_calls = _capture_alert_dispatch(monkeypatch)
    _stub_cycle_settings_for_halt(monkeypatch)

    # Three stale resting orders + a venue that 503s on every cancel
    # → sweep produces failed=3, cancelled=0 for the current cycle.
    stales = [_make_record(f"flaky-{i}", age_secs=120) for i in range(3)]
    cancelled = _install_resting(
        monkeypatch, stales,
        cancel_side_effect=lambda oid: RuntimeError("HTTP 503 from venue"),
    )

    async def go():
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
        try:
            async with pool.acquire() as c:
                await c.execute(_STALE_CANCEL_SCHEMA_SQL)
                await c.execute(
                    "TRUNCATE stale_cancel_observations, trading_cycles "
                    "RESTART IDENTITY"
                )
            _wire_real_db(monkeypatch, pool)
            await _seed_prior_observations(pool, "sports", [3, 3])
            result = await cycle_mod_local.run_cycle("sports")

            async with pool.acquire() as c:
                rows = await c.fetch(
                    "SELECT cycle_id, scanned, cancelled, failed, max_age "
                    "FROM stale_cancel_observations "
                    "WHERE vertical = 'sports' "
                    "ORDER BY observed_at ASC"
                )
            return result, rows
        finally:
            await pool.close()

    result, rows = _run(go())

    # Sweep observed three failures.
    assert cancelled == []
    state = kalshi_lifecycle.stale_resting_cancelled_state()
    assert state["cancel_failed"] == 3

    # Two seeded + one persisted by record_observation in the cycle tail.
    assert len(rows) == 3
    last = rows[-1]
    assert last["cycle_id"] == result.cycle_id
    assert last["scanned"] == 3
    assert last["cancelled"] == 0
    assert last["failed"] == 3

    # The failure-spike helper fired exactly once with the augmented
    # 24h counts (baseline 2 cycles / 6 failures + this cycle 1 / 3).
    assert len(alert_calls["failure"]) == 1, alert_calls
    fired = alert_calls["failure"][0]
    assert fired["vertical"] == "sports"
    assert fired["cycle_id"] == result.cycle_id
    assert fired["failed_cycles"] == 3
    assert fired["failed_total"] == 9
    assert fired["failure_window"] == 2
    assert fired["floor"] == sc_baseline.DEFAULT_FAILURE_FLOOR
    # Volume side stayed quiet — current cancelled=0 is well below floor.
    assert alert_calls["volume"] == []


def test_run_cycle_e2e_no_alert_after_recovery(pg_dsn, monkeypatch):
    """Companion: same two prior failed cycles, but this cycle is clean
    (no stale orders to cancel). The strict-tail check sees [3, 0] —
    not all cycles meet the per-cycle floor, so the alert must NOT
    fire even though the 24h aggregate still looks bad. Proves the
    rewrite from "24h aggregate" to "strict tail" doesn't latch."""
    from app.orchestrator import cycle as cycle_mod_local
    from app.orchestrator import stale_cancel_baseline as sc_baseline

    monkeypatch.setattr(sc_baseline, "_LAST_GC_TS", 0.0, raising=False)

    alert_calls = _capture_alert_dispatch(monkeypatch)
    _stub_cycle_settings_for_halt(monkeypatch)

    # Clean sweep: no resting orders → failed=0 this cycle.
    _install_resting(monkeypatch, [])

    async def go():
        pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
        try:
            async with pool.acquire() as c:
                await c.execute(_STALE_CANCEL_SCHEMA_SQL)
                await c.execute(
                    "TRUNCATE stale_cancel_observations, trading_cycles "
                    "RESTART IDENTITY"
                )
            _wire_real_db(monkeypatch, pool)
            await _seed_prior_observations(pool, "sports", [3, 3])
            result = await cycle_mod_local.run_cycle("sports")

            async with pool.acquire() as c:
                rows = await c.fetch(
                    "SELECT cycle_id, scanned, cancelled, failed "
                    "FROM stale_cancel_observations "
                    "WHERE vertical = 'sports' "
                    "ORDER BY observed_at ASC"
                )
            return result, rows
        finally:
            await pool.close()

    result, rows = _run(go())

    # Two seeded + one for the recovery cycle (failed=0).
    assert len(rows) == 3
    assert rows[-1]["cycle_id"] == result.cycle_id
    assert rows[-1]["failed"] == 0

    assert alert_calls["failure"] == []
    assert alert_calls["volume"] == []
