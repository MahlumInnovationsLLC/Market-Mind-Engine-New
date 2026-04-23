"""Tests for the admin-only crypto-binary backtest endpoint.

Confirms `/api/admin/crypto/backtest`:

  * Rejects unauthenticated callers.
  * Rejects unsupported symbols / timeframes with 400.
  * Wraps `app.crypto.backtest.run_backtest` and returns the structured
    `BacktestResult.as_dict()` payload (Brier / log-loss / accuracy
    summary plus the reliability bin table) the operator dashboard
    renders.

Reuses the in-memory auth fixtures from `test_admin_risk_and_models`
where applicable; here we just need an admin user + a stub
`run_backtest` so the test does not touch Postgres or the predictors.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import admin as admin_module
from app import auth as auth_module
from app import db as db_module
from app import main as main_module
from app.crypto import backtest as crypto_backtest
from app.crypto.backtest import BacktestResult
from app.main import app
from tests.conftest import TEST_CLERK_HOST


ISSUER = f"https://{TEST_CLERK_HOST}"
ADMIN_UID = "user_admin_crypto_backtest"


# ---------------------------------------------------------------------------
# JWKS / JWT plumbing — mirrors test_admin_risk_and_models.
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
# Tiny app_users-only fake DB so admin auth resolves the caller.
# ---------------------------------------------------------------------------


class _FakeUsersConn:
    def __init__(self, users: dict[str, dict[str, Any]]) -> None:
        self.users = users

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "from app_users" in " ".join(query.split()).lower():
            return self.users.get(args[0])
        return None

    async def execute(self, *_a: Any, **_k: Any) -> str:
        return "OK"

    async def fetch(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []

    async def fetchval(self, *_a: Any, **_k: Any) -> Any:
        return None


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
        return _FakeResponse(404, {})

    async def patch(self, *_a: Any, **_k: Any) -> _FakeResponse:
        return _FakeResponse(200, {})


@pytest.fixture(autouse=True)
def _wire_env(monkeypatch: pytest.MonkeyPatch, _keypair):
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
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_admin_crypto_backtest")
    monkeypatch.delenv("OPERATOR_ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_ALLOWED_EMAILS", raising=False)

    users: dict[str, dict[str, Any]] = {
        ADMIN_UID: {
            "clerk_user_id": ADMIN_UID,
            "email": "admin@example.com",
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
    }

    @asynccontextmanager
    async def fake_acquire():
        yield _FakeUsersConn(users)

    monkeypatch.setattr(db_module, "acquire", fake_acquire)
    monkeypatch.setattr(admin_module.db, "acquire", fake_acquire)

    yield

    auth_module._user_cache.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_crypto_backtest_requires_auth(client: TestClient) -> None:
    r = client.get("/api/admin/crypto/backtest")
    assert r.status_code in (401, 403), r.text


def test_crypto_backtest_rejects_unsupported_symbol(
    client: TestClient, _keypair
) -> None:
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/crypto/backtest?symbol=DOGE&timeframe=1h&days=7",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, r.text
    assert "DOGE" in r.json()["detail"]


def test_crypto_backtest_rejects_unsupported_timeframe(
    client: TestClient, _keypair
) -> None:
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/crypto/backtest?symbol=BTC&timeframe=4h&days=7",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, r.text
    assert "4h" in r.json()["detail"]


def test_crypto_backtest_returns_result_payload(
    client: TestClient, _keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: stub `run_backtest` so we don't need real OHLCV
    rows, and assert the endpoint forwards the symbol/timeframe/days
    args and returns the structured `BacktestResult.as_dict()`
    payload the operator dashboard consumes (summary metrics plus
    the reliability bin table).
    """
    captured: dict[str, Any] = {}

    async def fake_run_backtest(
        *,
        symbol: str,
        timeframe: str,
        days: int,
        lookback: int,
        model: Any | None = None,
    ) -> BacktestResult:
        captured["symbol"] = symbol
        captured["timeframe"] = timeframe
        captured["days"] = days
        captured["lookback"] = lookback
        return BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            n_evaluated=120,
            accuracy=0.625,
            brier_score=0.2125,
            log_loss=0.612345,
            reliability=[
                (0.0, 0.1, 0.05, 0.10, 12),
                (0.4, 0.5, 0.45, 0.42, 33),
                (0.9, 1.0, 0.95, 0.88, 9),
            ],
            started_at="2026-04-23T00:00:00+00:00",
            completed_at="2026-04-23T00:00:05+00:00",
        )

    # The endpoint imports the module as `crypto_backtest` and calls
    # `crypto_backtest.run_backtest(...)`, so patch it on both the
    # module and the alias on `app.main` for safety.
    monkeypatch.setattr(crypto_backtest, "run_backtest", fake_run_backtest)
    monkeypatch.setattr(
        main_module.crypto_backtest, "run_backtest", fake_run_backtest
    )

    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/crypto/backtest?symbol=eth&timeframe=15m&days=21&lookback=60",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Args were normalised (uppercased symbol, lowercased timeframe)
    # and forwarded to run_backtest.
    assert captured == {
        "symbol": "ETH",
        "timeframe": "15m",
        "days": 21,
        "lookback": 60,
    }

    # Summary metrics surfaced exactly as `as_dict` shapes them.
    assert body["symbol"] == "ETH"
    assert body["timeframe"] == "15m"
    assert body["n_evaluated"] == 120
    assert body["accuracy"] == 0.625
    assert body["brier_score"] == 0.2125
    assert body["log_loss"] == 0.612345

    # Reliability bins flattened into the dashboard-friendly object form.
    assert isinstance(body["reliability"], list)
    assert len(body["reliability"]) == 3
    first = body["reliability"][0]
    assert first["bin_lower"] == 0.0
    assert first["bin_upper"] == 0.1
    assert first["mean_predicted"] == 0.05
    assert first["mean_actual"] == 0.10
    assert first["count"] == 12


def test_crypto_backtest_days_validated(
    client: TestClient, _keypair
) -> None:
    """FastAPI's Query(ge=1, le=90) clamp must reject out-of-range
    `days` so the operator can't ask for an absurd window."""
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/crypto/backtest?symbol=BTC&timeframe=1h&days=500",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422, r.text
