"""Polymarket CLOB L2 authentication.

Polymarket's CLOB API authenticates each request with five headers:
  POLY_ADDRESS    — the funder/proxy wallet address
  POLY_SIGNATURE  — base64(HMAC-SHA256(secret_b64, ts + method + path + body))
  POLY_TIMESTAMP  — current unix time in **seconds**
  POLY_API_KEY    — the L2 API key (UUID)
  POLY_PASSPHRASE — the L2 API key passphrase

`path` is the URL path portion only (e.g. `/order`), without scheme/host.
The body is included verbatim (the JSON string actually sent on the wire);
empty for GET/DELETE.

The raw L2 secret is base64url-encoded; we decode it before HMAC'ing. The
order placement endpoint additionally requires an EIP-712 L1 signature
inside the body — that lives outside this module because paper-mode never
needs it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any


class PolymarketAuthError(RuntimeError):
    pass


def _decode_secret(secret_b64url: str) -> bytes:
    if not secret_b64url:
        raise PolymarketAuthError("POLYMARKET_API_SECRET is empty")
    s = secret_b64url.strip()
    # Tolerate base64 vs base64url, padded or unpadded.
    padding = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + padding)
    except Exception:
        try:
            return base64.b64decode(s + padding)
        except Exception as e:
            raise PolymarketAuthError(f"failed to decode POLYMARKET_API_SECRET: {e}") from e


def sign_request(
    *,
    api_key: str,
    api_secret: str,
    passphrase: str,
    address: str,
    method: str,
    path: str,
    body: str = "",
    ts: int | None = None,
) -> dict[str, str]:
    """Return the five Polymarket L2 auth headers for a (method, path, body)."""
    if not api_key:
        raise PolymarketAuthError("POLYMARKET_API_KEY is empty")
    if not passphrase:
        raise PolymarketAuthError("POLYMARKET_API_PASSPHRASE is empty")
    if not address:
        raise PolymarketAuthError("POLYMARKET_WALLET_ADDRESS is empty")
    secret = _decode_secret(api_secret)
    if ts is None:
        ts = int(time.time())
    msg = f"{ts}{method.upper()}{path}{body or ''}".encode("utf-8")
    sig = hmac.new(secret, msg, hashlib.sha256).digest()
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": base64.urlsafe_b64encode(sig).decode("ascii"),
        "POLY_TIMESTAMP": str(ts),
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": passphrase,
    }


def validate_credentials(
    api_key: str | None,
    api_secret: str | None,
    passphrase: str | None,
    address: str | None,
) -> dict[str, Any]:
    """Cheap startup check: produce a sample signature. Never raises."""
    if not api_key or not api_secret or not passphrase or not address:
        return {
            "ok": False,
            "error": (
                "missing one of POLYMARKET_API_KEY / POLYMARKET_API_SECRET / "
                "POLYMARKET_API_PASSPHRASE / POLYMARKET_WALLET_ADDRESS"
            ),
        }
    try:
        sign_request(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            address=address,
            method="GET",
            path="/auth/api-keys",
            body="",
            ts=0,
        )
        fp = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        return {"ok": True, "key_fingerprint": fp}
    except Exception as e:
        return {"ok": False, "error": str(e)}
