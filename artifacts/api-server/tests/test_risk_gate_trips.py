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


# ---------------------------------------------------------------------------
# Task #767 — gate_uncalibrated_vertical
# ---------------------------------------------------------------------------


class _StubSnapshot:
    def __init__(self) -> None:
        self.type = "isotonic"
        self.fit_at = "2026-05-01T12:00:00Z"
        self.sample_count = 250


class _StubCalState:
    def __init__(self, leagues_with_snapshot: set[str]) -> None:
        self._leagues = leagues_with_snapshot

    def champion(self, league: str):
        return _StubSnapshot() if league in self._leagues else None


def _patch_calibrators(
    monkeypatch: pytest.MonkeyPatch,
    *,
    supported: tuple[str, ...] = ("NBA", "WEATHER"),
    fitted: set[str] = frozenset({"NBA"}),
) -> None:
    from app import calibrators as _cal

    monkeypatch.setattr(_cal, "SUPPORTED_LEAGUES", supported, raising=True)
    monkeypatch.setattr(
        _cal, "get_state", lambda: _StubCalState(set(fitted)), raising=True
    )


def test_gate_uncalibrated_vertical_passes_when_calibrator_fitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_calibrators(monkeypatch)
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)
    ctx = _make_ctx(vertical="sports", league="NBA")
    res = _run(_gates.gate_uncalibrated_vertical(ctx))
    assert res.passed is True
    assert res.details["calibrator_loaded"] is True
    assert not cap.calls  # no risk_gate_trips persistence on the happy path


def test_gate_uncalibrated_vertical_blocks_when_calibrator_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_calibrators(monkeypatch, fitted=set())  # no fitted snapshot
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)
    ctx = _make_ctx(vertical="weather", league="WEATHER", operator_overrides={})
    res = _run(_gates.gate_uncalibrated_vertical(ctx))
    assert res.passed is False
    assert "no fitted calibrator snapshot" in res.reason
    assert res.details["calibrator_loaded"] is False
    # Block path does not persist a row — the run_all_gates wrapper does that.
    assert not cap.calls


def test_gate_uncalibrated_vertical_blocks_unsupported_league(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_calibrators(monkeypatch, supported=("NBA",), fitted={"NBA"})
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)
    ctx = _make_ctx(vertical="weather", league="WEATHER")
    res = _run(_gates.gate_uncalibrated_vertical(ctx))
    assert res.passed is False
    assert "calibrator missing for league" in res.reason
    assert res.details["supported"] is False


def test_gate_uncalibrated_vertical_raw_mode_passes_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_calibrators(monkeypatch, supported=("NBA",), fitted={"NBA"})
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)
    ctx = _make_ctx(
        vertical="weather",
        league="WEATHER",
        operator_overrides={"uncalibrated_raw_mode_weather": True},
    )
    res = _run(_gates.gate_uncalibrated_vertical(ctx))
    assert res.passed is True
    assert res.details["raw_mode"] is True
    # Exactly one INSERT into risk_gate_trips with the raw-mode marker.
    inserts = [
        args
        for q, args in cap.calls
        if "INSERT INTO risk_gate_trips" in q
    ]
    assert len(inserts) == 1
    assert inserts[0][0] == "uncalibrated_vertical_raw_mode"
    assert inserts[0][2] == "weather"  # vertical


def test_gate_uncalibrated_vertical_fails_closed_when_no_league(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed: a candidate without a league is rejected (no
    silent passthrough), since "we don't know the league" is exactly
    the silent-EV-leak shape this gate exists to close."""
    _patch_calibrators(monkeypatch)
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)
    ctx = _make_ctx(vertical="legacy", league=None)
    res = _run(_gates.gate_uncalibrated_vertical(ctx))
    assert res.passed is False
    assert "missing league" in res.reason
    assert res.details["state_error"] == "missing league on candidate"
    # Block path does not persist a row — run_all_gates wrapper does that.
    assert not cap.calls


def test_gate_uncalibrated_vertical_no_league_with_raw_mode_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator escape hatch wins even on a missing-league
    candidate — explicit opt-in, persisted as a raw-mode marker."""
    _patch_calibrators(monkeypatch)
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)
    ctx = _make_ctx(
        vertical="legacy",
        league=None,
        operator_overrides={"uncalibrated_raw_mode_legacy": True},
    )
    res = _run(_gates.gate_uncalibrated_vertical(ctx))
    assert res.passed is True
    assert res.details["raw_mode"] is True
    inserts = [
        args for q, args in cap.calls if "INSERT INTO risk_gate_trips" in q
    ]
    assert len(inserts) == 1
    assert inserts[0][0] == "uncalibrated_vertical_raw_mode"


def test_gates_registry_includes_uncalibrated_vertical() -> None:
    """Registry order is observable; #767 inserts immediately after
    `vertical_halted` so the cheap calibrator check runs before any
    DB-touching gate."""
    names = [n for n, _ in _gates.GATES]
    assert "uncalibrated_vertical" in names
    assert names.index("uncalibrated_vertical") == names.index("vertical_halted") + 1
    assert len(_gates.GATES) == 22
