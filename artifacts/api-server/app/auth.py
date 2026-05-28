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
from jwt import PyJWK

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


class _SigningKey:
    """Mirror of `jwt.PyJWKClient`'s return type: exposes `.key`."""

    __slots__ = ("key",)

    def __init__(self, key: Any) -> None:
        self.key = key


class _HttpxJWKSClient:
    """JWKS fetcher that uses `httpx` instead of stdlib `urllib`.

    Drop-in replacement for `jwt.PyJWKClient` (only the
    `get_signing_key_from_jwt` surface we actually use). We swapped
    off `PyJWKClient` because it calls `urllib.request.urlopen`
    under the hood, which in the Replit production deployment fails
    SSL against Clerk's FAPI host with
    `SSL: UNEXPECTED_EOF_WHILE_READING` — every signed-in request
    then 401s and the UI shows "session expired". `httpx` (which we
    already use for the Clerk reverse proxy in `clerk_proxy.py`)
    works fine against the same host, so we route JWKS fetching
    through it too.

    Caching matches the original `lifespan=600`: the JWKS document
    is fetched at most once every 10 minutes, with a single in-flight
    refresh guarded by a lock so a burst of requests after expiry
    doesn't fan out into N concurrent fetches.
    """

    _LIFESPAN = 600.0
    _TIMEOUT = 10.0

    def __init__(self, url: str) -> None:
        self._url = url
        self._lock = asyncio.Lock()
        self._cached_at: float = 0.0
        self._keys_by_kid: dict[str, Any] = {}

    def _parse(self, doc: dict[str, Any]) -> dict[str, Any]:
        keys: dict[str, Any] = {}
        for entry in doc.get("keys") or []:
            if not isinstance(entry, dict):
                continue
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                continue
            try:
                keys[kid] = PyJWK(entry).key
            except Exception as e:  # noqa: BLE001
                log.warning("jwks: failed to parse key kid=%s: %s", kid, e)
        return keys

    def _fetch_sync(self) -> dict[str, Any]:
        with httpx.Client(timeout=self._TIMEOUT) as client:
            r = client.get(self._url)
            r.raise_for_status()
            return self._parse(r.json())

    def get_signing_key_from_jwt(self, token: str) -> _SigningKey:
        # Synchronous on purpose: `verify_clerk_jwt` is itself sync
        # (called from sync deps in FastAPI's threadpool). The httpx
        # sync client uses the same network stack as the async one
        # but avoids forcing the caller to await.
        try:
            header = jwt.get_unverified_header(token)
        except Exception as e:  # noqa: BLE001
            raise jwt.PyJWKClientError(f"unable to decode jwt header: {e}") from e
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise jwt.PyJWKClientError("jwt header is missing 'kid'")

        now = time.time()
        fresh = now - self._cached_at < self._LIFESPAN
        if fresh and kid in self._keys_by_kid:
            return _SigningKey(self._keys_by_kid[kid])

        # Refresh (either expired or unknown kid — Clerk rotates keys
        # occasionally and a kid miss is a strong signal a rotation
        # has happened, so refresh eagerly rather than 401).
        try:
            self._keys_by_kid = self._fetch_sync()
            self._cached_at = now
        except Exception as e:
            # On refresh failure, fall back to the last good cache so
            # a transient JWKS outage doesn't take the whole API down.
            if kid in self._keys_by_kid:
                log.warning(
                    "jwks refresh failed, serving cached key: %s", e
                )
                return _SigningKey(self._keys_by_kid[kid])
            raise jwt.PyJWKClientError(
                f"unable to fetch jwks: {e}"
            ) from e

        if kid not in self._keys_by_kid:
            raise jwt.PyJWKClientError(
                f"unable to find signing key matching kid={kid!r}"
            )
        return _SigningKey(self._keys_by_kid[kid])


@lru_cache(maxsize=1)
def _jwks_client() -> _HttpxJWKSClient | None:
    host = _frontend_api_host()
    if not host:
        return None
    return _HttpxJWKSClient(f"https://{host}/.well-known/jwks.json")


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


def _auto_approve_email_domains() -> set[str]:
    """Test-only operator-allow-list bypass.

    When ``OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS`` is set to a
    comma-separated list of email domains (e.g. ``"example.com"``),
    any authenticated Clerk user whose primary email lives in one of
    those domains is treated as approved — *regardless* of the
    persisted ``app_users`` row, the ``OPERATOR_ALLOWED_EMAILS``
    static list, and Clerk metadata.

    Exists so the e2e test harness can sign in via Clerk's testing
    tickets (which mint fresh users with ``@example.com`` style
    addresses on every run) without an operator hand-approving each
    one. Production deployments leave this env var unset, in which
    case the function returns the empty set and is a no-op.
    """
    raw = os.environ.get("OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS", "")
    return {
        d.strip().lower().lstrip("@")
        for d in raw.split(",")
        if d.strip()
    }


def _email_in_auto_approve_domain(email: str | None) -> bool:
    if not email:
        return False
    domains = _auto_approve_email_domains()
    if not domains:
        return False
    _, _, domain = email.lower().partition("@")
    return bool(domain) and domain in domains


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
            from .http_retry import retry_request

            async with httpx.AsyncClient(timeout=10.0) as client:
                async def _do():
                    return await client.get(
                        f"https://api.clerk.com/v1/users/{user_id}",
                        headers={"Authorization": f"Bearer {secret}"},
                    )

                r = await retry_request(_do, client_name="clerk", max_attempts=3)
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


def _bootstrap_admin_emails() -> set[str]:
    raw = os.environ.get("OPERATOR_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _metadata_admin(meta: Any) -> bool:
    if not isinstance(meta, dict):
        return False
    v = meta.get("admin")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


def _display_name_from_clerk_user(data: dict[str, Any]) -> str | None:
    first = (data.get("first_name") or "").strip()
    last = (data.get("last_name") or "").strip()
    full = (first + " " + last).strip()
    if full:
        return full
    username = data.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip()
    return None


async def _resolve_email_and_name(user: AuthedUser) -> tuple[str | None, str | None]:
    """Resolve the user's primary email + display name, hitting Clerk if needed."""
    email = _email_from_claims(user.claims)
    name = None
    # Avoid a Clerk hit if we already have the email and metadata flags
    # are not the only signal we need.
    data = await _fetch_clerk_user(user.user_id)
    if data:
        if not email:
            email = _email_from_clerk_user(data)
        name = _display_name_from_clerk_user(data)
    return email, name


async def check_admin(user: AuthedUser) -> bool:
    """Return True if this user is a platform admin.

    `app_users` is the authoritative source. If a row exists, its
    `is_admin` value wins — even if a stale JWT claim or env-var
    allowlist would otherwise say "admin". Only when *no* row exists
    do we fall back to Clerk metadata + the bootstrap env var so the
    very first admin can sign in on a blank database.
    """
    try:
        from . import admin as admin_module

        row = await admin_module.get_user_row(user.user_id)
    except Exception as e:
        log.warning("check_admin DB lookup failed: %s", e)
        row = None

    if row is not None:
        return bool(row.get("is_admin"))

    # Bootstrap path only — no app_users row yet.
    if _metadata_admin(user.claims.get("public_metadata")) or _metadata_admin(
        user.claims.get("private_metadata")
    ):
        return True
    email = _email_from_claims(user.claims)
    if not email:
        data = await _fetch_clerk_user(user.user_id)
        if _metadata_admin(data.get("public_metadata")) or _metadata_admin(
            data.get("private_metadata")
        ):
            return True
        email = _email_from_clerk_user(data)
    return bool(email and email in _bootstrap_admin_emails())


async def check_approval(user: AuthedUser) -> tuple[bool, str | None]:
    """Return (approved, primary_email_lower) for the authenticated user.

    `app_users` is the authoritative source for revocations: if a row
    exists, its `approved`/`is_admin` flags win, so an admin-issued
    revoke takes effect on the next request even if the user's JWT
    still carries `approved=true` claims and even if their email is
    still listed in `OPERATOR_ALLOWED_EMAILS`. We only consult the env
    allowlist / Clerk metadata when no row exists yet (bootstrap).
    """
    email = _email_from_claims(user.claims)

    # Test-only bypass: if the user's email lives in an
    # OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS domain, short-circuit BEFORE
    # the app_users lookup so the bypass survives even when the row
    # exists but was last persisted with approved=False (e.g. the
    # test ticket was created on an earlier run when the env var was
    # not yet set). The env var is empty in production, so the check
    # is a no-op there.
    if _email_in_auto_approve_domain(email):
        return True, email

    try:
        from . import admin as admin_module

        row = await admin_module.get_user_row(user.user_id)
    except Exception as e:
        log.warning("check_approval DB lookup failed: %s", e)
        row = None

    if row is not None:
        approved = bool(row.get("approved") or row.get("is_admin"))
        row_email = row.get("email") or email
        # Test-only bypass: if the persisted row carries an email
        # whose domain is in OPERATOR_AUTO_APPROVE_EMAIL_DOMAINS, treat
        # the user as approved even when the row says otherwise. The
        # JWT may not have carried the email claim (so the upfront
        # bypass at the top of this function had nothing to match
        # against), but the row was upserted from a previous request
        # that did resolve the email via the Clerk Backend API. The
        # env var is empty in production, so this is a no-op there.
        if not approved and _email_in_auto_approve_domain(row_email):
            return True, row_email
        return approved, row_email

    # Bootstrap path only — no app_users row yet.
    allowed = _allowed_emails()
    bootstrap_admins = _bootstrap_admin_emails()
    if email and (email in allowed or email in bootstrap_admins):
        return True, email
    if (
        _metadata_approved(user.claims.get("public_metadata"))
        or _metadata_approved(user.claims.get("private_metadata"))
        or _metadata_admin(user.claims.get("public_metadata"))
        or _metadata_admin(user.claims.get("private_metadata"))
    ):
        return True, email
    data = await _fetch_clerk_user(user.user_id)
    if not email:
        email = _email_from_clerk_user(data)
    # Re-check the auto-approve domain bypass after we've resolved
    # the email via the Clerk Backend API — the JWT may not have
    # carried the email claim, in which case the upfront check above
    # had nothing to match against.
    if _email_in_auto_approve_domain(email):
        return True, email
    if email and (email in allowed or email in bootstrap_admins):
        return True, email
    if (
        _metadata_approved(data.get("public_metadata"))
        or _metadata_approved(data.get("private_metadata"))
        or _metadata_admin(data.get("public_metadata"))
        or _metadata_admin(data.get("private_metadata"))
    ):
        return True, email
    return False, email


async def require_approved_user(
    user: AuthedUser = Depends(require_auth),
) -> AuthedUser:
    approved, email = await check_approval(user)
    # Refresh app_users so the admin console always sees the caller.
    try:
        from . import admin as admin_module

        _, name = await _resolve_email_and_name(user)
        await admin_module.upsert_seen_user(user.user_id, email, name)
    except Exception as e:
        log.warning("upsert_seen_user (approved) failed: %s", e)
    if not approved:
        raise HTTPException(
            status_code=403,
            detail="account pending operator approval",
        )
    return user


async def require_admin(
    user: AuthedUser = Depends(require_auth),
) -> AuthedUser:
    is_admin = await check_admin(user)
    if not is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    try:
        from . import admin as admin_module

        email, name = await _resolve_email_and_name(user)
        await admin_module.upsert_seen_user(user.user_id, email, name)
    except Exception as e:
        log.warning("upsert_seen_user (admin) failed: %s", e)
    return user


async def ws_send_hello(websocket, *, channel: str | None = None) -> None:
    """Send a small ``{"type":"hello"}`` frame as proof the connection
    is fully wired through to a producing handler.

    The browser hook (``use-websocket.ts``) uses receipt of this frame
    as the ack for its seamless token-rotation handshake: the
    replacement socket is not promoted (and the old socket is not
    closed) until this frame lands.  Without it, an opaque
    ``code 1006`` close on the replacement between the TCP handshake
    and the first real event would drop the user's subscription.

    Best-effort: any send error is swallowed so a disconnect
    mid-handshake never escapes into the caller's main loop.  Call
    this right after subscribing to the relevant hub but before
    entering the receive loop.
    """
    import time as _time

    try:
        payload: dict[str, object] = {
            "type": "hello",
            "ts": int(_time.time() * 1000),
        }
        if channel:
            payload["channel"] = channel
        await websocket.send_json(payload)
    except Exception:
        # Disconnect / closed transport during the handshake is a
        # normal lifecycle event, not a bug to log.
        pass


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
        # Log the full exception server-side; only return a generic
        # message to the client so we don't leak verifier internals.
        import logging

        logging.getLogger("axiom.api").info("ws auth failed: %s", e)
        try:
            await websocket.send_json({"type": "auth_error", "error": "invalid token"})
        except Exception:
            pass
        await websocket.close(code=4401)
        return None
