"""Clerk session JWT verification + operator approval gate.

Every protected endpoint depends on `require_auth`, which:
  * Requires an `Authorization: Bearer <token>` header
  * Verifies the JWT signature against Clerk's JWKS
  * Verifies the issuer matches the Clerk instance derived from
    `CLERK_PUBLISHABLE_KEY`
  * Returns an `AuthedUser` with the Clerk user id (`sub` claim)

Telemetry endpoints additionally depend on `require_approved_user`,
which gates access on an operator-controlled allow-list:
  * `OPERATOR_ALLOWED_EMAILS` — comma-separated env var of approved
    primary email addresses (case-insensitive), and/or
  * Clerk user metadata: `public_metadata.approved == true` (or the
    same flag in `private_metadata`), settable from the Clerk dashboard.

There is no fallback path — no shared bearer token, no same-origin
shortcut, no referer check.  Either the request carries a valid Clerk
session JWT *and* the user is on the allow-list, or it gets 401/403.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
import jwt
from fastapi import Depends, Header, HTTPException
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


def verify_clerk_jwt(token: str) -> AuthedUser:
    """Verify a Clerk session JWT.  Raises HTTPException on failure.

    Reusable from both HTTP (`require_auth`) and WebSocket handlers,
    where the token arrives via `?token=` instead of an Authorization
    header.
    """
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    jwks = _jwks_client()
    issuer = _issuer()
    if jwks is None or not issuer:
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
    except Exception as e:
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


async def require_auth(
    authorization: str | None = Header(default=None),
) -> AuthedUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    return verify_clerk_jwt(token)


# ---------------------------------------------------------------------------
# Operator approval gate
# ---------------------------------------------------------------------------


def _allowed_emails() -> set[str]:
    raw = os.environ.get("OPERATOR_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _email_from_claims(claims: dict[str, Any]) -> str | None:
    # Clerk session tokens may include email when the JWT template is
    # configured for it; fall back through a few common shapes.
    for key in ("email", "primary_email", "email_address", "primary_email_address"):
        v = claims.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def _metadata_approved(meta: Any) -> bool:
    if not isinstance(meta, dict):
        return False
    v = meta.get("approved")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


# Per-user cache of the Clerk Backend API user object.
_USER_CACHE_TTL = 60.0
_user_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_user_cache_lock = asyncio.Lock()


async def _fetch_clerk_user(user_id: str) -> dict[str, Any]:
    """Look up a Clerk user via the Backend API. Returns {} on any failure."""
    secret = os.environ.get("CLERK_SECRET_KEY")
    if not secret:
        return {}
    now = time.time()
    cached = _user_cache.get(user_id)
    if cached and now - cached[0] < _USER_CACHE_TTL:
        return cached[1]
    async with _user_cache_lock:
        cached = _user_cache.get(user_id)
        if cached and now - cached[0] < _USER_CACHE_TTL:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"https://api.clerk.com/v1/users/{user_id}",
                    headers={"Authorization": f"Bearer {secret}"},
                )
            if r.status_code != 200:
                log.warning("clerk user fetch %s: %s", r.status_code, r.text[:200])
                return {}
            data = r.json()
        except Exception as e:
            log.warning("clerk user fetch failed: %s", e)
            return {}
        if isinstance(data, dict):
            _user_cache[user_id] = (now, data)
            return data
        return {}


def _email_from_clerk_user(data: dict[str, Any]) -> str | None:
    primary_id = data.get("primary_email_address_id")
    addresses = data.get("email_addresses") or []
    if isinstance(addresses, list):
        for entry in addresses:
            if isinstance(entry, dict) and entry.get("id") == primary_id:
                v = entry.get("email_address")
                if isinstance(v, str) and v.strip():
                    return v.strip().lower()
        for entry in addresses:
            if isinstance(entry, dict):
                v = entry.get("email_address")
                if isinstance(v, str) and v.strip():
                    return v.strip().lower()
    return None


async def check_approval(user: AuthedUser) -> tuple[bool, str | None]:
    """Return (approved, primary_email_lower) for the authenticated user."""
    email = _email_from_claims(user.claims)
    allowed = _allowed_emails()

    # Fast paths from the JWT claims first to avoid a Clerk API call.
    if email and email in allowed:
        return True, email
    if _metadata_approved(user.claims.get("public_metadata")) or _metadata_approved(
        user.claims.get("private_metadata")
    ):
        return True, email

    # Fall back to the Clerk Backend API (cached) to fetch email + metadata.
    data = await _fetch_clerk_user(user.user_id)
    if not email:
        email = _email_from_clerk_user(data)
    if email and email in allowed:
        return True, email
    if _metadata_approved(data.get("public_metadata")) or _metadata_approved(
        data.get("private_metadata")
    ):
        return True, email
    return False, email


async def require_approved_user(
    user: AuthedUser = Depends(require_auth),
) -> AuthedUser:
    approved, _ = await check_approval(user)
    if not approved:
        raise HTTPException(
            status_code=403,
            detail="account pending operator approval",
        )
    return user


async def ws_authenticate(websocket) -> "AuthedUser | None":
    """Shared WS auth helper: accept the upgrade first, then verify the
    Clerk JWT (passed as `?token=`) and confirm operator approval.

    Accepting before close ensures the browser surfaces the custom 4401
    close code instead of an opaque 1006 from a denied handshake.

    Returns the authenticated user on success.  On failure, sends a
    structured error frame, closes with 4401, and returns ``None``.
    Callers must `return` immediately when ``None`` is returned.

    In dev mode (no auth configured), the socket is accepted without a
    user so existing local workflows continue to work.
    """
    await websocket.accept()
    if not auth_configured():
        return None  # type: ignore[return-value]
    token = websocket.query_params.get("token") or ""
    if not token:
        try:
            await websocket.send_json({"type": "auth_error", "error": "missing token"})
        except Exception:
            pass
        await websocket.close(code=4401)
        return None
    try:
        user = verify_clerk_jwt(token)
        approved, _ = await check_approval(user)
        if not approved:
            try:
                await websocket.send_json(
                    {"type": "auth_error", "error": "account pending operator approval"}
                )
            except Exception:
                pass
            await websocket.close(code=4401)
            return None
        return user
    except Exception as e:
        try:
            await websocket.send_json({"type": "auth_error", "error": str(e)[:200]})
        except Exception:
            pass
        await websocket.close(code=4401)
        return None
