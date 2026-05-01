"""Tests for the structured risk-gate-trip log (Task #756 / roadmap T-21).

`_emit_tail_risk_trip_alert` must always write a row into
`risk_gate_trips` so the daily honesty report's `gate_trips_24h`
panel reflects the *real* trip count, not the (smaller) deduped
notification count.  The alert dispatch may still be deduped — the
persistence step must run regardless.
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


def test_emit_tail_risk_trip_alert_persists_even_when_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persistence step runs *before* the dedupe check, so the
    honesty report's gate_trips_24h count includes every trip."""
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)

    # Force "already-claimed" so the alert dispatch is suppressed.
    async def _already_seen(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(_alerts, "dedupe_or_skip", _already_seen)

    dispatched: list[Any] = []
    monkeypatch.setattr(
        _alerts,
        "tail_risk_gate_trip",
        lambda **kw: dispatched.append(kw),
    )

    ctx = _make_ctx(market_id="KX-DUPE-MARKET")
    _run(
        _gates._emit_tail_risk_trip_alert(
            ctx=ctx,
            gate_name="tail_risk_size_clamped",
            reason="tail clamp",
            details={"foo": "bar"},
        )
    )

    # Persistence happened…
    assert any(
        "INSERT INTO risk_gate_trips" in q for q, _ in cap.calls
    ), cap.calls
    persisted_args = next(
        args for q, args in cap.calls if "INSERT INTO risk_gate_trips" in q
    )
    assert persisted_args[1] == "KX-DUPE-MARKET"
    # …but the broadcast did NOT (deduped).
    assert dispatched == []


def test_emit_tail_risk_trip_alert_persists_and_dispatches_on_first_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _CapturedExec()
    monkeypatch.setattr(_db, "execute", cap)

    async def _first_seen(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(_alerts, "dedupe_or_skip", _first_seen)

    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        _alerts,
        "tail_risk_gate_trip",
        lambda **kw: dispatched.append(kw),
    )

    ctx = _make_ctx(market_id="KX-FRESH-MARKET")
    _run(
        _gates._emit_tail_risk_trip_alert(
            ctx=ctx,
            gate_name="tail_risk_size_clamped",
            reason="tail clamp",
            details={"k": "v"},
        )
    )
    assert any(
        "INSERT INTO risk_gate_trips" in q for q, _ in cap.calls
    )
    assert dispatched and dispatched[0]["market_id"] == "KX-FRESH-MARKET"
