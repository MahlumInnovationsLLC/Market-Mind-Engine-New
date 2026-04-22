"""Admin role + admin console backend.

Persists a mirror of every authenticated Clerk user we have seen
(`app_users`) plus an audit log of approval / admin role changes
(`admin_audit_log`).  The tables are created on startup via
:func:`ensure_schema`.

The admin model layers on top of the existing approval gate:

* `is_admin == True` implies `approved == True`.
* The very first admin is bootstrapped from the `OPERATOR_ADMIN_EMAILS`
  env var so the system has an operator-of-operators even on a
  blank database.  Once a row exists in `app_users` with
  ``is_admin = true`` for that email, the env var is no longer required.
* Approval (`approved`) is sourced from `app_users` first, falling back
  to `OPERATOR_ALLOWED_EMAILS` and Clerk public/private metadata.

All mutations are funnelled through helpers in this module so they
always end up in `admin_audit_log` and are mirrored back to Clerk's
``public_metadata`` (best effort) so existing JWT-claim fast paths in
``auth.check_approval`` stay consistent.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from . import db


log = logging.getLogger("axiom.admin")


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_users (
    clerk_user_id   text PRIMARY KEY,
    email           text,
    display_name    text,
    approved        boolean NOT NULL DEFAULT false,
    is_admin        boolean NOT NULL DEFAULT false,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    activity_count  integer NOT NULL DEFAULT 0,
    clerk_created_at      timestamptz,
    clerk_last_sign_in_at timestamptz,
    clerk_synced_at       timestamptz
);

ALTER TABLE app_users ADD COLUMN IF NOT EXISTS clerk_created_at      timestamptz;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS clerk_last_sign_in_at timestamptz;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS clerk_synced_at       timestamptz;
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS denied_at             timestamptz;

CREATE INDEX IF NOT EXISTS app_users_email_idx ON app_users (lower(email));

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id              bigserial PRIMARY KEY,
    actor_user_id   text NOT NULL,
    actor_email     text,
    target_user_id  text NOT NULL,
    target_email    text,
    action          text NOT NULL,
    before_value    jsonb,
    after_value     jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS admin_audit_log_target_idx
    ON admin_audit_log (target_user_id, created_at DESC);
"""


_SCHEMA_READY = False


async def ensure_schema() -> None:
    """Historically created the admin tables on first request.

    The schema is now owned by the Alembic baseline migration
    (``0001_baseline``) which runs in the FastAPI lifespan before any
    request is served. This function is kept as a no-op to preserve the
    call sites in case a future change wants to reintroduce a
    fallback path; calling it is cheap and never touches the DB.
    """
    global _SCHEMA_READY
    _SCHEMA_READY = True


# ---------------------------------------------------------------------------
# Bootstrap admin emails (env var)
# ---------------------------------------------------------------------------


def bootstrap_admin_emails() -> set[str]:
    raw = os.environ.get("OPERATOR_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _is_bootstrap_admin(email: str | None) -> bool:
    if not email:
        return False
    return email.strip().lower() in bootstrap_admin_emails()


# ---------------------------------------------------------------------------
# Upsert / lookup
# ---------------------------------------------------------------------------


async def upsert_seen_user(
    clerk_user_id: str,
    email: str | None,
    display_name: str | None,
) -> dict[str, Any] | None:
    """Insert or refresh the row for an authenticated user.

    Returns the resulting row as a dict (or ``None`` if Postgres is
    unavailable — callers must tolerate this since auth must keep
    working even with a degraded DB).
    """
    await ensure_schema()
    bootstrap = _is_bootstrap_admin(email)
    try:
        async with db.acquire() as c:
            row = await c.fetchrow(
                """
                INSERT INTO app_users (
                    clerk_user_id, email, display_name,
                    approved, is_admin,
                    first_seen_at, last_seen_at, activity_count
                ) VALUES ($1, $2, $3, $4, $4, now(), now(), 1)
                ON CONFLICT (clerk_user_id) DO UPDATE SET
                    email          = COALESCE(EXCLUDED.email, app_users.email),
                    display_name   = COALESCE(EXCLUDED.display_name, app_users.display_name),
                    approved       = app_users.approved OR EXCLUDED.approved,
                    is_admin       = app_users.is_admin OR EXCLUDED.is_admin,
                    last_seen_at   = now(),
                    activity_count = app_users.activity_count + 1
                RETURNING *
                """,
                clerk_user_id,
                email,
                display_name,
                bootstrap,
            )
            return dict(row) if row is not None else None
    except Exception as e:
        log.warning("app_users upsert failed for %s: %s", clerk_user_id, e)
        return None


async def get_user_row(clerk_user_id: str) -> dict[str, Any] | None:
    try:
        async with db.acquire() as c:
            row = await c.fetchrow(
                "SELECT * FROM app_users WHERE clerk_user_id = $1",
                clerk_user_id,
            )
            return dict(row) if row is not None else None
    except Exception as e:
        log.warning("app_users lookup failed for %s: %s", clerk_user_id, e)
        return None


# ---------------------------------------------------------------------------
# Admin list / detail
# ---------------------------------------------------------------------------


async def list_users() -> list[dict[str, Any]]:
    await ensure_schema()
    try:
        async with db.acquire() as c:
            rows = await c.fetch(
                """
                SELECT
                  clerk_user_id,
                  email,
                  display_name,
                  approved,
                  is_admin,
                  to_char(first_seen_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS first_seen_at,
                  to_char(last_seen_at,  'YYYY-MM-DD"T"HH24:MI:SSZ') AS last_seen_at,
                  activity_count,
                  to_char(clerk_created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')      AS clerk_created_at,
                  to_char(clerk_last_sign_in_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS clerk_last_sign_in_at,
                  clerk_synced_at
                FROM app_users
                ORDER BY last_seen_at DESC NULLS LAST
                """
            )
            users = [dict(r) for r in rows]
    except Exception as e:
        log.warning("admin list_users failed: %s", e)
        return []

    # Refresh stale Clerk metadata (sign-up date / last sign-in) so the
    # admin list shows accurate data even before each user signs in again.
    await _refresh_clerk_meta(users)
    for u in users:
        u.pop("clerk_synced_at", None)
    return users


_CLERK_REFRESH_TTL_SEC = 60 * 10  # 10 minutes


async def _refresh_clerk_meta(users: list[dict[str, Any]]) -> None:
    secret = os.environ.get("CLERK_SECRET_KEY")
    if not secret:
        return
    import asyncio
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stale: list[dict[str, Any]] = []
    for u in users:
        synced = u.get("clerk_synced_at")
        if synced is None:
            stale.append(u)
            continue
        try:
            age = (now - synced).total_seconds()
        except Exception:
            age = _CLERK_REFRESH_TTL_SEC + 1
        if age > _CLERK_REFRESH_TTL_SEC:
            stale.append(u)
    if not stale:
        return

    async def _one(u: dict[str, Any]) -> None:
        prof = await _fetch_clerk_profile(u["clerk_user_id"])
        if not prof:
            return
        created = _from_clerk_ts(prof.get("created_at"))
        last_in = _from_clerk_ts(prof.get("last_sign_in_at"))
        u["clerk_created_at"] = (
            created.strftime("%Y-%m-%dT%H:%M:%SZ") if created else u.get("clerk_created_at")
        )
        u["clerk_last_sign_in_at"] = (
            last_in.strftime("%Y-%m-%dT%H:%M:%SZ")
            if last_in
            else u.get("clerk_last_sign_in_at")
        )
        try:
            async with db.acquire() as c:
                await c.execute(
                    """
                    UPDATE app_users SET
                      clerk_created_at      = COALESCE($2, clerk_created_at),
                      clerk_last_sign_in_at = COALESCE($3, clerk_last_sign_in_at),
                      clerk_synced_at       = now()
                    WHERE clerk_user_id = $1
                    """,
                    u["clerk_user_id"],
                    created,
                    last_in,
                )
        except Exception as e:
            log.warning("clerk meta cache write failed for %s: %s", u["clerk_user_id"], e)

    # Cap concurrency to avoid hammering Clerk on large teams
    sem = asyncio.Semaphore(5)

    async def _bounded(u: dict[str, Any]) -> None:
        async with sem:
            await _one(u)

    await asyncio.gather(*[_bounded(u) for u in stale], return_exceptions=True)


def _from_clerk_ts(v: Any):
    """Clerk returns timestamps as ms-since-epoch. Convert to aware datetime."""
    if v is None:
        return None
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(v) / 1000.0, tz=timezone.utc)
    except Exception:
        return None


async def get_user_detail(clerk_user_id: str) -> dict[str, Any] | None:
    await ensure_schema()
    row = await get_user_row(clerk_user_id)
    if row is None:
        return None
    out = {
        "clerk_user_id": row["clerk_user_id"],
        "email": row.get("email"),
        "display_name": row.get("display_name"),
        "approved": bool(row.get("approved")),
        "is_admin": bool(row.get("is_admin")),
        "activity_count": int(row.get("activity_count") or 0),
        "first_seen_at": _iso(row.get("first_seen_at")),
        "last_seen_at": _iso(row.get("last_seen_at")),
        "approval_source": _approval_source(row),
        "clerk_profile": await _fetch_clerk_profile(clerk_user_id),
    }
    return out


def _approval_source(row: dict[str, Any]) -> str:
    if row.get("is_admin"):
        return "admin"
    if row.get("approved"):
        return "app_users"
    email = (row.get("email") or "").strip().lower()
    if email and email in _allowed_emails():
        return "env_allowlist"
    if _is_bootstrap_admin(email):
        return "bootstrap_admin_env"
    return "pending"


def _allowed_emails() -> set[str]:
    raw = os.environ.get("OPERATOR_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except Exception:
        return str(v)


async def _fetch_clerk_profile(clerk_user_id: str) -> dict[str, Any]:
    """Best-effort fetch of the Clerk user object for display only."""
    secret = os.environ.get("CLERK_SECRET_KEY")
    if not secret:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.clerk.com/v1/users/{clerk_user_id}",
                headers={"Authorization": f"Bearer {secret}"},
            )
        if r.status_code != 200:
            return {}
        data = r.json() or {}
    except Exception as e:
        log.warning("clerk profile fetch failed for %s: %s", clerk_user_id, e)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "id": data.get("id"),
        "first_name": data.get("first_name"),
        "last_name": data.get("last_name"),
        "username": data.get("username"),
        "image_url": data.get("image_url"),
        "created_at": data.get("created_at"),
        "last_sign_in_at": data.get("last_sign_in_at"),
        "public_metadata": data.get("public_metadata"),
    }


# ---------------------------------------------------------------------------
# Mutations + audit log
# ---------------------------------------------------------------------------


async def _ensure_target_row(
    target_user_id: str,
) -> dict[str, Any] | None:
    """Make sure a target user has an `app_users` row before we mutate it."""
    row = await get_user_row(target_user_id)
    if row is not None:
        return row
    profile = await _fetch_clerk_profile(target_user_id)
    email = None
    name = None
    if profile:
        addresses = profile.get("public_metadata") or {}
        # public profile doesn't include emails — we'll let the
        # next sign-in populate it
        email = profile.get("username")
        first = profile.get("first_name") or ""
        last = profile.get("last_name") or ""
        name = f"{first} {last}".strip() or profile.get("username")
        _ = addresses
    try:
        async with db.acquire() as c:
            row = await c.fetchrow(
                """
                INSERT INTO app_users (clerk_user_id, email, display_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (clerk_user_id) DO NOTHING
                RETURNING *
                """,
                target_user_id,
                email,
                name,
            )
        if row is not None:
            return dict(row)
    except Exception as e:
        log.warning("ensure target row failed for %s: %s", target_user_id, e)
    return await get_user_row(target_user_id)


class AdminInvariantError(Exception):
    """Raised when a mutation would violate the admin/approved invariant."""


async def set_approved(
    *,
    actor_user_id: str,
    actor_email: str | None,
    target_user_id: str,
    approved: bool,
) -> dict[str, Any]:
    # Invariant: admin implies approved. Refuse to revoke approval
    # while the target is still an admin — caller must demote first.
    if not approved:
        existing = await get_user_row(target_user_id)
        if existing and existing.get("is_admin"):
            raise AdminInvariantError(
                "cannot revoke approval while user is still an admin; demote first"
            )
    result = await _set_flag(
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_user_id=target_user_id,
        column="approved",
        value=approved,
        action="set_approved",
        clerk_metadata_key="approved",
    )
    # Approving a user should also clear any prior denial so they leave
    # the pending-requests panel and stay out of it.
    if approved:
        try:
            async with db.acquire() as c:
                await c.execute(
                    "UPDATE app_users SET denied_at = NULL WHERE clerk_user_id = $1",
                    target_user_id,
                )
        except Exception as e:
            log.warning("clear denied_at failed for %s: %s", target_user_id, e)
    return result


async def set_denied(
    *,
    actor_user_id: str,
    actor_email: str | None,
    target_user_id: str,
) -> dict[str, Any]:
    """Mark a pending access request as denied.

    Leaves ``approved`` / ``is_admin`` alone (an admin shouldn't be
    "denied" via this path) but stamps ``denied_at`` so the user drops
    out of the operator's pending-requests panel. Records an audit row
    so the action shows up in the admin audit log.
    """
    await ensure_schema()
    before_row = await _ensure_target_row(target_user_id)
    if before_row and before_row.get("is_admin"):
        raise AdminInvariantError(
            "cannot deny an admin; demote first"
        )
    before_denied = bool(before_row.get("denied_at")) if before_row else False
    try:
        async with db.acquire() as c:
            row = await c.fetchrow(
                """
                UPDATE app_users SET denied_at = now()
                WHERE clerk_user_id = $1
                RETURNING *
                """,
                target_user_id,
            )
    except Exception as e:
        log.warning("set_denied update failed for %s: %s", target_user_id, e)
        row = None
    after_row = dict(row) if row is not None else (before_row or {})
    target_email = after_row.get("email") if after_row else None
    try:
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id,
                actor_email,
                target_user_id,
                target_email,
                "set_denied",
                f'{{"denied": {str(before_denied).lower()}}}',
                '{"denied": true}',
            )
    except Exception as e:
        log.warning("audit log insert (deny) failed: %s", e)
    return {
        "clerk_user_id": target_user_id,
        "approved": bool(after_row.get("approved")),
        "is_admin": bool(after_row.get("is_admin")),
        "denied": True,
    }


async def list_pending_access_requests() -> list[dict[str, Any]]:
    """Users who have signed in but are still waiting for an operator
    to approve them. Denied users drop out — re-signing in or a manual
    DB clear of ``denied_at`` brings them back."""
    await ensure_schema()
    try:
        async with db.acquire() as c:
            rows = await c.fetch(
                """
                SELECT
                  clerk_user_id,
                  email,
                  display_name,
                  approved,
                  is_admin,
                  to_char(first_seen_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS first_seen_at,
                  to_char(last_seen_at,  'YYYY-MM-DD"T"HH24:MI:SSZ') AS last_seen_at,
                  activity_count,
                  to_char(clerk_created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ')      AS clerk_created_at,
                  to_char(clerk_last_sign_in_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS clerk_last_sign_in_at
                FROM app_users
                WHERE approved = false
                  AND is_admin = false
                  AND denied_at IS NULL
                ORDER BY last_seen_at DESC NULLS LAST
                """
            )
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning("list_pending_access_requests failed: %s", e)
        return []


async def set_admin(
    *,
    actor_user_id: str,
    actor_email: str | None,
    target_user_id: str,
    is_admin: bool,
) -> dict[str, Any]:
    return await _set_flag(
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_user_id=target_user_id,
        column="is_admin",
        value=is_admin,
        action="set_admin",
        clerk_metadata_key="admin",
    )


async def _set_flag(
    *,
    actor_user_id: str,
    actor_email: str | None,
    target_user_id: str,
    column: str,
    value: bool,
    action: str,
    clerk_metadata_key: str,
) -> dict[str, Any]:
    await ensure_schema()
    before_row = await _ensure_target_row(target_user_id)
    before_value = bool(before_row.get(column)) if before_row else False
    # Promoting to admin implicitly approves; demoting from admin keeps
    # whatever approval state was already there.
    if column == "is_admin" and value:
        sql = (
            "UPDATE app_users SET is_admin = true, approved = true, "
            "last_seen_at = last_seen_at WHERE clerk_user_id = $1 RETURNING *"
        )
        async with db.acquire() as c:
            row = await c.fetchrow(sql, target_user_id)
    else:
        sql = (
            f"UPDATE app_users SET {column} = $2 WHERE clerk_user_id = $1 RETURNING *"
        )
        async with db.acquire() as c:
            row = await c.fetchrow(sql, target_user_id, value)
    after_row = dict(row) if row is not None else before_row or {}

    # Mirror the change into Clerk public_metadata so the JWT fast path
    # in auth.check_approval keeps working.
    target_email = after_row.get("email") if after_row else None
    await _mirror_to_clerk(target_user_id, clerk_metadata_key, value)

    # Audit row
    try:
        async with db.acquire() as c:
            await c.execute(
                """
                INSERT INTO admin_audit_log (
                    actor_user_id, actor_email,
                    target_user_id, target_email,
                    action, before_value, after_value
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                """,
                actor_user_id,
                actor_email,
                target_user_id,
                target_email,
                action,
                f'{{"{column}": {str(before_value).lower()}}}',
                f'{{"{column}": {str(value).lower()}}}',
            )
    except Exception as e:
        log.warning("audit log insert failed: %s", e)

    # Bust the per-user Clerk cache in auth so the next request reflects
    # the metadata mirror immediately.
    try:
        from . import auth as auth_module

        auth_module._user_cache.pop(target_user_id, None)
    except Exception:
        pass

    return {
        "clerk_user_id": target_user_id,
        "approved": bool(after_row.get("approved")),
        "is_admin": bool(after_row.get("is_admin")),
        "before": {column: before_value},
        "after": {column: value},
    }


async def _mirror_to_clerk(
    clerk_user_id: str, key: str, value: bool
) -> None:
    secret = os.environ.get("CLERK_SECRET_KEY")
    if not secret:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(
                f"https://api.clerk.com/v1/users/{clerk_user_id}/metadata",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Content-Type": "application/json",
                },
                json={"public_metadata": {key: value}},
            )
        if r.status_code >= 400:
            log.warning(
                "clerk metadata mirror %s=%s for %s: %s %s",
                key, value, clerk_user_id, r.status_code, r.text[:200],
            )
    except Exception as e:
        log.warning("clerk metadata mirror failed: %s", e)


async def list_audit_log(limit: int = 100) -> list[dict[str, Any]]:
    await ensure_schema()
    try:
        async with db.acquire() as c:
            rows = await c.fetch(
                """
                SELECT
                  id,
                  actor_user_id,
                  actor_email,
                  target_user_id,
                  target_email,
                  action,
                  before_value,
                  after_value,
                  to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SSZ') AS created_at
                FROM admin_audit_log
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                # asyncpg returns jsonb as a string; parse for the client
                for k in ("before_value", "after_value"):
                    v = d.get(k)
                    if isinstance(v, str):
                        try:
                            import json

                            d[k] = json.loads(v)
                        except Exception:
                            pass
                out.append(d)
            return out
    except Exception as e:
        log.warning("admin audit_log list failed: %s", e)
        return []
