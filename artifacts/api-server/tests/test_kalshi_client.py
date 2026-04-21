"""Mocked-httpx integration tests for ``app.kalshi.client.KalshiClient``.

We construct a real ``KalshiClient`` but swap its underlying ``httpx.AsyncClient``
for one wired to ``httpx.MockTransport``. Each handler asserts that:
  * the request URL is built off the configured base URL,
  * Kalshi's three auth headers are present and verifiable,
  * the signed path includes the ``/trade-api/v2`` prefix.

Then we verify the parsed dataclasses for ``get_balance`` / ``get_positions``
and the raw round-trip behaviour for ``submit_order``, including the 4xx
rejection path.
"""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.kalshi import auth, client as kalshi_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
KEY_ID = "test-key-id"


def _gen_pem() -> tuple[rsa.RSAPrivateKey, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return key, pem


@pytest.fixture(scope="module")
def rsa_key():
    auth._load_private_key.cache_clear()
    return _gen_pem()


@pytest.fixture
def settings(rsa_key):
    _, pem = rsa_key
    # Settings is a frozen dataclass with many required fields. The client
    # only reads three attributes, so a SimpleNamespace duck is enough.
    return SimpleNamespace(
        kalshi_api_key_id=KEY_ID,
        kalshi_private_key=pem,
        kalshi_api_base_url=BASE_URL,
    )


def _verify_auth_headers(request: httpx.Request, public_key, expected_method: str, expected_path: str) -> None:
    assert request.headers.get("KALSHI-ACCESS-KEY") == KEY_ID
    ts = request.headers.get("KALSHI-ACCESS-TIMESTAMP")
    sig_b64 = request.headers.get("KALSHI-ACCESS-SIGNATURE")
    assert ts and sig_b64, "missing auth headers"
    sig = base64.b64decode(sig_b64)
    msg = f"{ts}{expected_method.upper()}{expected_path}".encode()
    public_key.verify(
        sig,
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def _make_client(settings, handler):
    """Build a KalshiClient whose internal AsyncClient routes to ``handler``.

    The constructor's real ``httpx.AsyncClient`` is replaced synchronously
    (no awaiting close) so the mock transport is the only thing that ever
    gets awaited.
    """
    c = kalshi_client.KalshiClient(settings=settings)
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return c


def _run(coro):
    # Each call gets a fresh event loop. The shared default loop can be
    # closed by earlier async tests in the suite, so don't rely on it.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------

def test_client_init_requires_credentials():
    bad = SimpleNamespace(
        kalshi_api_key_id=None,
        kalshi_private_key=None,
        kalshi_api_base_url=BASE_URL,
    )
    with pytest.raises(auth.KalshiAuthError):
        kalshi_client.KalshiClient(settings=bad)


# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------

def test_get_balance_parses_dollars(settings, rsa_key):
    key, _ = rsa_key

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == f"{BASE_URL}/portfolio/balance"
        _verify_auth_headers(request, key.public_key(), "GET", "/trade-api/v2/portfolio/balance")
        return httpx.Response(200, json={"balance": 1234, "payout": 50})

    c = _make_client(settings, handler)
    bal = _run(c.get_balance())
    _run(c.aclose())
    assert bal.balance_cents == 1234
    assert bal.payout_cents == 50
    assert bal.balance_dollars == pytest.approx(12.34)


def test_get_balance_handles_missing_fields(settings, rsa_key):
    def handler(_req):
        return httpx.Response(200, json={})
    c = _make_client(settings, handler)
    bal = _run(c.get_balance())
    _run(c.aclose())
    assert bal.balance_cents == 0
    assert bal.payout_cents == 0


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------

def test_get_positions_parses_rows(settings, rsa_key):
    key, _ = rsa_key

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/trade-api/v2/portfolio/positions"
        assert request.url.params.get("limit") == "100"
        _verify_auth_headers(request, key.public_key(), "GET", "/trade-api/v2/portfolio/positions")
        return httpx.Response(200, json={
            "market_positions": [
                {"ticker": "X-1", "position": 5, "market_exposure": 250,
                 "realized_pnl": 10, "fees_paid": 1},
                {"market_ticker": "X-2", "position": -3, "market_exposure": 90,
                 "realized_pnl": -5, "fees_paid": 2},
            ]
        })

    c = _make_client(settings, handler)
    positions = _run(c.get_positions())
    _run(c.aclose())
    assert len(positions) == 2
    assert positions[0].market_ticker == "X-1"
    assert positions[0].position == 5
    assert positions[0].market_exposure_cents == 250
    assert positions[1].market_ticker == "X-2"
    assert positions[1].position == -3


def test_get_positions_empty_payload(settings):
    c = _make_client(settings, lambda _r: httpx.Response(200, json={"market_positions": []}))
    positions = _run(c.get_positions())
    _run(c.aclose())
    assert positions == []


# ---------------------------------------------------------------------------
# submit_order
# ---------------------------------------------------------------------------

def test_submit_order_signs_post_and_returns_body(settings, rsa_key):
    key, _ = rsa_key
    payload = {
        "ticker": "X-1", "action": "buy", "side": "yes",
        "type": "limit", "count": 1, "yes_price": 55,
        "client_order_id": "coid-1", "time_in_force": "GTC",
    }

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/trade-api/v2/portfolio/orders"
        _verify_auth_headers(request, key.public_key(), "POST", "/trade-api/v2/portfolio/orders")
        seen["body"] = request.content
        return httpx.Response(200, json={"order": {"order_id": "exo-1", **payload}})

    c = _make_client(settings, handler)
    ack = _run(c.submit_order(payload))
    _run(c.aclose())
    assert ack["order"]["order_id"] == "exo-1"
    import json as _json
    body = _json.loads(seen["body"].decode())
    assert body["client_order_id"] == "coid-1"
    assert body["yes_price"] == 55


def test_submit_order_raises_on_4xx(settings):
    def handler(_req):
        return httpx.Response(409, json={"error": "duplicate client_order_id"})

    c = _make_client(settings, handler)
    with pytest.raises(kalshi_client.KalshiAPIError) as exc:
        _run(c.submit_order({"ticker": "X-1"}))
    _run(c.aclose())
    assert exc.value.status == 409
    assert exc.value.body == {"error": "duplicate client_order_id"}


def test_request_wraps_network_errors(settings):
    def handler(_req):
        raise httpx.ConnectError("boom")

    c = _make_client(settings, handler)
    with pytest.raises(kalshi_client.KalshiAPIError) as exc:
        _run(c.exchange_status())
    _run(c.aclose())
    assert exc.value.status == 0
    assert "network error" in str(exc.value)
