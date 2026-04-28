"""Unit tests for per-vertical gatekeeper-knob tuning (Task #714).

Covers four contracts:

1. **Storage**: ``set_gatekeeper_override`` writes the right
   ``system_settings`` key (``risk.<knob>`` for global,
   ``risk.<knob>.<vertical>`` for per-vertical) and an audit row
   with the prior + new value.  Idempotent set-to-current returns
   ``no_op=True`` and writes neither.

2. **Validation**: out-of-range, unknown-knob, and unknown-vertical
   inputs raise ``ValueError`` (the API layer maps these to HTTP 400).

3. **Effective view**: ``get_gatekeeper_effective_state`` resolves
   per-vertical → global → mode-default in that exact order and
   exposes the ``source`` tag.

4. **Gate read path**: ``app.risk.gates._resolve_per_vertical_override``
   honours the same precedence so the gate behaviour matches what
   the operator sees in the effective view.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import runtime_settings as runtime_settings_mod
from app.risk import gates as gates_mod
from app.risk.gates import RiskContext


# ---------------------------------------------------------------------------
# In-memory stub for the asyncpg pool — covers exactly the surface the
# gatekeeper-tuning helpers touch (system_settings + the new
# gatekeeper_setting_history audit table) plus the bits
# ``get_aggression_mode`` reads.
# ---------------------------------------------------------------------------


class _MemDb:
    def __init__(self) -> None:
        self.system_settings: dict[str, str] = {}
        self.runtime_settings: dict[str, str] = {}
        self.history: list[dict[str, Any]] = []
        self._next_id = 1

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM gatekeeper_setting_history" in sql:
            rows = list(self.history)
            # naive WHERE / ORDER / LIMIT handling matches what the
            # production helper builds
            if "knob = $1" in sql and "vertical = $2" in sql:
                rows = [
                    r
                    for r in rows
                    if r["knob"] == args[0] and r["vertical"] == args[1]
                ]
                limit = args[2]
            elif "knob = $1" in sql and "vertical IS NULL" in sql:
                rows = [
                    r for r in rows if r["knob"] == args[0] and r["vertical"] is None
                ]
                limit = args[1]
            elif "knob = $1" in sql:
                rows = [r for r in rows if r["knob"] == args[0]]
                limit = args[1]
            elif "vertical = $1" in sql:
                rows = [r for r in rows if r["vertical"] == args[0]]
                limit = args[1]
            elif "vertical IS NULL" in sql:
                rows = [r for r in rows if r["vertical"] is None]
                limit = args[0]
            else:
                limit = args[0] if args else 100
            rows.sort(key=lambda r: (r["changed_at"], r["id"]), reverse=True)
            return rows[: int(limit)]
        if "FROM system_settings" in sql and "LIKE" in sql:
            pattern = args[0] if args else ""
            prefix = pattern.rstrip("%")
            return [
                {"key": k, "value": v}
                for k, v in self.system_settings.items()
                if k.startswith(prefix)
            ]
        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM system_settings" in sql:
            key = args[0] if args else None
            if key in self.system_settings:
                return {"value": self.system_settings[key]}
            return None
        if "FROM runtime_settings" in sql and "aggression_mode" in sql:
            return None
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "key = 'aggression_mode'" in sql:
            return self.runtime_settings.get("aggression_mode")
        if "key = 'trading_halted'" in sql:
            return self.runtime_settings.get("trading_halted")
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO gatekeeper_setting_history" in sql:
            from datetime import datetime, timezone
            self.history.append(
                {
                    "id": self._next_id,
                    "knob": args[0],
                    "vertical": args[1],
                    "old_value": args[2],
                    "new_value": args[3],
                    "changed_by_user_id": args[4],
                    "changed_by_email": args[5],
                    "reason": args[6],
                    "changed_at": datetime.now(timezone.utc),
                }
            )
            self._next_id += 1
        elif "INSERT INTO system_settings" in sql:
            key, value = args[0], args[1]
            self.system_settings[key] = value
        elif "DELETE FROM system_settings" in sql:
            self.system_settings.pop(args[0], None)
        return "OK"

    def acquire(self):  # pragma: no cover — not used by tuning helpers
        parent = self

        class _Conn:
            async def execute(self_inner, sql: str, *args: Any) -> str:
                return await parent.execute(sql, *args)

        class _Ctx:
            async def __aenter__(self_inner):
                return _Conn()

            async def __aexit__(self_inner, *exc):
                return None

        return _Ctx()


@pytest.fixture
def mem_db(monkeypatch):
    db = _MemDb()
    monkeypatch.setattr(runtime_settings_mod, "db", db, raising=True)
    return db


# ---------------------------------------------------------------------------
# Storage contract
# ---------------------------------------------------------------------------


def test_set_global_override_writes_system_settings_and_audit(mem_db):
    res = asyncio.run(
        runtime_settings_mod.set_gatekeeper_override(
            knob="min_edge_pp",
            vertical=None,
            value=5.5,
            actor_user_id="user_admin",
            actor_email="ops@example.com",
            reason="tighten gate",
        )
    )
    assert res["changed"] is True
    assert res["no_op"] is False
    assert res["key"] == "risk.min_edge_pp"
    assert res["old_value"] is None
    assert res["new_value"] == 5.5
    assert mem_db.system_settings["risk.min_edge_pp"] == "5.5"
    assert len(mem_db.history) == 1
    row = mem_db.history[0]
    assert row["knob"] == "min_edge_pp"
    assert row["vertical"] is None
    assert row["old_value"] is None
    assert row["new_value"] == "5.5"
    assert row["changed_by_email"] == "ops@example.com"
    assert row["reason"] == "tighten gate"


def test_set_per_vertical_override_uses_dotted_key(mem_db, monkeypatch):
    # Stub the verticals registry so unknown-vertical validation
    # passes for the tests without needing the real registry.
    monkeypatch.setattr(
        "app.verticals.registry.has", lambda name: name == "crypto"
    )
    res = asyncio.run(
        runtime_settings_mod.set_gatekeeper_override(
            knob="kelly_fraction_cap",
            vertical="CRYPTO",
            value=0.10,
            actor_user_id="u1",
            actor_email="ops@example.com",
            reason=None,
        )
    )
    assert res["key"] == "risk.kelly_fraction_cap.crypto"
    assert mem_db.system_settings["risk.kelly_fraction_cap.crypto"] == "0.1"
    assert mem_db.history[0]["vertical"] == "crypto"


def test_set_to_current_value_is_noop(mem_db):
    asyncio.run(
        runtime_settings_mod.set_gatekeeper_override(
            knob="min_agreement_rate",
            vertical=None,
            value=0.55,
            actor_user_id="u1",
            actor_email="ops@example.com",
            reason="initial",
        )
    )
    assert len(mem_db.history) == 1
    res = asyncio.run(
        runtime_settings_mod.set_gatekeeper_override(
            knob="min_agreement_rate",
            vertical=None,
            value=0.55,
            actor_user_id="u1",
            actor_email="ops@example.com",
            reason="re-apply same value",
        )
    )
    assert res["no_op"] is True
    assert res["changed"] is False
    # History must not grow on a no-op set.
    assert len(mem_db.history) == 1


def test_clear_existing_override_writes_audit_row(mem_db):
    asyncio.run(
        runtime_settings_mod.set_gatekeeper_override(
            knob="max_confidence_band",
            vertical=None,
            value=0.20,
            actor_user_id="u1",
            actor_email="ops@example.com",
            reason="initial",
        )
    )
    assert "risk.max_confidence_band" in mem_db.system_settings
    res = asyncio.run(
        runtime_settings_mod.clear_gatekeeper_override(
            knob="max_confidence_band",
            vertical=None,
            actor_user_id="u1",
            actor_email="ops@example.com",
            reason="revert",
        )
    )
    assert res["changed"] is True
    assert res["new_value"] is None
    assert "risk.max_confidence_band" not in mem_db.system_settings
    assert len(mem_db.history) == 2
    last = mem_db.history[-1]
    assert last["old_value"] == "0.2"
    assert last["new_value"] is None
    assert last["reason"] == "revert"


def test_clear_absent_override_is_noop(mem_db):
    res = asyncio.run(
        runtime_settings_mod.clear_gatekeeper_override(
            knob="min_edge_pp",
            vertical=None,
            actor_user_id="u1",
            actor_email="ops@example.com",
            reason="not present",
        )
    )
    assert res["changed"] is False
    assert res["no_op"] is True
    assert len(mem_db.history) == 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_knob_rejected(mem_db):
    with pytest.raises(ValueError, match="unknown gatekeeper knob"):
        asyncio.run(
            runtime_settings_mod.set_gatekeeper_override(
                knob="not_a_real_knob",
                vertical=None,
                value=1.0,
                actor_user_id="u1",
                actor_email=None,
                reason=None,
            )
        )


def test_out_of_range_rejected(mem_db):
    # min_edge_pp spec is [0.0, 25.0] — 99 is way out of range.
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(
            runtime_settings_mod.set_gatekeeper_override(
                knob="min_edge_pp",
                vertical=None,
                value=99.0,
                actor_user_id="u1",
                actor_email=None,
                reason=None,
            )
        )
    # And the negative side too.
    with pytest.raises(ValueError, match="outside"):
        asyncio.run(
            runtime_settings_mod.set_gatekeeper_override(
                knob="kelly_fraction_cap",
                vertical=None,
                value=-0.5,
                actor_user_id="u1",
                actor_email=None,
                reason=None,
            )
        )


def test_unknown_vertical_rejected(mem_db, monkeypatch):
    monkeypatch.setattr(
        "app.verticals.registry.has", lambda name: name == "crypto"
    )
    with pytest.raises(ValueError, match="unknown vertical"):
        asyncio.run(
            runtime_settings_mod.set_gatekeeper_override(
                knob="min_edge_pp",
                vertical="not_a_vertical",
                value=4.0,
                actor_user_id="u1",
                actor_email=None,
                reason=None,
            )
        )


def test_kelly_multiplier_is_not_a_tunable_knob():
    # Surfaced read-only in the effective view; explicit POST/DELETE
    # must reject it so the operator can't tune it from this surface.
    assert "kelly_multiplier" not in runtime_settings_mod.GATEKEEPER_TUNABLE_KNOBS
    assert "kelly_multiplier" in runtime_settings_mod.GATEKEEPER_READONLY_KNOBS


# ---------------------------------------------------------------------------
# Effective-view precedence
# ---------------------------------------------------------------------------


def test_effective_state_resolves_precedence(mem_db, monkeypatch):
    monkeypatch.setattr(
        "app.verticals.registry.has", lambda name: name == "crypto"
    )
    # 1. No overrides -> mode default (balanced -> min_edge_pp = 4.0).
    state = asyncio.run(
        runtime_settings_mod.get_gatekeeper_effective_state("crypto")
    )
    knob = next(k for k in state["knobs"] if k["key"] == "min_edge_pp")
    assert knob["mode_default"] == 4.0
    assert knob["global_override"] is None
    assert knob["per_vertical_override"] is None
    assert knob["effective"] == 4.0
    assert knob["source"] == "mode_default"

    # 2. Set global override -> effective falls back to global.
    asyncio.run(
        runtime_settings_mod.set_gatekeeper_override(
            knob="min_edge_pp",
            vertical=None,
            value=6.0,
            actor_user_id="u1",
            actor_email=None,
            reason=None,
        )
    )
    state = asyncio.run(
        runtime_settings_mod.get_gatekeeper_effective_state("crypto")
    )
    knob = next(k for k in state["knobs"] if k["key"] == "min_edge_pp")
    assert knob["global_override"] == 6.0
    assert knob["per_vertical_override"] is None
    assert knob["effective"] == 6.0
    assert knob["source"] == "global"

    # 3. Add per-vertical override -> wins over the global.
    asyncio.run(
        runtime_settings_mod.set_gatekeeper_override(
            knob="min_edge_pp",
            vertical="crypto",
            value=8.5,
            actor_user_id="u1",
            actor_email=None,
            reason=None,
        )
    )
    state = asyncio.run(
        runtime_settings_mod.get_gatekeeper_effective_state("crypto")
    )
    knob = next(k for k in state["knobs"] if k["key"] == "min_edge_pp")
    assert knob["global_override"] == 6.0
    assert knob["per_vertical_override"] == 8.5
    assert knob["effective"] == 8.5
    assert knob["source"] == "per_vertical"

    # 4. A different vertical without its own override falls back to
    # the global on the same call.
    monkeypatch.setattr(
        "app.verticals.registry.has", lambda name: name in {"crypto", "sports"}
    )
    state = asyncio.run(
        runtime_settings_mod.get_gatekeeper_effective_state("sports")
    )
    knob = next(k for k in state["knobs"] if k["key"] == "min_edge_pp")
    assert knob["per_vertical_override"] is None
    assert knob["effective"] == 6.0
    assert knob["source"] == "global"


def test_effective_state_includes_readonly_kelly_multiplier(mem_db):
    state = asyncio.run(
        runtime_settings_mod.get_gatekeeper_effective_state(None)
    )
    keys = [k["key"] for k in state["knobs"]]
    assert "kelly_multiplier" in keys
    km = next(k for k in state["knobs"] if k["key"] == "kelly_multiplier")
    assert km["read_only"] is True


# ---------------------------------------------------------------------------
# Gate read-path matches the effective view (Task #714 contract)
# ---------------------------------------------------------------------------


def _ctx(vertical: str, overrides: dict[str, Any]) -> RiskContext:
    return RiskContext(
        market_id="m1",
        side="YES",
        vertical=vertical,
        league=None,
        market_type=None,
        matchup=None,
        current_price=0.50,
        model_probability=0.55,
        edge=0.05,  # 5pp gross edge
        confidence_band_low=0.50,
        confidence_band_high=0.60,
        agreement_rate=0.90,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=1,
        intended_quantity=10,
        intended_notional_usd=5.0,
        bankroll_usd=1000.0,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides=overrides,
        market_cooldown_until=None,
    )


def test_gate_resolver_per_vertical_beats_global():
    # Per-vertical key (`min_edge_pp.crypto`) wins over the global
    # key (`min_edge_pp`) which wins over the fallback.
    val = gates_mod._resolve_per_vertical_override(
        _ctx("crypto", {"min_edge_pp": 5.0, "min_edge_pp.crypto": 9.0}),
        "min_edge_pp",
        3.0,
    )
    assert val == 9.0


def test_gate_resolver_global_beats_fallback():
    val = gates_mod._resolve_per_vertical_override(
        _ctx("crypto", {"min_edge_pp": 5.0}),
        "min_edge_pp",
        3.0,
    )
    assert val == 5.0


def test_gate_resolver_falls_back_to_default_when_neither_set():
    val = gates_mod._resolve_per_vertical_override(
        _ctx("crypto", {}),
        "min_edge_pp",
        3.0,
    )
    assert val == 3.0


def test_gate_resolver_ignores_other_verticals_per_vertical_row():
    # A `min_edge_pp.sports` row must NOT influence the crypto vertical;
    # only the matching per-vertical key applies.
    val = gates_mod._resolve_per_vertical_override(
        _ctx("crypto", {"min_edge_pp.sports": 12.0, "min_edge_pp": 4.0}),
        "min_edge_pp",
        3.0,
    )
    assert val == 4.0


def test_gate_min_edge_uses_per_vertical_override():
    # Per-vertical override of 6pp on a candidate with edge=5% must
    # block; the same override at 4pp must pass.
    blocked = asyncio.run(
        gates_mod.gate_min_edge(
            _ctx("crypto", {"min_edge_pp.crypto": 6.0})
        )
    )
    assert blocked.passed is False
    assert "min" in blocked.reason

    passed = asyncio.run(
        gates_mod.gate_min_edge(
            _ctx("crypto", {"min_edge_pp.crypto": 4.0})
        )
    )
    assert passed.passed is True


# ===========================================================================
# API endpoint contract tests (HTTP shape, alert dispatch, no-op suppression)
# ===========================================================================

from contextlib import asynccontextmanager  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import auth as auth_module  # noqa: E402
from app.auth import AuthedUser  # noqa: E402
from app.main import app  # noqa: E402
from app.notifications import alerts as alerts_module  # noqa: E402


@pytest.fixture
def api_client(monkeypatch, mem_db):
    """Wire a TestClient with auth bypassed via dependency_overrides
    and the runtime_settings DB pointed at the in-memory stub.

    The vertical registry is also stubbed so the validation paths see
    a deterministic set of registered verticals.
    """
    fake_admin = AuthedUser(
        user_id="user_api_test_admin",
        session_id="sess_test",
        claims={"email": "ops-api@example.com"},
    )

    async def _fake_check_approval(user):
        return True, "ops-api@example.com"

    monkeypatch.setattr(auth_module, "check_approval", _fake_check_approval)
    monkeypatch.setattr(
        "app.main.check_approval", _fake_check_approval, raising=False
    )
    monkeypatch.setattr(
        "app.verticals.registry.has",
        lambda name: name in {"crypto", "sports", "weather"},
    )
    monkeypatch.setattr(
        "app.verticals.registry.names",
        lambda: ["crypto", "sports", "weather"],
    )

    # Construct TestClient without `with` so we don't trigger the
    # FastAPI lifespan (alembic upgrade + asyncpg pool) — these tests
    # only exercise the route layer with our in-memory stub.
    app.dependency_overrides[auth_module.require_admin] = lambda: fake_admin
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(auth_module.require_admin, None)


@pytest.fixture
def alert_calls(monkeypatch):
    """Capture every gatekeeper_override_changed dispatch."""
    calls: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        alerts_module, "gatekeeper_override_changed", _capture, raising=True
    )
    return calls


# ---------------------------------------------------------------------------
# GET /api/admin/gatekeeper/effective
# ---------------------------------------------------------------------------


def test_get_effective_shape_global_scope(api_client, mem_db):
    r = api_client.get("/api/admin/gatekeeper/effective")
    assert r.status_code == 200, r.text
    body = r.json()
    keys = {k["key"] for k in body["knobs"]}
    assert {"min_edge_pp", "min_agreement_rate", "max_confidence_band",
            "kelly_fraction_cap", "kelly_multiplier"}.issubset(keys)
    assert body["vertical"] is None
    assert "available_verticals" in body
    # mem_db has no overrides yet -> source is mode_default for tunables.
    edge = next(k for k in body["knobs"] if k["key"] == "min_edge_pp")
    assert edge["source"] == "mode_default"


def test_get_effective_shape_per_vertical_scope(api_client, mem_db):
    r = api_client.get(
        "/api/admin/gatekeeper/effective", params={"vertical": "crypto"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vertical"] == "crypto"


# ---------------------------------------------------------------------------
# POST /api/admin/gatekeeper/override — set
# ---------------------------------------------------------------------------


def test_post_set_global_dispatches_alert_and_writes_audit(
    api_client, mem_db, alert_calls
):
    r = api_client.post(
        "/api/admin/gatekeeper/override",
        json={
            "knob": "min_edge_pp",
            "vertical": None,
            "value": 6.0,
            "reason": "tighten via API",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_change"]["changed"] is True
    assert body["last_change"]["no_op"] is False
    assert mem_db.system_settings["risk.min_edge_pp"] == "6.0"
    assert len(mem_db.history) == 1
    # Real change -> exactly one operator alert dispatched.
    assert len(alert_calls) == 1
    assert alert_calls[0]["knob"] == "min_edge_pp"
    assert alert_calls[0]["vertical"] is None
    assert alert_calls[0]["new_value"] == 6.0
    assert alert_calls[0]["actor_email"] == "ops-api@example.com"


def test_post_set_per_vertical_uses_dotted_key(api_client, mem_db, alert_calls):
    r = api_client.post(
        "/api/admin/gatekeeper/override",
        json={
            "knob": "min_edge_pp",
            "vertical": "crypto",
            "value": 8.0,
            "reason": None,
        },
    )
    assert r.status_code == 200, r.text
    assert mem_db.system_settings["risk.min_edge_pp.crypto"] == "8.0"
    assert alert_calls[0]["vertical"] == "crypto"


def test_post_set_to_current_value_is_noop_no_alert(
    api_client, mem_db, alert_calls
):
    # First write lands an audit row + 1 alert.
    api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "kelly_fraction_cap", "vertical": None, "value": 0.30,
              "reason": "initial"},
    )
    assert len(alert_calls) == 1
    assert len(mem_db.history) == 1

    # Re-saving the same value must short-circuit.
    r = api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "kelly_fraction_cap", "vertical": None, "value": 0.30,
              "reason": "re-apply"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_change"]["no_op"] is True
    assert body["last_change"]["changed"] is False
    # No new audit row, no second alert.
    assert len(mem_db.history) == 1
    assert len(alert_calls) == 1


def test_post_out_of_range_returns_400(api_client, mem_db, alert_calls):
    r = api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "min_edge_pp", "vertical": None, "value": 999.0,
              "reason": None},
    )
    assert r.status_code == 400, r.text
    assert "min_edge_pp" in r.json()["detail"]
    assert mem_db.system_settings == {}
    assert len(mem_db.history) == 0
    assert len(alert_calls) == 0


def test_post_unknown_knob_returns_400(api_client, mem_db, alert_calls):
    r = api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "not_a_knob", "vertical": None, "value": 1.0,
              "reason": None},
    )
    assert r.status_code == 400, r.text
    assert "not_a_knob" in r.json()["detail"]
    assert len(alert_calls) == 0


def test_post_unknown_vertical_returns_400(api_client, mem_db, alert_calls):
    r = api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "min_edge_pp", "vertical": "atlantis", "value": 4.0,
              "reason": None},
    )
    assert r.status_code == 400, r.text
    assert "atlantis" in r.json()["detail"]
    assert len(alert_calls) == 0


def test_post_kelly_multiplier_rejected(api_client, mem_db, alert_calls):
    # Read-only — must not be writable through this surface.
    r = api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "kelly_multiplier", "vertical": "crypto", "value": 1.0,
              "reason": None},
    )
    assert r.status_code == 400, r.text
    assert len(alert_calls) == 0


def test_post_value_null_clears_existing_override(api_client, mem_db, alert_calls):
    # Seed an override.
    api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "max_confidence_band", "vertical": None, "value": 0.20,
              "reason": "set"},
    )
    assert "risk.max_confidence_band" in mem_db.system_settings
    assert len(alert_calls) == 1

    # value=null routes to clear and writes a clear-audit row + alert.
    r = api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "max_confidence_band", "vertical": None, "value": None,
              "reason": "clear via POST"},
    )
    assert r.status_code == 200, r.text
    assert "risk.max_confidence_band" not in mem_db.system_settings
    assert len(alert_calls) == 2
    assert alert_calls[1]["new_value"] is None


# ---------------------------------------------------------------------------
# DELETE /api/admin/gatekeeper/override
# ---------------------------------------------------------------------------


def test_delete_existing_override_dispatches_alert(
    api_client, mem_db, alert_calls
):
    api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "min_agreement_rate", "vertical": None, "value": 0.65,
              "reason": "set"},
    )
    assert len(alert_calls) == 1

    r = api_client.delete(
        "/api/admin/gatekeeper/override",
        params={"knob": "min_agreement_rate", "reason": "rollback"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_change"]["changed"] is True
    assert "risk.min_agreement_rate" not in mem_db.system_settings
    assert len(alert_calls) == 2
    assert alert_calls[1]["new_value"] is None


def test_delete_absent_override_is_noop_no_alert(
    api_client, mem_db, alert_calls
):
    r = api_client.delete(
        "/api/admin/gatekeeper/override",
        params={"knob": "min_edge_pp", "vertical": "crypto",
                "reason": "nothing to do"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_change"]["no_op"] is True
    assert body["last_change"]["changed"] is False
    assert len(alert_calls) == 0
    assert len(mem_db.history) == 0


# ---------------------------------------------------------------------------
# GET /api/admin/gatekeeper/history
# ---------------------------------------------------------------------------


def test_get_history_returns_audit_rows(api_client, mem_db, alert_calls):
    api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "min_edge_pp", "vertical": "crypto", "value": 7.0,
              "reason": "first"},
    )
    api_client.post(
        "/api/admin/gatekeeper/override",
        json={"knob": "min_edge_pp", "vertical": "crypto", "value": 8.0,
              "reason": "raise"},
    )
    r = api_client.get(
        "/api/admin/gatekeeper/history",
        params={"knob": "min_edge_pp", "vertical": "crypto", "limit": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["history"]) == 2
    # Most recent first per the helper's ORDER BY.
    assert body["history"][0]["new_value"] == "8.0"
    assert body["history"][1]["new_value"] == "7.0"
