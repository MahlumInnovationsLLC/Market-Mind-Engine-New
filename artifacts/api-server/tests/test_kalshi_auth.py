"""Unit tests for ``app.kalshi.auth``.

Covers:
  * ``sign_request`` produces the three required headers and a valid
    RSA-PSS signature over ``timestamp + METHOD + path``.
  * ``sign_request`` honours an explicit ``ts_ms`` and rejects empty key id.
  * ``_load_private_key`` parses real PEM, tolerates literal ``\\n``
    sequences, and raises ``KalshiAuthError`` on garbage input.
  * ``validate_credentials`` returns ``ok`` for a real key, ``ok=False``
    for missing / unparseable inputs, and never raises.
"""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.kalshi import auth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gen_pem() -> tuple[rsa.RSAPrivateKey, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return key, pem


@pytest.fixture(scope="module")
def rsa_key() -> tuple[rsa.RSAPrivateKey, str]:
    auth._load_private_key.cache_clear()
    return _gen_pem()


# ---------------------------------------------------------------------------
# sign_request
# ---------------------------------------------------------------------------

def test_sign_request_emits_three_headers_and_verifies(rsa_key):
    key, pem = rsa_key
    headers = auth.sign_request(
        key_id="kid-123",
        private_key_pem=pem,
        method="GET",
        path="/trade-api/v2/portfolio/balance",
        ts_ms=1_700_000_000_000,
    )
    assert headers["KALSHI-ACCESS-KEY"] == "kid-123"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    # The signed payload is ``timestamp_ms + METHOD + path``; verify with
    # the matching public key to prove the algorithm matches Kalshi's spec.
    msg = b"1700000000000GET/trade-api/v2/portfolio/balance"
    key.public_key().verify(
        sig,
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_sign_request_uppercases_method(rsa_key):
    key, pem = rsa_key
    headers = auth.sign_request(
        key_id="kid",
        private_key_pem=pem,
        method="post",  # lowercase on purpose
        path="/trade-api/v2/portfolio/orders",
        ts_ms=42,
    )
    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(
        sig,
        b"42POST/trade-api/v2/portfolio/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_sign_request_default_timestamp_is_unix_ms(monkeypatch, rsa_key):
    _, pem = rsa_key
    monkeypatch.setattr(auth.time, "time", lambda: 12345.678)
    headers = auth.sign_request(
        key_id="kid",
        private_key_pem=pem,
        method="GET",
        path="/x",
    )
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == str(int(12345.678 * 1000))


def test_sign_request_rejects_empty_key_id(rsa_key):
    _, pem = rsa_key
    with pytest.raises(auth.KalshiAuthError):
        auth.sign_request(key_id="", private_key_pem=pem, method="GET", path="/x")


# ---------------------------------------------------------------------------
# _load_private_key
# ---------------------------------------------------------------------------

def test_load_private_key_rejects_empty():
    auth._load_private_key.cache_clear()
    with pytest.raises(auth.KalshiAuthError):
        auth._load_private_key("")


def test_load_private_key_rejects_garbage():
    auth._load_private_key.cache_clear()
    with pytest.raises(auth.KalshiAuthError):
        auth._load_private_key("-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n")


def test_load_private_key_accepts_literal_backslash_n(rsa_key):
    """Users who paste their PEM into a single-line Replit secret often end
    up with literal ``\\n`` instead of real newlines; the loader normalises."""
    _, pem = rsa_key
    mangled = pem.replace("\n", "\\n")
    auth._load_private_key.cache_clear()
    key = auth._load_private_key(mangled)
    assert isinstance(key, rsa.RSAPrivateKey)


# ---------------------------------------------------------------------------
# validate_credentials
# ---------------------------------------------------------------------------

def test_validate_credentials_missing_inputs():
    assert auth.validate_credentials(None, None)["ok"] is False
    assert auth.validate_credentials("kid", None)["ok"] is False
    assert auth.validate_credentials(None, "pem")["ok"] is False


def test_validate_credentials_happy_path(rsa_key):
    _, pem = rsa_key
    auth._load_private_key.cache_clear()
    out = auth.validate_credentials("kid", pem)
    assert out["ok"] is True
    assert isinstance(out["key_fingerprint"], str)
    assert len(out["key_fingerprint"]) == 16


def test_validate_credentials_swallows_bad_pem():
    out = auth.validate_credentials("kid", "not a pem")
    assert out["ok"] is False
    assert "error" in out
