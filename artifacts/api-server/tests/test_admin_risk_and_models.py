"""Positive-path tests for the admin risk-knob + active-model endpoints.

Complements `test_auth_gate.py` (which only proves these routes reject
unauthenticated callers) by exercising the validation + audit-log
contract:

  * POST /api/admin/risk-knobs with an out-of-bounds value returns 400
    and writes nothing to system_settings or admin_audit_log.
  * POST /api/admin/risk-knobs with valid overrides upserts every
    requested ``risk.<knob>`` row and writes one
    ``set_risk_knob`` audit row.
  * POST /api/admin/models/active with an unknown predictor returns 400.
  * POST /api/admin/models/active with a known predictor persists
    ``predictor.active_name`` in system_settings and flips
    ``predictor.get_active_override()``.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import admin as admin_module
from app import auth as auth_module
from app import db as db_module
from app import models_loader as models_loader_module
from app import predictor as predictor_module
from app import runtime_settings as runtime_settings_mod
from app.main import app
from tests.conftest import TEST_CLERK_HOST


ISSUER = f"https://{TEST_CLERK_HOST}"
ADMIN_UID = "user_admin_risk_models"


# ---------------------------------------------------------------------------
# JWKS / JWT plumbing — same approach as test_admin_endpoints.py.
# ---------------------------------------------------------------------------


class _StubSigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _StubJWKSClient:
    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> _StubSigningKey:
        return _StubSigningKey(self._public_key)


@pytest.fixture(scope="module")
def _keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mint(private_key, *, sub: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "sub": sub,
            "iat": now - 60,
            "nbf": now - 60,
            "exp": now + 3600,
            "sid": "sess_test",
        },
        private_key,
        algorithm="RS256",
    )


# ---------------------------------------------------------------------------
# In-memory stand-in for the asyncpg pool covering all SQL these endpoints
# touch (app_users for auth, system_settings for risk knobs + active-model,
# runtime_settings for the aggression-mode read, admin_audit_log).
# ---------------------------------------------------------------------------


@dataclass
class FakeStore:
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    system_settings: dict[str, str] = field(default_factory=dict)
    runtime_settings: dict[str, str] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)

    def add_admin(self, uid: str, *, email: str = "admin@example.com") -> None:
        self.users[uid] = {
            "clerk_user_id": uid,
            "email": email,
            "display_name": None,
            "approved": True,
            "is_admin": True,
            "denied_at": None,
            "activity_count": 0,
            "first_seen_at": None,
            "last_seen_at": None,
            "clerk_created_at": None,
            "clerk_last_sign_in_at": None,
            "clerk_synced_at": None,
        }


def _norm(sql: str) -> str:
    return " ".join(sql.split()).lower()


class FakeConn:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = _norm(query)
        if q.startswith("select * from app_users where clerk_user_id"):
            return self.store.users.get(args[0])
        if q.startswith("insert into app_users"):
            uid = args[0]
            if uid not in self.store.users:
                self.store.users[uid] = {
                    "clerk_user_id": uid,
                    "email": args[1] if len(args) > 1 else None,
                    "display_name": args[2] if len(args) > 2 else None,
                    "approved": False,
                    "is_admin": False,
                    "denied_at": None,
                }
            return self.store.users[uid]
        return None

    async def fetchval(self, query: str, *args: Any):
        q = _norm(query)
        if "from runtime_settings" in q and "where key = 'aggression_mode'" in q:
            return self.store.runtime_settings.get("aggression_mode")
        if "from runtime_settings" in q and "where key = $1" in q:
            return self.store.runtime_settings.get(args[0])
        if "from system_settings" in q and "where key =" in q and args:
            return self.store.system_settings.get(args[0])
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        q = _norm(query)
        if "from system_settings" in q and "key like 'risk.%'" in q:
            return [
                {"key": k, "value": v}
                for k, v in self.store.system_settings.items()
                if k.startswith("risk.")
            ]
        return []

    async def execute(self, query: str, *args: Any) -> str:
        q = _norm(query)
        if q.startswith("create table"):
            return "OK"
        if q.startswith("insert into system_settings"):
            # Two argument shapes:
            #   ('predictor.active_name', $1, now())  → params=(value,)
            #   ($1, $2, now())                       → params=(key, value)
            if "'predictor.active_name'" in q:
                self.store.system_settings["predictor.active_name"] = str(args[0])
            else:
                self.store.system_settings[args[0]] = str(args[1])
            return "OK"
        if q.startswith("delete from system_settings"):
            if "'predictor.active_name'" in q:
                self.store.system_settings.pop("predictor.active_name", None)
            elif args:
                self.store.system_settings.pop(args[0], None)
            return "OK"
        if q.startswith("insert into runtime_settings"):
            # Two argument shapes (see runtime_settings.set_aggression_mode);
            # not needed for these endpoints but kept inert for safety.
            if len(args) >= 3:
                self.store.runtime_settings[args[0]] = str(args[1])
            return "OK"
        if q.startswith("delete from runtime_settings"):
            self.store.runtime_settings.pop(args[0], None)
            return "OK"
        if q.startswith("insert into admin_audit_log"):
            self.store.audit.append(
                {
                    "actor_user_id": args[0],
                    "actor_email": args[1],
                    "target_user_id": args[2],
                    "target_email": args[3],
                    "action": args[4],
                    "before_value": args[5],
                    "after_value": args[6],
                }
            )
            return "OK"
        return "OK"


# ---------------------------------------------------------------------------
# Stub for the Clerk Backend API HTTP client used during admin auth.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 404, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *_, **__) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get(self, *_a: Any, **_k: Any) -> _FakeResponse:
        # 404 → admin._fetch_clerk_profile yields {} and the auth path
        # falls back to the in-DB app_users row, which we fully control.
        return _FakeResponse(404, {})

    async def patch(self, *_a: Any, **_k: Any) -> _FakeResponse:
        return _FakeResponse(200, {})


# ---------------------------------------------------------------------------
# Autouse fixture wiring everything together.
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch: pytest.MonkeyPatch, _keypair, store: FakeStore):
    auth_module._frontend_api_host.cache_clear()
    auth_module._issuer.cache_clear()
    auth_module._jwks_client.cache_clear()
    auth_module._user_cache.clear()
    monkeypatch.setattr(
        auth_module,
        "_jwks_client",
        lambda: _StubJWKSClient(_keypair.public_key()),
    )
    monkeypatch.setattr(admin_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_admin_risk_models")
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)

    @asynccontextmanager
    async def fake_acquire():
        yield FakeConn(store)

    monkeypatch.setattr(db_module, "acquire", fake_acquire)
    monkeypatch.setattr(admin_module.db, "acquire", fake_acquire)
    monkeypatch.setattr(runtime_settings_mod.db, "acquire", fake_acquire)

    async def fake_execute(query: str, *args: Any) -> str:
        async with fake_acquire() as c:
            return await c.execute(query, *args)

    async def fake_fetchval(query: str, *args: Any):
        async with fake_acquire() as c:
            return await c.fetchval(query, *args)

    async def fake_fetch(query: str, *args: Any):
        async with fake_acquire() as c:
            return await c.fetch(query, *args)

    async def fake_fetchrow(query: str, *args: Any):
        async with fake_acquire() as c:
            return await c.fetchrow(query, *args)

    monkeypatch.setattr(db_module, "execute", fake_execute)
    monkeypatch.setattr(db_module, "fetchval", fake_fetchval)
    monkeypatch.setattr(db_module, "fetch", fake_fetch)
    monkeypatch.setattr(db_module, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(runtime_settings_mod.db, "fetchval", fake_fetchval)
    monkeypatch.setattr(runtime_settings_mod.db, "fetch", fake_fetch)

    # Force the schema-bootstrap guards to "already ran" so we don't have
    # to script the CREATE TABLE round-trip for every test.
    runtime_settings_mod._ready = True
    runtime_settings_mod._system_settings_ready = True

    # Ensure the predictor override is clean for each test.
    predictor_module.set_active_override(None)

    yield

    auth_module._user_cache.clear()
    predictor_module.set_active_override(None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# /api/admin/risk-knobs
# ---------------------------------------------------------------------------


def test_risk_knobs_out_of_bounds_returns_400_and_writes_nothing(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    """Validation must reject a value outside the spec range BEFORE
    any DB write or audit-log row lands."""
    store.add_admin(ADMIN_UID)
    token = _mint(_keypair, sub=ADMIN_UID)

    # min_edge_pp spec is [0.0, 25.0]; 999 is clearly out of bounds.
    r = client.post(
        "/api/admin/risk-knobs",
        headers={"Authorization": f"Bearer {token}"},
        json={"overrides": {"min_edge_pp": 999.0}},
    )
    assert r.status_code == 400, r.text
    assert "min_edge_pp" in r.json()["detail"]

    # Nothing landed in system_settings under risk.* and no audit row.
    risk_rows = {k: v for k, v in store.system_settings.items() if k.startswith("risk.")}
    assert risk_rows == {}
    assert [a for a in store.audit if a["action"] == "set_risk_knob"] == []


def test_risk_knobs_unknown_key_returns_400(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    store.add_admin(ADMIN_UID)
    token = _mint(_keypair, sub=ADMIN_UID)

    r = client.post(
        "/api/admin/risk-knobs",
        headers={"Authorization": f"Bearer {token}"},
        json={"overrides": {"not_a_knob": 1.0}},
    )
    assert r.status_code == 400, r.text
    assert "not_a_knob" in r.json()["detail"]
    assert {k for k in store.system_settings if k.startswith("risk.")} == set()
    assert [a for a in store.audit if a["action"] == "set_risk_knob"] == []


def test_risk_knobs_valid_upserts_and_audits(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    store.add_admin(ADMIN_UID, email="boss@example.com")
    token = _mint(_keypair, sub=ADMIN_UID)

    r = client.post(
        "/api/admin/risk-knobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "overrides": {
                "min_edge_pp": 5.5,
                "kelly_fraction_cap": 0.3,
            }
        },
    )
    assert r.status_code == 200, r.text

    # Both knobs persisted under their ``risk.<key>`` row.
    assert store.system_settings.get("risk.min_edge_pp") == "5.5"
    assert store.system_settings.get("risk.kelly_fraction_cap") == "0.3"

    # Exactly one audit row, action=set_risk_knob, attributed to the
    # admin caller, with after_value carrying the new overrides.
    set_rows = [a for a in store.audit if a["action"] == "set_risk_knob"]
    assert len(set_rows) == 1
    row = set_rows[0]
    assert row["actor_user_id"] == ADMIN_UID
    assert row["target_user_id"] == "system"
    import json as _json

    after = _json.loads(row["after_value"])
    assert after["min_edge_pp"] == 5.5
    assert after["kelly_fraction_cap"] == 0.3

    # Response echoes the combined editor state with the overrides reflected.
    body = r.json()
    by_key = {k["key"]: k for k in body["knobs"]}
    assert by_key["min_edge_pp"]["override_value"] == 5.5
    assert by_key["min_edge_pp"]["effective_value"] == 5.5
    assert by_key["kelly_fraction_cap"]["override_value"] == 0.3


# ---------------------------------------------------------------------------
# /api/admin/models/active
# ---------------------------------------------------------------------------


def _patch_models_state(monkeypatch: pytest.MonkeyPatch, predictors: dict[str, dict[str, Any]]):
    """Substitute a deterministic model catalog so the admin endpoint
    can validate predictor names without touching the loader's cache."""
    state = models_loader_module.ModelsState(
        ready=True,
        in_progress=False,
        predictors=predictors,
    )
    monkeypatch.setattr(models_loader_module, "get_state", lambda: state)


def test_models_active_unknown_returns_400(
    client: TestClient, store: FakeStore, _keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_admin(ADMIN_UID)
    token = _mint(_keypair, sub=ADMIN_UID)
    _patch_models_state(
        monkeypatch,
        {"models/saved/meta_model.pkl": {"version": "v1.0", "sha256": "abc"}},
    )

    r = client.post(
        "/api/admin/models/active",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "models/versions/v9.999/meta_model.pkl"},
    )
    assert r.status_code == 400, r.text
    assert "unknown predictor" in r.json()["detail"]

    # No persistence + override stays cleared.
    assert "predictor.active_name" not in store.system_settings
    assert predictor_module.get_active_override() is None
    assert [a for a in store.audit if a["action"] == "set_active_model"] == []


def test_models_active_known_persists_and_flips_override(
    client: TestClient, store: FakeStore, _keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_admin(ADMIN_UID, email="boss@example.com")
    token = _mint(_keypair, sub=ADMIN_UID)
    target = "models/versions/v1.42/meta_model.pkl"
    _patch_models_state(
        monkeypatch,
        {
            "models/saved/meta_model.pkl": {"version": "v1.0", "sha256": "abc"},
            target: {"version": "v1.42", "sha256": "def"},
        },
    )
    # Disable the GitHub mirror background task so the test stays
    # hermetic — the background coroutine would otherwise fire-and-forget
    # against a real client.
    from app import github_mirror as _github_mirror

    monkeypatch.setattr(_github_mirror, "is_enabled", lambda: False)

    assert predictor_module.get_active_override() is None

    r = client.post(
        "/api/admin/models/active",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": target},
    )
    assert r.status_code == 200, r.text

    # Persisted under predictor.active_name.
    assert store.system_settings.get("predictor.active_name") == target
    # In-process override flipped immediately.
    assert predictor_module.get_active_override() == target

    # Audit row landed.
    set_rows = [a for a in store.audit if a["action"] == "set_active_model"]
    assert len(set_rows) == 1
    row = set_rows[0]
    assert row["actor_user_id"] == ADMIN_UID
    import json as _json

    assert _json.loads(row["after_value"])["name"] == target
    assert _json.loads(row["before_value"])["name"] is None


# ---------------------------------------------------------------------------
# /api/admin/orchestrator/run-cycle
# ---------------------------------------------------------------------------


def test_admin_run_cycle_refuses_until_ready_gate_flips(
    client: TestClient, store: FakeStore, _keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manual cycle endpoint must mirror the Pro+ endpoint and
    refuse with 503 + the ready-gate status until the loader signals
    READY — otherwise an operator could trade against half-loaded
    predictors."""
    store.add_admin(ADMIN_UID)
    token = _mint(_keypair, sub=ADMIN_UID)

    from app import main as app_main

    monkeypatch.setattr(app_main.ready_gate, "is_ready", lambda: False)
    monkeypatch.setattr(
        app_main.ready_gate,
        "status",
        lambda: {"checks": {"models": {"ok": False}}, "ready": False},
    )

    called: list[str] = []

    async def boom_run_cycle(_vertical: str) -> Any:  # pragma: no cover
        called.append("run_cycle")
        raise AssertionError("run_cycle must not run while gate is closed")

    monkeypatch.setattr(app_main, "run_cycle", boom_run_cycle)

    r = client.post(
        "/api/admin/orchestrator/run-cycle",
        headers={"Authorization": f"Bearer {token}"},
        json={"vertical": "sports"},
    )
    assert r.status_code == 503, r.text
    body = r.json()["detail"]
    assert body["status"] == "not_ready"
    assert called == []


def test_admin_run_cycle_returns_payload_and_filtered_trace(
    client: TestClient, store: FakeStore, _keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the gate is ready, the response must surface the cycle
    summary plus only the trace events emitted under that cycle_id —
    so the admin UI doesn't render rows from neighbouring cycles."""
    store.add_admin(ADMIN_UID)
    token = _mint(_keypair, sub=ADMIN_UID)

    from app import main as app_main
    from app.orchestrator.cycle import CycleResult

    monkeypatch.setattr(app_main.ready_gate, "is_ready", lambda: True)

    cr = CycleResult(
        cycle_id=4242,
        vertical="sports",
        started_at="2026-04-23T00:00:00Z",
        completed_at="2026-04-23T00:00:01Z",
        duration_seconds=1.0,
        picks_evaluated=3,
        trades_placed=1,
        fresh_debates=2,
        errors=[],
    )

    async def fake_run_cycle(vertical: str) -> CycleResult:
        assert vertical == "sports"
        return cr

    monkeypatch.setattr(app_main, "run_cycle", fake_run_cycle)
    monkeypatch.setattr(
        app_main.pipeline_event_hub,
        "recent_events",
        lambda max_cycles=10: [
            {"cycle_id": 4242, "stage": "picks", "ok": True},
            {"cycle_id": 4241, "stage": "picks", "ok": True},  # other cycle — filter out
            {"cycle_id": 4242, "stage": "trade", "ok": True},
        ],
    )

    r = client.post(
        "/api/admin/orchestrator/run-cycle",
        headers={"Authorization": f"Bearer {token}"},
        json={"vertical": "sports"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cycle_id"] == 4242
    assert body["vertical"] == "sports"
    assert body["picks_evaluated"] == 3
    assert body["trades_placed"] == 1
    # Trace must contain only the events tagged with our cycle_id.
    assert [ev["stage"] for ev in body["trace"]] == ["picks", "trade"]
    assert all(ev["cycle_id"] == 4242 for ev in body["trace"])


# ---------------------------------------------------------------------------
# /api/admin/rearm-trading
# ---------------------------------------------------------------------------


def test_rearm_trading_resets_mode_and_audits(
    client: TestClient,
    store: FakeStore,
    _keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After an emergency stop persisted ``risk.trading_mode='halt'``,
    POST /api/admin/rearm-trading must:

      * Persist the chosen mode (``paper`` or ``live``) into
        ``system_settings.risk.trading_mode``.
      * Call :func:`kalshi_lifecycle.resume` to clear the in-memory
        kill switch.
      * Write one ``rearm_trading`` audit row capturing the previous
        and new mode.
    """
    from app import main as app_main

    store.add_admin(ADMIN_UID, email="boss@example.com")
    store.system_settings["risk.trading_mode"] = "halt"
    token = _mint(_keypair, sub=ADMIN_UID)

    resume_calls: list[None] = []

    def _fake_resume() -> dict[str, Any]:
        resume_calls.append(None)
        return {"halted": False}

    monkeypatch.setattr(app_main.kalshi_lifecycle, "resume", _fake_resume)

    r = client.post(
        "/api/admin/rearm-trading",
        headers={"Authorization": f"Bearer {token}"},
        json={"mode": "paper"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "ok": True,
        "trading_mode": "paper",
        "previous_mode": "halt",
        "kill_switch": {"halted": False},
    }

    # Mode landed in system_settings, kill switch was cleared, audit
    # row captured the transition.
    assert store.system_settings["risk.trading_mode"] == "paper"
    assert len(resume_calls) == 1
    rearm_rows = [a for a in store.audit if a["action"] == "rearm_trading"]
    assert len(rearm_rows) == 1
    row = rearm_rows[0]
    assert row["actor_user_id"] == ADMIN_UID
    assert row["actor_email"] == "boss@example.com"
    import json as _json
    assert _json.loads(row["before_value"]) == {"trading_mode": "halt"}
    assert _json.loads(row["after_value"]) == {"trading_mode": "paper"}


def test_rearm_trading_to_live_persists(
    client: TestClient,
    store: FakeStore,
    _keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    store.add_admin(ADMIN_UID)
    store.system_settings["risk.trading_mode"] = "halt"
    token = _mint(_keypair, sub=ADMIN_UID)

    monkeypatch.setattr(
        app_main.kalshi_lifecycle, "resume", lambda: {"halted": False}
    )

    r = client.post(
        "/api/admin/rearm-trading",
        headers={"Authorization": f"Bearer {token}"},
        json={"mode": "live"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["trading_mode"] == "live"
    assert store.system_settings["risk.trading_mode"] == "live"


def test_rearm_trading_rejects_invalid_mode(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    store.add_admin(ADMIN_UID)
    token = _mint(_keypair, sub=ADMIN_UID)

    r = client.post(
        "/api/admin/rearm-trading",
        headers={"Authorization": f"Bearer {token}"},
        json={"mode": "halt"},
    )
    # Pydantic regex rejection — must not write anything or call resume.
    assert r.status_code == 422, r.text
    assert "risk.trading_mode" not in store.system_settings
    assert [a for a in store.audit if a["action"] == "rearm_trading"] == []


def test_rearm_trading_requires_admin(
    client: TestClient, store: FakeStore, _keypair
) -> None:
    """Non-admin signed-in caller must be rejected before any side effect."""
    NON_ADMIN_UID = "user_not_admin_rearm"
    store.users[NON_ADMIN_UID] = {
        "clerk_user_id": NON_ADMIN_UID,
        "email": "user@example.com",
        "display_name": None,
        "approved": True,
        "is_admin": False,
        "denied_at": None,
    }
    token = _mint(_keypair, sub=NON_ADMIN_UID)

    r = client.post(
        "/api/admin/rearm-trading",
        headers={"Authorization": f"Bearer {token}"},
        json={"mode": "paper"},
    )
    assert r.status_code in (401, 403), r.text
    assert "risk.trading_mode" not in store.system_settings
    assert [a for a in store.audit if a["action"] == "rearm_trading"] == []
