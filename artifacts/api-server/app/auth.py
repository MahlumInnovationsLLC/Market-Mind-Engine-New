"""Clerk session JWT verification for the operator console API.

Every protected endpoint depends on `require_auth`, which:
  * Requires an `Authorization: Bearer <token>` header
  * Verifies the JWT signature against Clerk's JWKS
  * Verifies the issuer matches the Clerk instance derived from
    `CLERK_PUBLISHABLE_KEY`
  * Returns an `AuthedUser` with the Clerk user id (`sub` claim)

There is no fallback path — no shared bearer token, no same-origin
shortcut, no referer check.  Either the request carries a valid Clerk
session JWT or it gets a 401.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

log = logging.getLogger("axiom.auth")


# ---------------------------------------------------------------------------
# Clerk instance discovery
# ---------------------------------------------------------------------------


def _decode_publishable_key(pk: str) -> str:
    """Return the Clerk frontend-API host encoded inside a publishable key.

    A Clerk publishable key looks like `pk_test_<base64>` or `pk_live_<base64>`.
    The base64 payload decodes to e.g. `clerk.busy-rabbit-12.lcl.dev$`.
    """
    parts = pk.split("_", 2)
    body = parts[-1] if len(parts) >= 2 else pk
    padded = body + "=" * (-len(body) % 4)
    try:
        raw = base64.b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    return raw.rstrip("$").strip()


@lru_cache(maxsize=1)
def _frontend_api_host() -> str:
    pk = (
        os.environ.get("CLERK_PUBLISHABLE_KEY")
        or os.environ.get("VITE_CLERK_PUBLISHABLE_KEY")
        or ""
    ).strip()
    if not pk:
        return ""
    return _decode_publishable_key(pk)


@lru_cache(maxsize=1)
def _issuer() -> str:
    host = _frontend_api_host()
    return f"https://{host}" if host else ""


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient | None:
    host = _frontend_api_host()
    if not host:
        return None
    return PyJWKClient(
        f"https://{host}/.well-known/jwks.json",
        cache_keys=True,
        lifespan=600,
    )


def auth_configured() -> bool:
    return bool(_frontend_api_host())


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthedUser:
    user_id: str
    session_id: str | None
    claims: dict[str, Any]


async def require_auth(
    authorization: str | None = Header(default=None),
) -> AuthedUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")

    jwks = _jwks_client()
    issuer = _issuer()
    if jwks is None or not issuer:
        # Misconfigured server — refuse rather than letting traffic through.
        raise HTTPException(status_code=503, detail="auth not configured")

    try:
        signing_key = jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            issuer=issuer,
            options={"verify_aud": False},
            leeway=10,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="invalid token issuer")
    except jwt.PyJWTError as e:
        log.warning("jwt verify failed: %s", e)
        raise HTTPException(status_code=401, detail="invalid token")
    except Exception as e:  # JWKS fetch failure, etc.
        log.error("auth verification error: %s", e)
        raise HTTPException(status_code=401, detail="invalid token")

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="token missing subject")

    return AuthedUser(
        user_id=str(user_id),
        session_id=claims.get("sid"),
        claims=claims,
    )
