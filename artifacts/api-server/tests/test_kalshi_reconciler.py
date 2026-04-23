"""Unit tests for ``app.kalshi.reconciler``: gating, position diff,
drift alerting, and missed-fill replay.

These tests stub Postgres + the Kalshi REST client so no external
services are touched. The reconciler module is small and deterministic
once those two dependencies are mocked.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.kalshi import reconciler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class _FakeClient:
    def __init__(
        self,
        *,
        positions: list[Any] | None = None,
        fills: list[dict[str, Any]] | None = None,
    ) -> None:
        self.positions = positions or []
        self.fills = fills or []
        self.fills_calls: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_positions(self, *, limit: int = 200):
        return list(self.positions)

    async def list_fills(self, *, min_ts: int | None = None, limit: int = 200,
                         ticker: str | None = None):
        self.fills_calls.append({"min_ts": min_ts, "limit": limit, "ticker": ticker})
        return list(self.fills)


def _pos(ticker: str, qty: int) -> SimpleNamespace:
    """Mimic the bits of ``KalshiClient.get_positions``'s PositionSummary
    that the reconciler reads.
    """
    return SimpleNamespace(market_ticker=ticker, position=qty)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Fresh module state before every test + safe defaults so the
    reconciler is enabled.
    """
    # Module-level counters/cursors.
    reconciler._task = None
    reconciler._last_tick_at = None
    reconciler._last_error = None
    reconciler._last_skipped_reason = None
    reconciler._last_drift_count = 0
    reconciler._last_auto_corrected = 0
    reconciler._last_missed_fills = 0
    reconciler._last_unknown_fills = 0
    reconciler._total_drift_alerts = 0
    reconciler._total_replayed_fills = 0
    reconciler._fills_cursor_ts = None
    # Re-bind the stop event to a fresh loop each test.
    reconciler._stop = asyncio.Event()
    # Default: live mode + creds present + kill switch off.
    monkeypatch.setattr(reconciler, "get_settings", lambda: SimpleNamespace(
        kalshi_api_key_id="kid", kalshi_private_key="pem", trading_mode="live",
    ))
    reconciler._kill._halted = False
    monkeypatch.delenv("AXIOM_RECONCILER_ENABLED", raising=False)
    monkeypatch.delenv("AXIOM_RECONCILER_DRIFT_TOLERANCE", raising=False)
    yield


def _stub_db(monkeypatch, *, local_positions=None, recent_alert=False,
             order_lookup=None, agg=None, requested=None,
             executes=None, alerts=None):
    """Wire ``app.db`` and the alerts module to in-memory captures."""
    from app import db as app_db

    local_positions = local_positions or []
    executes = executes if executes is not None else []
    alerts = alerts if alerts is not None else []

    async def _fetch(q, *a):
        if "FROM positions" in q:
            return [{"market_id": t, "side": s, "qty": q_} for t, s, q_ in local_positions]
        if "FROM order_fills" in q:
            # Replay path: arrival-ordered fills with count + price_cents.
            if agg is None:
                return []
            # Synthesize a single row that matches the previous `agg`
            # contract: total_count contracts at the average price.
            tc = int(agg.get("total_count") or 0)
            ws = int(agg.get("weighted_sum") or 0)
            if tc <= 0:
                return []
            return [{"count": tc, "price_cents": int(round(ws / tc))}]
        return []

    async def _fetchval(q, *a):
        if "model_health_alerts" in q:
            return 1 if recent_alert else None
        return None

    async def _fetchrow(q, *a):
        if "FROM order_lifecycle" in q:
            # The reconciler issues TWO order_lifecycle SELECTs: one to
            # find the order, then one for `max(contracts)`.
            if "max(coalesce(contracts" in q:
                return {"req": int(requested or 0)}
            return order_lookup
        if "FROM order_fills" in q:
            return agg or {"total_count": 0, "weighted_sum": 0}
        return None

    async def _execute(q, *a):
        executes.append((q, a))
        return None

    monkeypatch.setattr(app_db, "fetch", _fetch)
    monkeypatch.setattr(app_db, "fetchval", _fetchval)
    monkeypatch.setattr(app_db, "fetchrow", _fetchrow)
    monkeypatch.setattr(app_db, "execute", _execute)

    # Stub the alerts dispatch so dedupe_or_skip never hits the DB and
    # drift() never reaches for SMTP.
    import sys

    async def _dedupe_or_skip(*a, **kw):
        return True

    def _drift(**kwargs):
        alerts.append(kwargs)
        return {"sent": True}

    fake_alerts = SimpleNamespace(
        dedupe_or_skip=_dedupe_or_skip,
        drift=_drift,
    )
    monkeypatch.setitem(sys.modules, "app.notifications.alerts", fake_alerts)
    # ``from app.notifications import alerts`` resolves the attribute on
    # the parent package first when one is already bound (any earlier
    # test in the suite that imported the real module triggers this),
    # so patch the attribute too — sys.modules alone isn't enough.
    import app.notifications as _notif_pkg
    monkeypatch.setattr(_notif_pkg, "alerts", fake_alerts, raising=False)
    return executes, alerts


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_disabled_when_paper_mode(monkeypatch):
    monkeypatch.setattr(reconciler, "get_settings", lambda: SimpleNamespace(
        kalshi_api_key_id="kid", kalshi_private_key="pem", trading_mode="paper",
    ))
    summary = _run(reconciler._tick_once(client_factory=lambda: _FakeClient()))
    assert summary["skipped"] is True
    assert "paper" in (summary.get("reason") or "")


def test_disabled_when_creds_missing(monkeypatch):
    monkeypatch.setattr(reconciler, "get_settings", lambda: SimpleNamespace(
        kalshi_api_key_id=None, kalshi_private_key=None, trading_mode="live",
    ))
    summary = _run(reconciler._tick_once(client_factory=lambda: _FakeClient()))
    assert summary["skipped"] is True


def test_disabled_when_env_killed(monkeypatch):
    monkeypatch.setenv("AXIOM_RECONCILER_ENABLED", "0")
    summary = _run(reconciler._tick_once(client_factory=lambda: _FakeClient()))
    assert summary["skipped"] is True


def test_skipped_when_kill_switch_active(monkeypatch):
    _stub_db(monkeypatch)
    reconciler._kill._halted = True
    summary = _run(reconciler._tick_once(client_factory=lambda: _FakeClient()))
    assert summary["skipped"] is True
    assert "kill" in (summary.get("reason") or "")


# ---------------------------------------------------------------------------
# Position diff
# ---------------------------------------------------------------------------


def test_clean_run_no_drift(monkeypatch):
    executes, alerts = _stub_db(
        monkeypatch,
        local_positions=[("MKT-A", "yes", 10), ("MKT-B", "no", 5)],
    )
    client = _FakeClient(positions=[_pos("MKT-A", 10), _pos("MKT-B", -5)])
    summary = _run(reconciler._tick_once(client_factory=lambda: client))
    assert summary["skipped"] is False
    assert summary["drift_count"] == 0
    assert summary["auto_corrected"] == 0
    assert alerts == []
    # No model_health_alerts INSERT should have run.
    assert not any("INSERT INTO model_health_alerts" in q for q, _ in executes)


def test_drift_within_tolerance_logs_only(monkeypatch):
    monkeypatch.setenv("AXIOM_RECONCILER_DRIFT_TOLERANCE", "2")
    executes, alerts = _stub_db(
        monkeypatch,
        local_positions=[("MKT-A", "yes", 10)],
    )
    # Exchange says 11 contracts; diff=1 <= tolerance=2 -> auto-corrected.
    client = _FakeClient(positions=[_pos("MKT-A", 11)])
    summary = _run(reconciler._tick_once(client_factory=lambda: client))
    assert summary["drift_count"] == 0
    assert summary["auto_corrected"] == 1
    # No operator email for within-tolerance drift.
    assert alerts == []
    # But an INFO-severity, pre-acknowledged audit row IS written so
    # small drifts are reviewable after the fact.
    audit = [q for q, _ in executes if "INSERT INTO model_health_alerts" in q]
    assert len(audit) == 1
    assert "'info'" in audit[0]
    assert "true" in audit[0]  # acknowledged=true


def test_drift_exceeds_tolerance_raises_alert(monkeypatch):
    executes, alerts = _stub_db(
        monkeypatch,
        local_positions=[("MKT-A", "yes", 10)],
    )
    # Exchange reports 5 — diff=5 with default tolerance 0 fires.
    client = _FakeClient(positions=[_pos("MKT-A", 5)])
    summary = _run(reconciler._tick_once(client_factory=lambda: client))
    assert summary["drift_count"] == 1
    inserts = [q for q, _ in executes if "INSERT INTO model_health_alerts" in q]
    assert len(inserts) == 1
    assert len(alerts) == 1
    assert alerts[0]["vertical"] == "trading"
    assert "MKT-A" in (alerts[0].get("metric") or "")
    assert reconciler.state()["total_drift_alerts"] == 1


def test_drift_alert_suppressed_when_recent(monkeypatch):
    """An existing same-hour row in `model_health_alerts` blocks a duplicate."""
    executes, alerts = _stub_db(
        monkeypatch,
        local_positions=[("MKT-A", "yes", 10)],
        recent_alert=True,
    )
    client = _FakeClient(positions=[_pos("MKT-A", 5)])
    summary = _run(reconciler._tick_once(client_factory=lambda: client))
    # Drift was detected ...
    assert summary["drift_count"] == 1
    # ... but the row insert was suppressed by the recent-alert guard.
    inserts = [q for q, _ in executes if "INSERT INTO model_health_alerts" in q]
    assert inserts == []
    assert alerts == []


def test_local_only_position_treated_as_drift(monkeypatch):
    """We hold a position the exchange says is closed -> drift alert."""
    executes, alerts = _stub_db(
        monkeypatch,
        local_positions=[("MKT-STALE", "yes", 7)],
    )
    client = _FakeClient(positions=[])  # exchange shows nothing
    summary = _run(reconciler._tick_once(client_factory=lambda: client))
    assert summary["drift_count"] == 1
    assert any("INSERT INTO model_health_alerts" in q for q, _ in executes)


# ---------------------------------------------------------------------------
# Missed-fill replay
# ---------------------------------------------------------------------------


def test_missed_fill_replayed_into_order_fills(monkeypatch):
    """A fill the WS missed is back-filled: insert into order_fills,
    upsert position rollup, and emit a `reconciler_replay` audit row.
    """
    from app.kalshi import lifecycle as life

    executes, alerts = _stub_db(
        monkeypatch,
        local_positions=[],
        order_lookup={
            "order_id": "coid-1", "market_id": "MKT-A",
            "side": "yes", "vertical": "sports",
        },
        # Recompute matches the single replayed fill (3 @ 50¢).
        agg={"total_count": 3, "weighted_sum": 150},
        requested=10,  # plenty of headroom -> not over-filled
    )

    record_calls: list[dict[str, Any]] = []
    rollup_calls: list[dict[str, Any]] = []

    async def _record_fill(**kw):
        record_calls.append(kw)
        return True  # fresh insert

    async def _upsert(**kw):
        rollup_calls.append(kw)

    monkeypatch.setattr(life, "_record_fill_idempotent", _record_fill)
    monkeypatch.setattr(life, "_upsert_position_rollup", _upsert)

    fill = {
        "order_id": "exo-1", "trade_id": "fill-99",
        "side": "yes", "yes_price": 50, "count": 3,
        "ticker": "MKT-A", "created_time": 1234567890,
    }
    client = _FakeClient(positions=[], fills=[fill])
    summary = _run(reconciler._tick_once(client_factory=lambda: client))

    assert summary["replayed_fills"] == 1
    assert summary["unknown_fills"] == 0
    assert summary["duplicate_fills"] == 0
    # Record fill called with our derived order_id + fill_id.
    assert record_calls[0]["order_id"] == "coid-1"
    assert record_calls[0]["fill_id"] == "fill-99"
    assert record_calls[0]["count"] == 3
    assert record_calls[0]["price_cents"] == 50
    # Rollup carried the recomputed cumulative numbers.
    assert rollup_calls[0]["quantity"] == 3
    assert rollup_calls[0]["avg_price_cents"] == 50
    # Audit row inserted into order_lifecycle, tagged as reconciler_replay.
    audit = [q for q, _ in executes if "INSERT INTO order_lifecycle" in q]
    assert len(audit) == 1
    # Cursor advanced.
    assert reconciler.state()["fills_cursor_ts"] == 1234567890


def test_duplicate_fill_does_not_double_count(monkeypatch):
    from app.kalshi import lifecycle as life

    executes, _ = _stub_db(
        monkeypatch,
        order_lookup={"order_id": "coid-1", "market_id": "MKT-A",
                      "side": "yes", "vertical": "sports"},
        requested=10,
    )

    async def _record_fill(**kw):
        return False  # already in order_fills

    rollup_calls: list[dict[str, Any]] = []

    async def _upsert(**kw):
        rollup_calls.append(kw)

    monkeypatch.setattr(life, "_record_fill_idempotent", _record_fill)
    monkeypatch.setattr(life, "_upsert_position_rollup", _upsert)

    fill = {"order_id": "exo-1", "trade_id": "fill-99",
            "side": "yes", "yes_price": 50, "count": 3, "ticker": "MKT-A"}
    client = _FakeClient(fills=[fill])
    summary = _run(reconciler._tick_once(client_factory=lambda: client))

    assert summary["duplicate_fills"] == 1
    assert summary["replayed_fills"] == 0
    # Position rollup must NOT fire when the fill was already known.
    assert rollup_calls == []
    # No audit row either.
    assert not any("INSERT INTO order_lifecycle" in q for q, _ in executes)


def test_replay_overfill_uses_arrival_order_not_proportional(monkeypatch):
    """When mixed-price fills exceed requested size, the rollup must
    discard the OVERFLOW from the latest-arriving fill (matching the
    WS path), not proportionally scale across fills — otherwise the
    avg_price drifts from what the live path produces.

    Scenario: requested=5. Order_fills contains (in arrival order):
      1) count=3 @ 50¢  -> all 3 accepted
      2) count=5 @ 60¢  -> only 2 accepted (cap = 5 - 3); 3 discarded
    Expected avg = (3*50 + 2*60) / 5 = 270 / 5 = 54¢.
    Proportional scaling would have given (3*50+5*60)/8 ≈ 56¢ — wrong.
    """
    from app.kalshi import lifecycle as life
    from app import db as app_db

    async def _fetch(q, *a):
        if "FROM order_fills" in q:
            return [
                {"count": 3, "price_cents": 50},
                {"count": 5, "price_cents": 60},
            ]
        return []

    async def _fetchval(q, *a):
        return None

    async def _fetchrow(q, *a):
        if "FROM order_lifecycle" in q:
            if "max(coalesce(contracts" in q:
                return {"req": 5}
            return {"order_id": "coid-1", "market_id": "MKT-A",
                    "side": "yes", "vertical": "sports"}
        return None

    async def _execute(q, *a):
        return None

    monkeypatch.setattr(app_db, "fetch", _fetch)
    monkeypatch.setattr(app_db, "fetchval", _fetchval)
    monkeypatch.setattr(app_db, "fetchrow", _fetchrow)
    monkeypatch.setattr(app_db, "execute", _execute)

    rollup_calls: list[dict[str, Any]] = []

    async def _record_fill(**kw):
        return True

    async def _upsert(**kw):
        rollup_calls.append(kw)

    monkeypatch.setattr(life, "_record_fill_idempotent", _record_fill)
    monkeypatch.setattr(life, "_upsert_position_rollup", _upsert)

    fill = {"order_id": "exo-1", "trade_id": "fill-99",
            "side": "yes", "yes_price": 60, "count": 5, "ticker": "MKT-A"}
    client = _FakeClient(fills=[fill])
    _run(reconciler._tick_once(client_factory=lambda: client))

    assert rollup_calls[0]["quantity"] == 5
    assert rollup_calls[0]["over_filled"] is True
    # Arrival-ordered semantics: 54¢, not ~56¢.
    assert rollup_calls[0]["avg_price_cents"] == 54


def test_replay_caps_overfill_at_requested_size(monkeypatch):
    """A missed fill that pushes total fills past the requested order
    size must be capped (and `over_filled=True`) — mirroring the live
    `handle_ws_fill` behaviour. Otherwise a dropped overfill would
    silently inflate `positions.quantity` past what the user asked for.
    """
    from app.kalshi import lifecycle as life

    _stub_db(
        monkeypatch,
        order_lookup={"order_id": "coid-1", "market_id": "MKT-A",
                      "side": "yes", "vertical": "sports"},
        # Order requested 5 contracts; durable record now totals 8 (3
        # accepted previously + 5 in this missed fill, all @ 50¢).
        agg={"total_count": 8, "weighted_sum": 400},
        requested=5,
    )

    async def _record_fill(**kw):
        return True

    rollup_calls: list[dict[str, Any]] = []

    async def _upsert(**kw):
        rollup_calls.append(kw)

    monkeypatch.setattr(life, "_record_fill_idempotent", _record_fill)
    monkeypatch.setattr(life, "_upsert_position_rollup", _upsert)

    fill = {"order_id": "exo-1", "trade_id": "fill-99",
            "side": "yes", "yes_price": 50, "count": 5, "ticker": "MKT-A"}
    client = _FakeClient(fills=[fill])
    summary = _run(reconciler._tick_once(client_factory=lambda: client))

    assert summary["replayed_fills"] == 1
    assert rollup_calls[0]["quantity"] == 5  # capped, not 8
    assert rollup_calls[0]["over_filled"] is True
    assert rollup_calls[0]["avg_price_cents"] == 50


def test_replay_runs_before_diff_so_no_false_drift_alert(monkeypatch):
    """When the WS missed a fill, the same tick that replays it must
    NOT first fire a drift alert based on the stale local state.

    We assert the ordering by recording the sequence of operations the
    tick performs against the DB stub: the order_fills SELECT (driven
    by `_replay_fill`) must precede the `FROM positions` SELECT
    (driven by `_diff_positions`).
    """
    from app.kalshi import lifecycle as life
    from app import db as app_db

    call_order: list[str] = []

    async def _fetch(q, *a):
        if "FROM positions" in q:
            call_order.append("diff_positions_query")
            return []
        return []

    async def _fetchval(q, *a):
        return None

    async def _fetchrow(q, *a):
        if "FROM order_lifecycle" in q:
            if "max(coalesce(contracts" in q:
                return {"req": 10}
            call_order.append("lookup_order")
            return {"order_id": "coid-1", "market_id": "MKT-A",
                    "side": "yes", "vertical": "sports"}
        if "FROM order_fills" in q:
            call_order.append("rollup_recompute")
            return {"total_count": 8, "weighted_sum": 400}
        return None

    async def _execute(q, *a):
        return None

    monkeypatch.setattr(app_db, "fetch", _fetch)
    monkeypatch.setattr(app_db, "fetchval", _fetchval)
    monkeypatch.setattr(app_db, "fetchrow", _fetchrow)
    monkeypatch.setattr(app_db, "execute", _execute)

    async def _record_fill(**kw):
        call_order.append("record_fill")
        return True

    async def _upsert(**kw):
        call_order.append("upsert_rollup")

    monkeypatch.setattr(life, "_record_fill_idempotent", _record_fill)
    monkeypatch.setattr(life, "_upsert_position_rollup", _upsert)

    fill = {"order_id": "exo-1", "trade_id": "fill-99",
            "side": "yes", "yes_price": 50, "count": 3, "ticker": "MKT-A"}
    client = _FakeClient(positions=[_pos("MKT-A", 8)], fills=[fill])
    summary = _run(reconciler._tick_once(client_factory=lambda: client))

    assert summary["replayed_fills"] == 1
    # Critical invariant: the diff query runs strictly AFTER the
    # replay's record/upsert calls.
    assert "diff_positions_query" in call_order
    assert "upsert_rollup" in call_order
    assert call_order.index("upsert_rollup") < call_order.index(
        "diff_positions_query"
    ), f"replay must precede diff; got order={call_order}"


def test_unknown_order_fill_is_counted_separately(monkeypatch):
    """A fill whose exchange_order_id we never recorded locally counts
    as `unknown` (not `replayed`) and does not corrupt local state.
    """
    from app.kalshi import lifecycle as life

    _stub_db(monkeypatch, order_lookup=None)

    async def _record_fill(**kw):
        raise AssertionError("must not insert order_fills for unknown order")

    monkeypatch.setattr(life, "_record_fill_idempotent", _record_fill)

    fill = {"order_id": "exo-ghost", "trade_id": "f1",
            "side": "yes", "yes_price": 60, "count": 2, "ticker": "MKT-X"}
    client = _FakeClient(fills=[fill])
    summary = _run(reconciler._tick_once(client_factory=lambda: client))

    assert summary["unknown_fills"] == 1
    assert summary["replayed_fills"] == 0
    assert reconciler.state()["last_unknown_fills"] == 1


def test_state_includes_running_and_cursor(monkeypatch):
    s = reconciler.state()
    assert s["running"] is False
    assert s["fills_cursor_ts"] is None
    assert s["interval_seconds"] >= 10.0
    assert s["drift_tolerance"] >= 0


def test_tick_records_error_on_client_failure(monkeypatch):
    _stub_db(monkeypatch)

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("kalshi 502")

        async def __aexit__(self, *exc):
            return None

    summary = _run(reconciler._tick_once(client_factory=lambda: _Boom()))
    assert summary["error"] is not None
    assert "kalshi 502" in summary["error"]
    assert reconciler.state()["last_error"] is not None
