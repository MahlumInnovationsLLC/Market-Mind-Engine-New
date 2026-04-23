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


# ---------------------------------------------------------------------------
# cluster_probe — auto-detect wrong KALSHI_ENV at boot
# ---------------------------------------------------------------------------


def _patch_cluster_probe(monkeypatch, rsa_key, *, kalshi_env: str, responses: dict[str, httpx.Response]):
    """Wire ``cluster_probe`` to a fake settings + a per-base-URL response map.

    ``responses`` is keyed by the base URL the probe should call (one of
    ``KALSHI_PROD_BASE`` / ``KALSHI_DEMO_BASE``). Any URL not in the map
    raises so we catch unintended calls.
    """
    _, pem = rsa_key
    base = (
        kalshi_client.KALSHI_PROD_BASE
        if kalshi_env == "prod"
        else kalshi_client.KALSHI_DEMO_BASE
    )
    fake = SimpleNamespace(
        kalshi_api_key_id=KEY_ID,
        kalshi_private_key=pem,
        kalshi_api_base_url=base,
        kalshi_env=kalshi_env,
    )
    monkeypatch.setattr(kalshi_client, "get_settings", lambda: fake)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        # strip the trailing /portfolio/balance to match map keys
        assert url.endswith("/portfolio/balance"), url
        base_key = url[: -len("/portfolio/balance")]
        seen.append(base_key)
        if base_key not in responses:
            raise httpx.ConnectError(f"unexpected base url {base_key}")
        return responses[base_key]

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(kalshi_client.httpx, "AsyncClient", patched_client)
    # Reset cached state between tests.
    kalshi_client._cluster_probe_state.clear()
    kalshi_client._cluster_probe_state["checked"] = False
    return seen


def test_cluster_probe_ok_against_configured(monkeypatch, rsa_key):
    seen = _patch_cluster_probe(
        monkeypatch,
        rsa_key,
        kalshi_env="prod",
        responses={
            kalshi_client.KALSHI_PROD_BASE: httpx.Response(200, json={"balance": 1}),
        },
    )
    out = _run(kalshi_client.cluster_probe())
    assert out["ok"] is True
    assert out["configured_env"] == "prod"
    assert out["key_belongs_to"] == "prod"
    # Only the configured cluster should be hit.
    assert seen == [kalshi_client.KALSHI_PROD_BASE]
    state = kalshi_client.cluster_probe_state()
    assert state["ok"] is True
    assert state["checked"] is True


def test_cluster_probe_detects_wrong_env(monkeypatch, rsa_key):
    """Configured=demo but key authenticates against prod -> loud error
    that names the right cluster."""
    seen = _patch_cluster_probe(
        monkeypatch,
        rsa_key,
        kalshi_env="demo",
        responses={
            kalshi_client.KALSHI_DEMO_BASE: httpx.Response(401, json={"error": "unauthorized"}),
            kalshi_client.KALSHI_PROD_BASE: httpx.Response(200, json={"balance": 0}),
        },
    )
    out = _run(kalshi_client.cluster_probe())
    assert out["ok"] is False
    assert out["configured_env"] == "demo"
    assert out["key_belongs_to"] == "prod"
    assert "KALSHI_ENV=prod" in out["error"]
    # Both clusters were probed.
    assert kalshi_client.KALSHI_DEMO_BASE in seen
    assert kalshi_client.KALSHI_PROD_BASE in seen


def test_cluster_probe_both_clusters_reject(monkeypatch, rsa_key):
    """If both clusters reject the key, we don't speculate about ownership."""
    _patch_cluster_probe(
        monkeypatch,
        rsa_key,
        kalshi_env="prod",
        responses={
            kalshi_client.KALSHI_PROD_BASE: httpx.Response(401, json={"error": "unauthorized"}),
            kalshi_client.KALSHI_DEMO_BASE: httpx.Response(401, json={"error": "unauthorized"}),
        },
    )
    out = _run(kalshi_client.cluster_probe())
    assert out["ok"] is False
    assert out["key_belongs_to"] is None
    assert "Check KALSHI_API_KEY_ID" in out["error"]


def test_cluster_probe_other_status_does_not_mislabel(monkeypatch, rsa_key):
    """A 5xx from the configured cluster is not a wrong-env signal —
    don't cross-probe and don't claim the key belongs elsewhere."""
    seen = _patch_cluster_probe(
        monkeypatch,
        rsa_key,
        kalshi_env="prod",
        responses={
            kalshi_client.KALSHI_PROD_BASE: httpx.Response(503, text="upstream down"),
        },
    )
    out = _run(kalshi_client.cluster_probe())
    assert out["ok"] is False
    assert out["key_belongs_to"] is None
    assert out["status"] == 503
    # Did NOT probe the other cluster.
    assert seen == [kalshi_client.KALSHI_PROD_BASE]


def test_cluster_probe_missing_credentials(monkeypatch):
    fake = SimpleNamespace(
        kalshi_api_key_id=None,
        kalshi_private_key=None,
        kalshi_api_base_url=BASE_URL,
        kalshi_env="prod",
    )
    monkeypatch.setattr(kalshi_client, "get_settings", lambda: fake)
    kalshi_client._cluster_probe_state.clear()
    out = _run(kalshi_client.cluster_probe())
    assert out["ok"] is False
    assert "missing" in out["error"].lower()
    assert kalshi_client.cluster_probe_state()["checked"] is True

# ---------------------------------------------------------------------------
# get_market_candlesticks
# ---------------------------------------------------------------------------


def test_get_market_candlesticks_signs_and_paths(settings, rsa_key):
    key, _ = rsa_key
    ticker = "KXNFL-25NOV23DETHGB-DET"
    expected_path = (
        "/trade-api/v2/series/KXNFL/markets/KXNFL-25NOV23DETHGB-DET/candlesticks"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == expected_path
        assert request.url.params.get("start_ts") == "100"
        assert request.url.params.get("end_ts") == "200"
        assert request.url.params.get("period_interval") == "1"
        _verify_auth_headers(request, key.public_key(), "GET", expected_path)
        return httpx.Response(
            200,
            json={
                "candlesticks": [
                    {
                        "end_period_ts": 150,
                        "price": {
                            "open": 50, "high": 55, "low": 45,
                            "close": 52, "mean": 51, "previous": 49,
                        },
                        "volume": 10,
                        "open_interest": 100,
                    }
                ],
                "ticker": ticker,
            },
        )

    c = _make_client(settings, handler)
    out = _run(
        c.get_market_candlesticks(
            ticker, start_ts=100, end_ts=200, period_interval=1
        )
    )
    _run(c.aclose())
    assert out["ticker"] == ticker
    assert len(out["candlesticks"]) == 1
    assert out["candlesticks"][0]["price"]["mean"] == 51


def test_get_market_candlesticks_rejects_ticker_without_series(settings):
    c = _make_client(settings, lambda _r: httpx.Response(200, json={}))
    with pytest.raises(kalshi_client.KalshiAPIError) as exc:
        _run(c.get_market_candlesticks("BADTICKER", start_ts=1, end_ts=2))
    _run(c.aclose())
    assert "series ticker" in str(exc.value)
