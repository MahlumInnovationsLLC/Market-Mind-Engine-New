"""Persistent dedupe tests for halt / drift / tier_change / live_trade.

These verify that a webhook re-delivery, settlement re-run, fill
re-broadcast, or process restart does NOT cause the same operator
email to fire twice within the configured TTL window.

The asyncpg pool is never opened — `app.notifications.alerts._ensure_dedupe_table`
is monkey-patched to a no-op and `app.db.fetchrow` is replaced with a
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

    async def _noop_ensure() -> None:
        return None

    monkeypatch.setattr(_alerts, "_ensure_dedupe_table", _noop_ensure)

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


def test_dedupe_fails_open_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("db gone")

    async def _noop_ensure() -> None:
        return None

    monkeypatch.setattr(_alerts, "_ensure_dedupe_table", _noop_ensure)
    monkeypatch.setattr(_db, "fetchrow", boom)

    async def go() -> None:
        ok = await _alerts.dedupe_or_skip("halt", "anything", ttl_seconds=60)
        assert ok is True

    asyncio.run(go())
