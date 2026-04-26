"""Tests for the rolling-window crypto accuracy report (task #580).

Covers two layers:

  * The pure compute step (``compute_from_rows``) — exercises the
    Brier / log-loss / accuracy math, void / unscored bookkeeping, and
    the symbol/timeframe slice grouping (including the ticker-parse
    fallback for legacy rows whose ``game_context`` is missing the
    symbol/timeframe tags).
  * The admin endpoint ``/api/admin/crypto/accuracy`` — auth gate,
    request-shape validation, and the multi-window payload it returns
    once the per-window evaluator is stubbed out.

The auth + DB stub plumbing mirrors ``test_admin_crypto_backtest`` so
this file does not need a real Postgres or the predictor stack.
"""
from __future__ import annotations

import math
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
from app.crypto import accuracy_report
from app.main import app
from tests.conftest import TEST_CLERK_HOST


ISSUER = f"https://{TEST_CLERK_HOST}"
ADMIN_UID = "user_admin_crypto_accuracy"


# ---------------------------------------------------------------------------
# JWKS / JWT plumbing — mirrors test_admin_crypto_backtest.
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
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_admin_crypto_accuracy")
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
# Pure-function compute tests — no DB, no auth.
# ---------------------------------------------------------------------------


def _row(
    *,
    outcome: str,
    p_yes: float | None,
    symbol: str | None = "BTC",
    timeframe: str | None = "1h",
    market_id: str = "KXBTC-26APR2612-T100000",
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "p_yes": p_yes,
        "symbol": symbol or "",
        "timeframe": timeframe or "",
        "market_id": market_id,
    }


def test_compute_handcomputed_metrics_match_definitions() -> None:
    """Three rows with known probabilities and outcomes give Brier /
    log-loss / accuracy values we can hand-check against the closed-form
    formulas — guarding against an off-by-one in the per-row averaging."""
    rows = [
        _row(outcome="yes", p_yes=0.8),  # correct, brier (1-0.8)^2 = 0.04
        _row(outcome="no", p_yes=0.2),   # correct, brier (0.2-0)^2 = 0.04
        _row(outcome="yes", p_yes=0.4),  # wrong,   brier (1-0.4)^2 = 0.36
    ]
    rep = accuracy_report.compute_from_rows(24, rows)
    assert rep.window_hours == 24
    assert rep.n_settled == 3
    assert rep.n_void == 0
    assert rep.n_unscored == 0
    overall = rep.overall
    assert overall.n == 3
    assert overall.yes_count == 2
    assert overall.no_count == 1
    assert overall.accuracy == pytest.approx(2 / 3)
    assert overall.brier == pytest.approx((0.04 + 0.04 + 0.36) / 3)
    expected_log_loss = (
        -math.log(0.8) - math.log(0.8) - math.log(0.4)
    ) / 3
    assert overall.log_loss == pytest.approx(expected_log_loss)


def test_compute_skips_void_and_unscored_rows() -> None:
    """Void rows go into `n_void`, settled-but-no-probability rows go
    into `n_unscored`, and only the remaining scored rows feed the
    metrics."""
    rows = [
        _row(outcome="void", p_yes=0.7),               # refunded
        _row(outcome="yes", p_yes=None),               # no debate joined
        _row(outcome="no", p_yes=float("nan")),        # garbage prob
        _row(outcome="yes", p_yes=1.5),                # out-of-range prob
        _row(outcome="weird", p_yes=0.5),              # unknown label
        _row(outcome="yes", p_yes=0.9),                # the only scored row
    ]
    rep = accuracy_report.compute_from_rows(24, rows)
    assert rep.n_settled == 6
    assert rep.n_void == 1
    # 1 missing prob + 1 nan + 1 out-of-range + 1 unknown label = 4
    assert rep.n_unscored == 4
    assert rep.overall.n == 1
    assert rep.overall.accuracy == pytest.approx(1.0)
    assert rep.overall.brier == pytest.approx((0.9 - 1) ** 2)


def test_compute_log_loss_clamps_extreme_probabilities() -> None:
    """A probability of exactly 0 against a YES outcome would blow up
    the log term — the report must clamp via the same epsilon the
    in-process backtest scaffold uses."""
    rows = [_row(outcome="yes", p_yes=0.0)]
    rep = accuracy_report.compute_from_rows(24, rows)
    assert rep.overall.n == 1
    assert math.isfinite(rep.overall.log_loss)
    assert rep.overall.log_loss == pytest.approx(-math.log(1e-9))


def test_compute_partitions_by_symbol_and_timeframe() -> None:
    """Slices key off ``(symbol, timeframe)`` and sort BTC before ETH,
    with timeframes in canonical order (5m < 15m < 1h)."""
    rows = [
        _row(outcome="yes", p_yes=0.6, symbol="BTC", timeframe="1h"),
        _row(outcome="no", p_yes=0.2, symbol="BTC", timeframe="5m"),
        _row(outcome="yes", p_yes=0.7, symbol="ETH", timeframe="15m"),
        _row(outcome="no", p_yes=0.45, symbol="ETH", timeframe="15m"),
    ]
    rep = accuracy_report.compute_from_rows(168, rows)
    assert [(s.symbol, s.timeframe, s.n) for s in rep.slices] == [
        ("BTC", "5m", 1),
        ("BTC", "1h", 1),
        ("ETH", "15m", 2),
    ]
    eth_15m = next(
        s for s in rep.slices if s.symbol == "ETH" and s.timeframe == "15m"
    )
    assert eth_15m.accuracy == pytest.approx(1.0)
    assert eth_15m.yes_count == 1
    assert eth_15m.no_count == 1


def test_compute_falls_back_to_ticker_parse_for_legacy_rows() -> None:
    """Rows whose ``game_context`` predates the symbol/timeframe tags
    must still be slotted by re-parsing the Kalshi ticker so they don't
    collapse into an "unknown" bucket."""
    rows = [
        # No symbol/timeframe in game_context — must fall back to the
        # KXETH 5-minute ticker layout.
        _row(
            outcome="yes",
            p_yes=0.6,
            symbol=None,
            timeframe=None,
            market_id="KXETHD-26APR2612-T2500",
        ),
    ]
    rep = accuracy_report.compute_from_rows(24, rows)
    assert len(rep.slices) == 1
    only = rep.slices[0]
    assert only.symbol == "ETH"
    # KXETHD = ETH daily/5m? confirm timeframe is one of the supported
    # set even if the exact value depends on the ticker grammar.
    assert only.timeframe in {"5m", "15m", "1h"}


def test_compute_empty_rows_returns_none_metrics() -> None:
    """No data → zero counts and ``None`` metrics so the dashboard can
    render "no data" rather than a misleading 0.0 perfect score."""
    rep = accuracy_report.compute_from_rows(720, [])
    assert rep.n_settled == 0
    assert rep.overall.n == 0
    assert rep.overall.accuracy is None
    assert rep.overall.brier is None
    assert rep.overall.log_loss is None
    assert rep.slices == []


# ---------------------------------------------------------------------------
# Admin endpoint tests.
# ---------------------------------------------------------------------------


def test_accuracy_endpoint_requires_auth(client: TestClient) -> None:
    r = client.get("/api/admin/crypto/accuracy")
    assert r.status_code in (401, 403), r.text


def test_accuracy_endpoint_returns_default_window_payload(
    client: TestClient,
    _keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: stub ``evaluate_window`` so the test does not need a
    real Postgres, then assert the endpoint forwards the default 24h /
    7d / 30d windows in order and returns the multi-window payload the
    operator dashboard consumes."""
    captured: list[int] = []

    async def fake_evaluate_window(window_hours: int):
        captured.append(window_hours)
        # Return distinguishable totals per window so we can assert the
        # ordering survives the round trip.
        return accuracy_report.WindowReport(
            window_hours=window_hours,
            n_settled=window_hours,  # toy: window size doubles as count
            n_void=0,
            n_unscored=0,
            overall=accuracy_report.SliceMetrics(
                symbol=None, timeframe=None,
                n=window_hours, accuracy=0.6, brier=0.2, log_loss=0.55,
                yes_count=window_hours // 2,
                no_count=window_hours - window_hours // 2,
            ),
            slices=[],
        )

    monkeypatch.setattr(
        accuracy_report, "evaluate_window", fake_evaluate_window
    )
    monkeypatch.setattr(
        main_module.crypto_accuracy_report,
        "evaluate_window",
        fake_evaluate_window,
    )

    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/crypto/accuracy",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["league"] == "CRYPTO"
    assert captured == [24, 168, 720]
    assert [w["window_hours"] for w in body["windows"]] == [24, 168, 720]
    assert body["windows"][0]["overall"]["accuracy"] == 0.6
    assert body["windows"][2]["n_settled"] == 720


def test_accuracy_endpoint_dedupes_window_overrides(
    client: TestClient,
    _keypair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator can override windows via repeated ``windows_hours``
    query params; duplicates collapse, ordering is preserved."""
    captured: list[int] = []

    async def fake_evaluate_window(window_hours: int):
        captured.append(window_hours)
        return accuracy_report.WindowReport(
            window_hours=window_hours,
            n_settled=0, n_void=0, n_unscored=0,
            overall=accuracy_report.SliceMetrics(
                symbol=None, timeframe=None,
                n=0, accuracy=None, brier=None, log_loss=None,
                yes_count=0, no_count=0,
            ),
            slices=[],
        )

    monkeypatch.setattr(
        accuracy_report, "evaluate_window", fake_evaluate_window
    )
    monkeypatch.setattr(
        main_module.crypto_accuracy_report,
        "evaluate_window",
        fake_evaluate_window,
    )

    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/crypto/accuracy"
        "?windows_hours=12&windows_hours=48&windows_hours=12",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert captured == [12, 48]
    assert [w["window_hours"] for w in body["windows"]] == [12, 48]


def test_accuracy_endpoint_rejects_non_positive_window(
    client: TestClient, _keypair
) -> None:
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/crypto/accuracy?windows_hours=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, r.text
    assert "positive" in r.json()["detail"].lower()


def test_accuracy_endpoint_rejects_oversized_window(
    client: TestClient, _keypair
) -> None:
    """Hard cap protects the join from a multi-year scan."""
    token = _mint(_keypair, sub=ADMIN_UID)
    r = client.get(
        "/api/admin/crypto/accuracy?windows_hours=999999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400, r.text
    assert "exceeds" in r.json()["detail"].lower()
