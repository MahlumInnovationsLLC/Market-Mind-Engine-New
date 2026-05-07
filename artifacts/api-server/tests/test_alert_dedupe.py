"""Persistent dedupe tests for halt / drift / tier_change / live_trade.

These verify that a webhook re-delivery, settlement re-run, fill
re-broadcast, or process restart does NOT cause the same operator
email to fire twice within the configured TTL window.

The asyncpg pool is never opened — `app.db.fetchrow` is replaced with a
small in-memory implementation that mimics the
`INSERT ... ON CONFLICT ... WHERE expires_at <= now()` semantics.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from app import db as _db
from app.notifications import alerts as _alerts


# ---------------------------------------------------------------------------
# In-memory shim for `notification_dedupe`.
# ---------------------------------------------------------------------------


class _FakeDedupeStore:
    def __init__(self) -> None:
        # key -> expires_at (datetime)
        self.rows: dict[str, datetime] = {}
        self.dispatch_count: dict[str, int] = {}

    async def fake_fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        # We only model the dedupe INSERT...ON CONFLICT here.
        assert "notification_dedupe" in query, query
        key, ttl = args[0], int(args[1])
        now = datetime.now(timezone.utc)
        existing = self.rows.get(key)
        if existing is None or existing <= now:
            self.rows[key] = now + timedelta(seconds=ttl)
            return {"ok": 1}
        return None

    async def fake_execute(self, *_args: Any, **_kw: Any) -> str:
        return "OK"

    def record_dispatch(self, tag: str) -> None:
        self.dispatch_count[tag] = self.dispatch_count.get(tag, 0) + 1


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeDedupeStore:
    store = _FakeDedupeStore()
    monkeypatch.setattr(_db, "fetchrow", store.fake_fetchrow)
    monkeypatch.setattr(_db, "execute", store.fake_execute)

    # Capture every dispatch (instead of actually queueing email).
    def _fake_dispatch(subject: str, text_body: str, *, tag: str, dry_run: bool):
        store.record_dispatch(tag)
        return {"tag": tag, "dispatched": True}

    monkeypatch.setattr(_alerts, "_dispatch", _fake_dispatch)
    return store


# ---------------------------------------------------------------------------
# Generic dedupe primitive
# ---------------------------------------------------------------------------


def test_dedupe_or_skip_first_call_wins(fake_store: _FakeDedupeStore) -> None:
    async def go() -> None:
        ok1 = await _alerts.dedupe_or_skip("halt", "k1", ttl_seconds=60)
        ok2 = await _alerts.dedupe_or_skip("halt", "k1", ttl_seconds=60)
        ok3 = await _alerts.dedupe_or_skip("halt", "k2", ttl_seconds=60)
        assert ok1 is True
        assert ok2 is False
        assert ok3 is True

    asyncio.run(go())


def test_suppression_counts_track_per_alert_type(
    fake_store: _FakeDedupeStore,
) -> None:
    _alerts.reset_suppression_counts()

    async def go() -> None:
        # First claims: not suppressed, no counter bump.
        await _alerts.dedupe_or_skip("halt", "k1", ttl_seconds=60)
        await _alerts.dedupe_or_skip("drift", "vx:warning", ttl_seconds=60)
        # Replays: suppressed, counter bumps.
        await _alerts.dedupe_or_skip("halt", "k1", ttl_seconds=60)
        await _alerts.dedupe_or_skip("halt", "k1", ttl_seconds=60)
        await _alerts.dedupe_or_skip("drift", "vx:warning", ttl_seconds=60)

    asyncio.run(go())
    counts = _alerts.suppression_counts()
    assert counts == {"halt": 2, "drift": 1}
    # Snapshot is a copy — mutating it must not affect the live counter.
    counts["halt"] = 999
    assert _alerts.suppression_counts()["halt"] == 2
    _alerts.reset_suppression_counts()
    assert _alerts.suppression_counts() == {}


def test_dedupe_or_skip_expired_window_re_fires(fake_store: _FakeDedupeStore) -> None:
    async def go() -> None:
        assert await _alerts.dedupe_or_skip("drift", "vx:warning", ttl_seconds=60)
        # Force expiry by rewinding the stored row.
        full_key = "drift:vx:warning"
        fake_store.rows[full_key] = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert await _alerts.dedupe_or_skip("drift", "vx:warning", ttl_seconds=60)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Tier change — Stripe re-delivery
# ---------------------------------------------------------------------------


def test_tier_change_dedupe_blocks_webhook_replay(fake_store: _FakeDedupeStore) -> None:
    async def go() -> None:
        for _ in range(3):
            ok = await _alerts.dedupe_or_skip(
                "tier_change", "user_42:elite", ttl_seconds=24 * 3600
            )
            if ok:
                _alerts.tier_change(
                    user_email="x@y.com", user_id="user_42",
                    old_tier="pro", new_tier="elite",
                )
        # Real upgrade from elite -> founder should still fire.
        ok2 = await _alerts.dedupe_or_skip(
            "tier_change", "user_42:founder", ttl_seconds=24 * 3600
        )
        if ok2:
            _alerts.tier_change(
                user_email="x@y.com", user_id="user_42",
                old_tier="elite", new_tier="founder",
            )

    asyncio.run(go())
    assert fake_store.dispatch_count.get("tier-change") == 2


# ---------------------------------------------------------------------------
# Halt — process restart while halted
# ---------------------------------------------------------------------------


def test_halt_dedupe_survives_restart(fake_store: _FakeDedupeStore) -> None:
    async def go() -> None:
        # First trip on instance A.
        from app.exchange_kill_switch import _KillSwitch
        ks_a = _KillSwitch()
        ks_a.trip("data feed stale")
        # Allow the scheduled task to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Simulate process restart — fresh KillSwitch (no in-memory _halted)
        # tries to halt again with the same reason.  Persistent dedupe
        # must suppress.
        ks_b = _KillSwitch()
        ks_b.trip("data feed stale")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Different reason on a third boot — that *should* fire again.
        ks_c = _KillSwitch()
        ks_c.trip("operator panic")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert fake_store.dispatch_count.get("halt") == 2


# ---------------------------------------------------------------------------
# Drift — settlement re-runs of the same vertical
# ---------------------------------------------------------------------------


def test_drift_dedupe_settlement_replay(fake_store: _FakeDedupeStore) -> None:
    async def go() -> None:
        # Three settle-re-runs of the same vertical at the same severity
        # within the TTL window — only the first should email.
        for _ in range(3):
            if await _alerts.dedupe_or_skip(
                "drift", "sports:warning", ttl_seconds=3600
            ):
                _alerts.drift(
                    vertical="sports", metric="brier_score_24h",
                    value=0.27, threshold=0.25,
                )
        # Severity escalation -> different dedupe key -> should fire.
        if await _alerts.dedupe_or_skip(
            "drift", "sports:critical", ttl_seconds=3600
        ):
            _alerts.drift(
                vertical="sports", metric="brier_score_24h",
                value=0.33, threshold=0.30,
            )

    asyncio.run(go())
    assert fake_store.dispatch_count.get("drift") == 2


# ---------------------------------------------------------------------------
# Live trade — fill websocket re-delivery
# ---------------------------------------------------------------------------


def test_live_trade_dedupe_fill_replay(fake_store: _FakeDedupeStore) -> None:
    async def go() -> None:
        # Same (order_id, fill_id) re-broadcast 5 times -> 1 email.
        for _ in range(5):
            if await _alerts.dedupe_or_skip(
                "live_trade", "ord_1:fill_99", ttl_seconds=7 * 24 * 3600
            ):
                _alerts.live_trade(
                    market_ticker="X", side="yes", action="buy",
                    contracts=10, price_cents=42,
                    order_id="ord_1", fill_id="fill_99",
                )
        # Different fill on the same order -> separate email.
        if await _alerts.dedupe_or_skip(
            "live_trade", "ord_1:fill_100", ttl_seconds=7 * 24 * 3600
        ):
            _alerts.live_trade(
                market_ticker="X", side="yes", action="buy",
                contracts=5, price_cents=43,
                order_id="ord_1", fill_id="fill_100",
            )

    asyncio.run(go())
    assert fake_store.dispatch_count.get("live-trade") == 2


# ---------------------------------------------------------------------------
# Fail-open behaviour — DB failure must not silently drop alerts.
# ---------------------------------------------------------------------------


def test_live_trade_dedupe_fill_replay_no_fill_id(
    fake_store: _FakeDedupeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through `handle_ws_fill`: the WS bus re-broadcasts the
    same fill (no trade_id/fill_id present) and we must still email
    only once.  Regression for the bug where the fallback dedupe key
    used `rec.filled_count` (mutates as fills land) and so produced a
    fresh key on each replay.
    """
    from app.kalshi import lifecycle as _lc

    # Stub out side effects we don't want in this test.
    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(_lc, "_record", _noop)
    monkeypatch.setattr(_lc, "_emit", _noop)
    # `_record_fill_idempotent` calls real asyncpg via `db.fetchval`,
    # which the in-memory dedupe shim above does NOT intercept.  In
    # environments where a Postgres pool happens to be reachable AND
    # a previous run inserted the (order_id='ord_zzz', fill_id) row
    # into `order_fills`, the ON CONFLICT branch trips and the whole
    # fill path short-circuits before we reach the alert dispatch we
    # are trying to exercise.  Stub it out: this test is about the
    # alert-dedupe key derivation under no-fill-id replay, NOT the
    # fill-row uniqueness which has its own dedicated tests.
    async def _always_fresh(**_kw: Any) -> bool:
        return True
    monkeypatch.setattr(_lc, "_record_fill_idempotent", _always_fresh)
    # Same goes for the position rollup write.
    monkeypatch.setattr(_lc, "_upsert_position_rollup", _noop)

    req = _lc.OrderRequest(
        market_ticker="MKT", side="yes", action="buy",
        count=20, limit_price_cents=42, vertical="sports",
        client_order_id="coid_zzz",
    )
    rec = _lc.OrderRecord(
        order_id="ord_zzz",
        request=req,
        paper=False,
        state="resting",
        exchange_order_id="exch_1",
    )

    async def go() -> None:
        async with _lc._resting_lock:
            _lc._resting[req.client_order_id] = rec

        msg = {
            "msg": {
                "type": "fill",
                "msg": {
                    "client_order_id": req.client_order_id,
                    # NOTE: no trade_id / fill_id — exercises fallback.
                    "count": 5,
                    "yes_price": 42,
                    "created_time": "2026-04-21T12:00:00Z",
                },
            }
        }
        # Replay the same raw fill 4 times.
        for _ in range(4):
            # Re-insert into _resting since handle_ws_fill pops on full fill.
            async with _lc._resting_lock:
                _lc._resting[req.client_order_id] = rec
            rec.state = "resting"
            await _lc.handle_ws_fill(msg)

    asyncio.run(go())
    assert fake_store.dispatch_count.get("live-trade") == 1


def test_gc_expired_dedupe_drops_only_expired_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GC sweep must delete rows whose `expires_at` is older than the
    retention window, preserve in-window rows, and never touch
    daily-summary markers (`expires_at IS NULL`)."""

    rows: list[dict[str, Any]] = [
        # Stale TTL — should be deleted.
        {"key": "live_trade:ord_1:fill_1",
         "expires_at": datetime.now(timezone.utc) - timedelta(days=10)},
        # Just outside the window — should be deleted.
        {"key": "drift:sports:warning",
         "expires_at": datetime.now(timezone.utc) - timedelta(days=8)},
        # In-window expired (less than retention) — preserved.
        {"key": "halt:k1",
         "expires_at": datetime.now(timezone.utc) - timedelta(days=2)},
        # Still active — preserved.
        {"key": "halt:k2",
         "expires_at": datetime.now(timezone.utc) + timedelta(hours=1)},
        # Daily-summary marker (NULL expires_at) — must be preserved
        # regardless of age.
        {"key": "daily_pnl_summary", "expires_at": None},
    ]

    async def fake_fetchval(query: str, *args: Any) -> int:
        if "DELETE FROM notification_dedupe" in query:
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(args[0]))
            before = len(rows)
            rows[:] = [
                r for r in rows
                if r["expires_at"] is None or r["expires_at"] >= cutoff
            ]
            return before - len(rows)
        if "count(*)" in query:
            return len(rows)
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(_db, "fetchval", fake_fetchval)

    out = asyncio.run(_alerts.gc_expired_dedupe(older_than_days=7))
    assert out["deleted"] == 2
    assert out["remaining"] == 3
    remaining_keys = {r["key"] for r in rows}
    assert "daily_pnl_summary" in remaining_keys
    assert "halt:k1" in remaining_keys
    assert "halt:k2" in remaining_keys


def test_gc_expired_dedupe_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("db gone")

    monkeypatch.setattr(_db, "fetchval", boom)
    out = asyncio.run(_alerts.gc_expired_dedupe(older_than_days=7))
    assert out["deleted"] == 0
    assert out["remaining"] == 0
    assert "error" in out


def test_maybe_gc_dedupe_throttles_to_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cycle tail calls this every cycle but the DELETE must only
    actually fire once per ``DEDUPE_GC_INTERVAL_SECS`` (24h) per worker
    process — proves the throttle gate works so we don't hammer
    Postgres with redundant retention sweeps every ~120s."""
    monkeypatch.setattr(_alerts, "_LAST_DEDUPE_GC_TS", 0.0, raising=False)
    calls: list[int] = []

    async def _fake_gc(*, older_than_days: int) -> dict[str, Any]:
        calls.append(older_than_days)
        return {"deleted": 4, "remaining": 11}

    monkeypatch.setattr(_alerts, "gc_expired_dedupe", _fake_gc)
    # First call: throttle gate is open (last==0), GC actually runs.
    out = asyncio.run(_alerts.maybe_gc_dedupe(now=1_000_000.0))
    assert out == {"deleted": 4, "remaining": 11}
    assert calls == [7]
    # Second call 1h later: still inside the 24h window → short-circuit.
    out = asyncio.run(_alerts.maybe_gc_dedupe(now=1_000_000.0 + 3600))
    assert out == {"skipped": True}
    assert calls == [7]
    # Third call after the interval elapses → runs again.
    out = asyncio.run(_alerts.maybe_gc_dedupe(now=1_000_000.0 + 86401))
    assert out == {"deleted": 4, "remaining": 11}
    assert calls == [7, 7]


def test_maybe_gc_dedupe_honors_env_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AXIOM_NOTIFICATION_DEDUPE_RETENTION_DAYS` overrides the default
    7-day retention window."""
    monkeypatch.setattr(_alerts, "_LAST_DEDUPE_GC_TS", 0.0, raising=False)
    monkeypatch.setenv("AXIOM_NOTIFICATION_DEDUPE_RETENTION_DAYS", "30")
    seen: list[int] = []

    async def _fake_gc(*, older_than_days: int) -> dict[str, Any]:
        seen.append(older_than_days)
        return {"deleted": 0, "remaining": 0}

    monkeypatch.setattr(_alerts, "gc_expired_dedupe", _fake_gc)
    asyncio.run(_alerts.maybe_gc_dedupe(now=1_000_000.0))
    assert seen == [30]


def test_maybe_gc_dedupe_does_not_advance_stamp_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the underlying GC reports an error the throttle stamp must
    NOT advance, so the next cycle retries instead of waiting a full
    day."""
    monkeypatch.setattr(_alerts, "_LAST_DEDUPE_GC_TS", 0.0, raising=False)

    async def _fake_gc(*, older_than_days: int) -> dict[str, Any]:
        return {"deleted": 0, "remaining": 0, "error": "db gone"}

    monkeypatch.setattr(_alerts, "gc_expired_dedupe", _fake_gc)
    out = asyncio.run(_alerts.maybe_gc_dedupe(now=1_000_000.0))
    assert "error" in out
    assert _alerts._LAST_DEDUPE_GC_TS == 0.0
    # Next call still attempts the sweep rather than short-circuiting.
    calls: list[int] = []

    async def _fake_gc_ok(*, older_than_days: int) -> dict[str, Any]:
        calls.append(older_than_days)
        return {"deleted": 1, "remaining": 5}

    monkeypatch.setattr(_alerts, "gc_expired_dedupe", _fake_gc_ok)
    out = asyncio.run(_alerts.maybe_gc_dedupe(now=1_000_000.0 + 60))
    assert out == {"deleted": 1, "remaining": 5}
    assert calls == [7]


def test_dedupe_fails_open_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("db gone")

    monkeypatch.setattr(_db, "fetchrow", boom)

    async def go() -> None:
        ok = await _alerts.dedupe_or_skip("halt", "anything", ttl_seconds=60)
        assert ok is True

    asyncio.run(go())
