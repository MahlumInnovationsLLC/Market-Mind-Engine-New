"""Tests for the structured risk-gate-trip log (Task #756 / roadmap T-21).

Two layered guarantees:

1. The low-level :func:`_persist_risk_gate_trip` helper writes a single
   row into ``risk_gate_trips`` and never bubbles DB errors back up.
2. :func:`run_all_gates` writes a row for **every** failed gate (not
   just tail-risk ones), so the daily honesty report's
   ``gate_trips_24h`` panel becomes a single source of truth across
   all 19 gates (follow-up #763).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from app import db as _db
from app.notifications import alerts as _alerts
from app.risk import gates as _gates


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(**overrides: Any) -> Any:
    """Build a minimal RiskContext stand-in just for the persist call."""
    base = dict(
        market_id="KX-TEST-MARKET",
        side="YES",
        vertical="sports",
        league="NBA",
        market_type="team_event",
        matchup="LAL@BOS",
        current_price=0.05,
        model_probability=0.10,
        edge=0.05,
        confidence_band_low=0.02,
        confidence_band_high=0.20,
        agreement_rate=0.8,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=42,
        intended_quantity=100,
        intended_notional_usd=5.0,
        bankroll_usd=10000.0,
        open_positions_count=3,
        open_positions_notional_usd=300.0,
        open_positions_per_vertical_usd={"sports": 300.0},
        operator_overrides={},
        market_cooldown_until=None,
    )
    base.update(overrides)
    return _gates.RiskContext(**base)


# ---------------------------------------------------------------------------
# In-memory capture for db.execute
# ---------------------------------------------------------------------------


class _CapturedExec:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def __call__(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return "OK"


# ---------------------------------------------------------------------------
# T012 — `_persist_risk_gate_trip` writes a row
# ---------------------------------------------------------------------------


def test_persist_risk_gate_trip_writes_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)

    ctx = _make_ctx(market_id="KX-NBA-LAL-BOS", intended_quantity=250)
    _run(
        _gates._persist_risk_gate_trip(
            ctx=ctx,
            gate_name="tail_risk_size_clamped",
            reason="tail bucket exceeds max-loss cap",
            details={"cap_usd": 200.0, "current_loss_usd": 350.0},
        )
    )
    assert len(cap.calls) == 1
    query, args = cap.calls[0]
    assert "INSERT INTO risk_gate_trips" in query
    # gate_name, market_id, vertical, side, qty, price, reason, details_jsonb
    assert args[0] == "tail_risk_size_clamped"
    assert args[1] == "KX-NBA-LAL-BOS"
    assert args[2] == "sports"
    assert args[3] == "YES"
    assert args[4] == 250
    assert args[5] == pytest.approx(0.05)
    assert "tail bucket" in args[6]
    # details is JSON-encoded string
    import json

    decoded = json.loads(args[7])
    assert decoded == {"cap_usd": 200.0, "current_loss_usd": 350.0}


def test_persist_risk_gate_trip_swallows_db_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed INSERT must not bubble up — the gate decision is what
    matters; the row is best-effort."""

    async def _boom(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(_db, "execute", _boom)

    # Should not raise.
    _run(
        _gates._persist_risk_gate_trip(
            ctx=_make_ctx(),
            gate_name="tail_risk_size_clamped",
            reason="x",
            details={},
        )
    )


def test_emit_tail_risk_trip_alert_does_not_double_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_emit_tail_risk_trip_alert`` deliberately leaves persistence to
    :func:`run_all_gates` so the row is written exactly once per trip
    (follow-up #763 made the central writer authoritative)."""
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)

    async def _first_seen(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(_alerts, "dedupe_or_skip", _first_seen)
    monkeypatch.setattr(_alerts, "tail_risk_gate_trip", lambda **kw: None)

    _run(
        _gates._emit_tail_risk_trip_alert(
            ctx=_make_ctx(market_id="KX-NODOUBLE"),
            gate_name="tail_risk_size_clamped",
            reason="tail clamp",
            details={"k": "v"},
        )
    )

    # No INSERT from inside the alert helper.
    assert not any(
        "INSERT INTO risk_gate_trips" in q for q, _ in cap.calls
    )


# ---------------------------------------------------------------------------
# F763 — `run_all_gates` writes a row for every failed gate
# ---------------------------------------------------------------------------


def _run_one_failing_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate_name: str,
    reason: str,
    details: dict[str, Any],
) -> _CapturedExec:
    """Helper: monkey-patch GATES to a single failing gate, then run."""
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)

    async def _fail(_ctx: Any) -> Any:
        return _gates.GateResult(gate_name, False, reason, details)

    monkeypatch.setattr(_gates, "GATES", ((gate_name, _fail),))

    ctx = _make_ctx(market_id=f"KX-{gate_name.upper()}-MK")
    ok, results = _run(_gates.run_all_gates(ctx))
    assert ok is False
    assert len(results) == 1
    assert results[0].name == gate_name
    return cap


def _assert_persisted(cap: _CapturedExec, *, gate_name: str) -> tuple:
    insert_calls = [
        (q, args) for q, args in cap.calls if "INSERT INTO risk_gate_trips" in q
    ]
    assert len(insert_calls) == 1, (
        f"expected exactly one risk_gate_trips INSERT for {gate_name!r}, "
        f"got {len(insert_calls)}: {cap.calls}"
    )
    args = insert_calls[0][1]
    assert args[0] == gate_name
    return args


def test_run_all_gates_persists_kill_switch_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _run_one_failing_gate(
        monkeypatch,
        gate_name="kill_switch",
        reason="trading_mode=halt",
        details={"trading_mode": "halt"},
    )
    args = _assert_persisted(cap, gate_name="kill_switch")
    import json

    assert json.loads(args[7]) == {"trading_mode": "halt"}


def test_run_all_gates_persists_daily_loss_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _run_one_failing_gate(
        monkeypatch,
        gate_name="daily_loss",
        reason="daily loss cap exceeded",
        details={"loss_usd": 1234.5, "threshold_usd": 1000.0},
    )
    args = _assert_persisted(cap, gate_name="daily_loss")
    import json

    decoded = json.loads(args[7])
    assert decoded["loss_usd"] == 1234.5


def test_run_all_gates_persists_kelly_fraction_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _run_one_failing_gate(
        monkeypatch,
        gate_name="kelly_fraction",
        reason="kelly > cap",
        details={"kelly": 0.42, "cap": 0.10},
    )
    _assert_persisted(cap, gate_name="kelly_fraction")


def test_run_all_gates_persist_failure_does_not_break_gate_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB-side INSERT failure must not mask the FAIL decision."""

    async def _boom(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(_db, "execute", _boom)

    async def _fail(_ctx: Any) -> Any:
        return _gates.GateResult("kill_switch", False, "halted", {})

    monkeypatch.setattr(_gates, "GATES", (("kill_switch", _fail),))

    ok, results = _run(_gates.run_all_gates(_make_ctx()))
    assert ok is False
    assert results[0].name == "kill_switch" and results[0].passed is False


def test_run_all_gates_does_not_persist_on_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No row is written for gates that pass — the table is FAIL-only."""
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)

    async def _ok(_ctx: Any) -> Any:
        return _gates.GateResult("kill_switch", True, "", {})

    monkeypatch.setattr(_gates, "GATES", (("kill_switch", _ok),))

    ok, _results = _run(_gates.run_all_gates(_make_ctx()))
    assert ok is True
    assert not any(
        "INSERT INTO risk_gate_trips" in q for q, _ in cap.calls
    )
