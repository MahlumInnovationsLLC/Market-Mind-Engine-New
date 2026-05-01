"""Tests for the tail-risk gate auto-arm + trip-alert plumbing (Task #750).

Two surfaces:

1. ``runtime_settings.ensure_tail_risk_defaults_armed`` — idempotently
   persists the four tail-risk knob defaults into ``system_settings``.
   Existing operator overrides must NEVER be overwritten.

2. ``gate_tail_price_size_cap`` and ``gate_jumbo_quantity_cap`` — when
   they veto a candidate they fire an info-severity
   ``tail_risk_gate_trip`` alert (deduped on
   ``(gate_name, market_id)``).  Alert failures must never break the
   gate verdict.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import runtime_settings as runtime_settings_mod
from app.notifications import alerts as alerts_mod
from app.risk import gates as gates_mod


# ---------------------------------------------------------------------------
# Shared in-memory DB stub — reads/writes the rows the auto-arm function
# touches: `system_settings` (SELECT by key, INSERT, DELETE) and
# `admin_audit_log` (INSERT).
# ---------------------------------------------------------------------------


class _MemoryDb:
    def __init__(self) -> None:
        self.system_settings: dict[str, str] = {}
        self.audit: list[dict[str, Any]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "FROM system_settings" in sql and "WHERE key = $1" in sql and args:
            return self.system_settings.get(args[0])
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM system_settings" in sql:
            return [
                {"key": k, "value": v} for k, v in self.system_settings.items()
            ]
        return []

    class _Conn:
        def __init__(self, parent: "_MemoryDb") -> None:
            self.parent = parent

        async def fetchval(self, sql: str, *args: Any) -> Any:
            if "INSERT INTO system_settings" in sql and "RETURNING key" in sql:
                key, value = args[0], args[1]
                # `ON CONFLICT (key) DO NOTHING RETURNING key` — return
                # the key only when the insert actually happened.
                if key in self.parent.system_settings:
                    return None
                self.parent.system_settings[key] = value
                return key
            return None

        async def execute(self, sql: str, *args: Any) -> str:
            if "INSERT INTO system_settings" in sql:
                key, value = args[0], args[1]
                if "DO NOTHING" in sql:
                    self.parent.system_settings.setdefault(key, value)
                else:
                    self.parent.system_settings[key] = value
            elif "DELETE FROM system_settings" in sql:
                self.parent.system_settings.pop(args[0], None)
            elif "INSERT INTO admin_audit_log" in sql:
                self.parent.audit.append(
                    {
                        "actor_user_id": args[0],
                        "action": args[4],
                        "before_value": args[5],
                        "after_value": args[6],
                    }
                )
            return "OK"

    def acquire(self):
        parent = self

        class _Ctx:
            async def __aenter__(self_inner):
                return _MemoryDb._Conn(parent)

            async def __aexit__(self_inner, *exc):
                return None

        return _Ctx()


@pytest.fixture
def mem_db(monkeypatch):
    db = _MemoryDb()
    monkeypatch.setattr(runtime_settings_mod, "db", db, raising=True)
    return db


# ---------------------------------------------------------------------------
# ensure_tail_risk_defaults_armed
# ---------------------------------------------------------------------------


def test_auto_arm_writes_all_four_defaults_when_db_empty(mem_db):
    result = asyncio.run(runtime_settings_mod.ensure_tail_risk_defaults_armed())

    armed_keys = {row["key"] for row in result["armed"]}
    assert armed_keys == set(runtime_settings_mod.TAIL_RISK_DEFAULT_KNOBS)
    assert result["preserved"] == []

    # Each row landed in `system_settings` with the audit default.
    for key, default in runtime_settings_mod.TAIL_RISK_DEFAULT_KNOBS.items():
        assert mem_db.system_settings[f"risk.{key}"] == str(default)


def test_auto_arm_writes_audit_log_row_when_anything_was_armed(mem_db):
    asyncio.run(runtime_settings_mod.ensure_tail_risk_defaults_armed())

    actions = [r["action"] for r in mem_db.audit]
    assert actions == ["auto_arm_tail_risk_defaults"]
    assert mem_db.audit[0]["actor_user_id"] == "system"


def test_auto_arm_is_idempotent_on_rerun(mem_db):
    # First run arms everything.
    first = asyncio.run(runtime_settings_mod.ensure_tail_risk_defaults_armed())
    assert len(first["armed"]) == len(runtime_settings_mod.TAIL_RISK_DEFAULT_KNOBS)

    # Second run sees all rows and writes nothing.
    second = asyncio.run(runtime_settings_mod.ensure_tail_risk_defaults_armed())
    assert second["armed"] == []
    assert {row["key"] for row in second["preserved"]} == set(
        runtime_settings_mod.TAIL_RISK_DEFAULT_KNOBS
    )

    # Audit log only gained one row (from the first call, not the second).
    assert len(mem_db.audit) == 1


def test_auto_arm_preserves_existing_operator_override(mem_db):
    """Operator's custom value must NOT be overwritten — even if it
    differs from the audit default (including 0, which disables the
    gate)."""
    mem_db.system_settings["risk.tail_price_threshold"] = "0.05"  # tighter
    mem_db.system_settings["risk.jumbo_quantity_cap"] = "0"      # disabled

    result = asyncio.run(runtime_settings_mod.ensure_tail_risk_defaults_armed())

    # Preserved rows kept their operator-set values.
    assert mem_db.system_settings["risk.tail_price_threshold"] == "0.05"
    assert mem_db.system_settings["risk.jumbo_quantity_cap"] == "0"

    # The other two were missing → got armed.
    armed_keys = {row["key"] for row in result["armed"]}
    assert armed_keys == {"max_tail_loss_usd", "max_tail_notional_usd"}
    preserved_keys = {row["key"] for row in result["preserved"]}
    assert preserved_keys == {"tail_price_threshold", "jumbo_quantity_cap"}


def test_auto_arm_default_values_match_in_code_gate_fallbacks():
    """Drift guard — if the gate fallback constants change but the
    auto-arm constants don't (or vice versa) the production behaviour
    diverges from what the dashboard claims is armed."""
    # These four values are the ones hard-coded in
    # `gate_tail_price_size_cap` / `gate_jumbo_quantity_cap` as
    # `_resolve_per_vertical_override(..., fallback)`.  Update both
    # sides in the same commit if the audit ever revises them.
    assert runtime_settings_mod.TAIL_RISK_DEFAULT_KNOBS == {
        "tail_price_threshold":  0.10,
        "max_tail_loss_usd":     400.0,
        "max_tail_notional_usd": 400.0,
        "jumbo_quantity_cap":    750,
    }


def test_auto_arm_loser_does_not_claim_armed_in_replica_race(monkeypatch):
    """Multi-replica race: if two pods boot in parallel, both see the
    rows missing in the SELECT, but only one wins the INSERT.  The
    loser must NOT report keys as `armed` (and must NOT write a
    misleading audit log entry) — the loser's report must show the
    keys as `preserved`."""
    db = _MemoryDb()
    monkeypatch.setattr(runtime_settings_mod, "db", db, raising=True)

    # Stub `fetchval` so the *initial* `SELECT value FROM system_settings`
    # always sees no row (simulating the race window where both replicas
    # have read but neither has written).  The conn-level fetchval still
    # races on `ON CONFLICT DO NOTHING RETURNING key`, so the loser
    # gets None back.
    real_fetchval = db.fetchval
    select_calls = {"n": 0}

    async def _stubbed_fetchval(sql: str, *args: Any) -> Any:
        if "SELECT value FROM system_settings" in sql and "WHERE key = $1" in sql:
            select_calls["n"] += 1
            # First call per knob (the initial existence check) sees
            # nothing; second call (the race-loser re-read after a
            # conflict) sees whatever the winner wrote.
            if args[0] not in db.system_settings or select_calls["n"] <= len(
                runtime_settings_mod.TAIL_RISK_DEFAULT_KNOBS
            ):
                return None
            return db.system_settings.get(args[0])
        return await real_fetchval(sql, *args)

    monkeypatch.setattr(db, "fetchval", _stubbed_fetchval, raising=False)

    # Pre-populate the rows (simulating "winner already wrote them")
    # so every conn-level RETURNING key call sees ON CONFLICT and
    # returns None.
    for k, v in runtime_settings_mod.TAIL_RISK_DEFAULT_KNOBS.items():
        db.system_settings[f"risk.{k}"] = str(v)

    result = asyncio.run(runtime_settings_mod.ensure_tail_risk_defaults_armed())

    # Loser correctly reports nothing armed.
    assert result["armed"] == []
    # Every key surfaces as preserved.
    assert {row["key"] for row in result["preserved"]} == set(
        runtime_settings_mod.TAIL_RISK_DEFAULT_KNOBS
    )
    # No audit log entry was written (because nothing was armed).
    assert db.audit == []


def test_auto_arm_swallows_db_read_failures(monkeypatch):
    """A flaky `system_settings` SELECT must not crash startup."""
    class _BrokenDb:
        async def fetchval(self, *a, **k):
            raise RuntimeError("simulated DB outage")

        def acquire(self):
            raise AssertionError("must not be called when read fails")

    monkeypatch.setattr(runtime_settings_mod, "db", _BrokenDb(), raising=True)
    result = asyncio.run(runtime_settings_mod.ensure_tail_risk_defaults_armed())
    # Best-effort: nothing armed, nothing preserved, no exception.
    assert result == {"armed": [], "preserved": []}


# ---------------------------------------------------------------------------
# Tail-risk gate trip alert dispatch
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


def _make_ctx(
    *,
    side: str = "YES",
    vertical: str = "sports",
    current_price: float | None = 0.50,
    intended_quantity: int = 50,
    intended_notional_usd: float | None = None,
    market_id: str = "TEST-T1",
    overrides: dict | None = None,
) -> gates_mod.RiskContext:
    if intended_notional_usd is None:
        intended_notional_usd = intended_quantity * float(current_price or 0.0)
    return gates_mod.RiskContext(
        market_id=market_id,
        side=side,
        vertical=vertical,
        league="NFL",
        market_type="moneyline",
        matchup="A vs B",
        current_price=current_price,
        model_probability=0.60,
        edge=0.10,
        confidence_band_low=0.55,
        confidence_band_high=0.65,
        agreement_rate=0.7,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES" if side == "YES" else "BUY_NO",
        debate_id=42,
        intended_quantity=intended_quantity,
        intended_notional_usd=intended_notional_usd,
        bankroll_usd=1_000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides or {},
        market_cooldown_until=None,
    )


@pytest.fixture
def captured_alerts(monkeypatch):
    """Capture every `tail_risk_gate_trip` invocation and stub dedupe to
    always claim the key (so each call dispatches)."""
    calls: list[dict[str, Any]] = []

    def _capture(**kwargs):
        calls.append(kwargs)
        return {"queued": True, "tag": "tail-risk-gate-trip"}

    async def _always_claim(*a, **k):
        return True

    monkeypatch.setattr(alerts_mod, "tail_risk_gate_trip", _capture, raising=True)
    monkeypatch.setattr(alerts_mod, "dedupe_or_skip", _always_claim, raising=True)
    return calls


def test_tail_price_gate_fires_alert_on_max_loss_veto(captured_alerts):
    # Feb-20 NBA replay: YES_price 0.04, side=NO → entry 0.96, qty 4394
    # → max_loss = 0.96 × 4394 = $4218.24, > $400 cap.
    ctx = _make_ctx(
        side="NO",
        current_price=0.04,
        intended_quantity=4_394,
        intended_notional_usd=4_394 * 0.04,
        market_id="KXNBABOSWAR-25FEB20",
    )
    res = asyncio.get_event_loop().run_until_complete(
        gates_mod.gate_tail_price_size_cap(ctx)
    )
    assert res.passed is False
    assert len(captured_alerts) == 1
    payload = captured_alerts[0]
    assert payload["gate_name"] == "tail_price_size_cap"
    assert payload["market_id"] == "KXNBABOSWAR-25FEB20"
    assert payload["vertical"] == "sports"
    assert payload["side"] == "NO"
    assert payload["intended_quantity"] == 4_394
    assert payload["current_price"] == pytest.approx(0.04)
    assert "max-loss" in payload["reason"]


def test_tail_price_gate_fires_alert_on_notional_veto(captured_alerts):
    # Tighten the max-loss cap so it doesn't trip first; force a
    # notional-only veto.
    ctx = _make_ctx(
        side="YES",
        current_price=0.05,
        intended_quantity=10_000,
        intended_notional_usd=10_000 * 0.05,  # $500
        overrides={
            "max_tail_loss_usd": 0,        # disable max-loss subcheck
            "max_tail_notional_usd": 100,  # very tight
        },
    )
    res = asyncio.get_event_loop().run_until_complete(
        gates_mod.gate_tail_price_size_cap(ctx)
    )
    assert res.passed is False
    assert len(captured_alerts) == 1
    assert "notional" in captured_alerts[0]["reason"]


def test_jumbo_gate_fires_alert_on_veto(captured_alerts):
    ctx = _make_ctx(intended_quantity=2_000)  # > 750 default cap
    res = asyncio.get_event_loop().run_until_complete(
        gates_mod.gate_jumbo_quantity_cap(ctx)
    )
    assert res.passed is False
    assert len(captured_alerts) == 1
    payload = captured_alerts[0]
    assert payload["gate_name"] == "jumbo_quantity_cap"
    assert payload["intended_quantity"] == 2_000
    assert "jumbo cap" in payload["reason"]


def test_passing_gate_does_not_fire_alert(captured_alerts):
    """A normal in-cap candidate must not generate an alert."""
    ctx = _make_ctx(
        side="YES", current_price=0.50, intended_quantity=10
    )
    res1 = asyncio.get_event_loop().run_until_complete(
        gates_mod.gate_tail_price_size_cap(ctx)
    )
    res2 = asyncio.get_event_loop().run_until_complete(
        gates_mod.gate_jumbo_quantity_cap(ctx)
    )
    assert res1.passed is True and res2.passed is True
    assert captured_alerts == []


def test_dedupe_suppresses_repeat_alert_for_same_market(monkeypatch):
    """A market that keeps tripping the gate must only generate one
    email per dedupe window — `dedupe_or_skip` returning False short-
    circuits the dispatch."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        alerts_mod, "tail_risk_gate_trip",
        lambda **kw: captured.append(kw) or {"queued": True},
        raising=True,
    )
    # First call wins, second loses.
    claim_results = iter([True, False])
    async def _claim(*a, **k):
        return next(claim_results)
    monkeypatch.setattr(alerts_mod, "dedupe_or_skip", _claim, raising=True)

    ctx = _make_ctx(intended_quantity=2_000, market_id="REPEAT-MKT-1")
    asyncio.get_event_loop().run_until_complete(
        gates_mod.gate_jumbo_quantity_cap(ctx)
    )
    asyncio.get_event_loop().run_until_complete(
        gates_mod.gate_jumbo_quantity_cap(ctx)
    )
    # Gate vetoed both times, but only one alert fired.
    assert len(captured) == 1


def test_alert_dispatch_failure_does_not_break_gate(monkeypatch):
    """If the alert plumbing throws (DB down, postmark unavailable),
    the gate verdict must still propagate — the order MUST still be
    rejected."""
    def _boom(**kwargs):
        raise RuntimeError("postmark unavailable")

    async def _claim(*a, **k):
        return True

    monkeypatch.setattr(alerts_mod, "tail_risk_gate_trip", _boom, raising=True)
    monkeypatch.setattr(alerts_mod, "dedupe_or_skip", _claim, raising=True)

    ctx = _make_ctx(intended_quantity=2_000)
    res = asyncio.get_event_loop().run_until_complete(
        gates_mod.gate_jumbo_quantity_cap(ctx)
    )
    # Veto verdict and reason still surface even when the alert blew up.
    assert res.passed is False
    assert "jumbo cap" in res.reason


def test_tail_risk_gate_trip_alert_dry_run_renders_subject_and_body():
    """End-to-end of the alert renderer in `dry_run=True` mode (no
    Postmark call) — covers the subject + body + tag mapping."""
    out = alerts_mod.tail_risk_gate_trip(
        gate_name="tail_price_size_cap",
        market_id="KXNBABOSWAR-25FEB20",
        vertical="sports",
        side="NO",
        intended_quantity=4_394,
        current_price=0.04,
        reason="tail-priced market (NO entry $0.960); max-loss $4218.24 > cap $400.00",
        details={
            "threshold": 0.10,
            "tail_skew": 0.04,
            "entry_price": 0.96,
            "max_loss_usd": 4218.24,
            "max_loss_cap_usd": 400.0,
        },
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["tag"] == "tail-risk-gate-trip"
    assert "tail_price_size_cap" in out["subject"]
    assert "KXNBABOSWAR-25FEB20" in out["subject"]
    assert out["user_pref_column"] == "email_drift"
    body = out["body_preview"]
    assert "tail_price_size_cap" in body
    assert "max-loss" in body
    assert "$0.0400" in body  # current_price formatted
