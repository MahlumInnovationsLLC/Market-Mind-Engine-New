"""Kalshi RSA-PSS request signing.

Kalshi authenticates each request with three headers:
  KALSHI-ACCESS-KEY        — the API key id (UUID)
  KALSHI-ACCESS-TIMESTAMP  — current unix time in **milliseconds**
  KALSHI-ACCESS-SIGNATURE  — base64(RSA-PSS-SHA256(timestamp_ms + method + path))

`path` is the URL path portion only (e.g. `/trade-api/v2/portfolio/balance`),
without scheme/host/query.

We load the PEM private key once and cache it in-process.  Loading is
deferred so the process can boot when keys are absent (degraded mode).
"""
from __future__ import annotations

import base64
import time
from functools import lru_cache
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiAuthError(RuntimeError):
    pass


@lru_cache(maxsize=4)
def _load_private_key(pem: str) -> rsa.RSAPrivateKey:
    if not pem:
        raise KalshiAuthError("KALSHI_API_PRIVATE_KEY is empty")
    pem_bytes = pem.encode("utf-8") if isinstance(pem, str) else pem
    # Tolerate users pasting PEM with literal "\n" sequences instead of newlines.
    if b"\\n" in pem_bytes and b"-----BEGIN" in pem_bytes:
        pem_bytes = pem_bytes.replace(b"\\n", b"\n")
    try:
        key = serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as e:  # pragma: no cover - defensive
        raise KalshiAuthError(f"failed to parse Kalshi private key: {e}") from e
    if not isinstance(key, rsa.RSAPrivateKey):
        raise KalshiAuthError("Kalshi private key is not an RSA key")
    return key


def sign_request(
    *, key_id: str, private_key_pem: str, method: str, path: str, ts_ms: int | None = None
) -> dict[str, str]:
    """Return the three Kalshi auth headers for a given (method, path).

    The `path` MUST include the `/trade-api/v2` prefix and any sub-path,
    but MUST NOT include the scheme/host or the query string.
    """
    if not key_id:
        raise KalshiAuthError("KALSHI_API_KEY_ID is empty")
    key = _load_private_key(private_key_pem)
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    msg = f"{ts_ms}{method.upper()}{path}".encode("utf-8")
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("ascii"),
        "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
    }


def validate_credentials(key_id: str | None, private_key_pem: str | None) -> dict[str, Any]:
    """Cheap startup check: parse the key and produce a sample signature.

    Returns a dict with `ok` and either `error` or `key_fingerprint`.
    Never raises.
    """
    if not key_id or not private_key_pem:
        return {"ok": False, "error": "missing KALSHI_API_KEY_ID or KALSHI_API_PRIVATE_KEY"}
    try:
        key = _load_private_key(private_key_pem)
        # produce a dummy signature so we know signing actually works
        sign_request(
            key_id=key_id,
            private_key_pem=private_key_pem,
            method="GET",
            path="/trade-api/v2/exchange/status",
            ts_ms=0,
        )
        pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        import hashlib
        fp = hashlib.sha256(pub).hexdigest()[:16]
        return {"ok": True, "key_fingerprint": fp}
    except Exception as e:
        return {"ok": False, "error": str(e)}
