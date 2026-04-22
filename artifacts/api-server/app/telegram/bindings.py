"""Telegram <-> Clerk binding storage.

The schema is owned by the ``0004_telegram_bindings`` Alembic
migration. This module never runs DDL; it just reads and writes rows.

Lifecycle of a binding:

1. Operator hits ``POST /api/me/telegram/link`` from the dashboard. We
   generate a short-lived 8-character link code, store it on the
   operator's ``telegram_bindings`` row (creating the row if needed)
   and return the code to the dashboard so they can paste it into the
   bot.
2. Operator opens the bot in Telegram and sends ``/start <code>``. The
   bot looks up the row by ``link_code`` (and checks
   ``link_code_expires_at``), then writes the Telegram identity onto
   it. The link code is cleared on success so it can't be replayed.
3. Subsequent commands look up the row by Telegram user id.

A single Clerk user can only own one Telegram binding at a time and a
single Telegram user can only be bound to one Clerk user — both sides
are enforced by unique constraints in the migration.
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db


log = logging.getLogger(__name__)


# How long the dashboard-issued link code is valid before the operator
# has to request a new one.
LINK_CODE_TTL = timedelta(minutes=15)
LINK_CODE_LEN = 8
# Excludes look-alike characters (0/O, 1/I/L) so the operator doesn't
# fat-finger when typing the code into Telegram.
_LINK_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gen_link_code() -> str:
    return "".join(secrets.choice(_LINK_ALPHABET) for _ in range(LINK_CODE_LEN))


async def issue_link_code(clerk_user_id: str) -> dict[str, Any]:
    """Generate a fresh link code for ``clerk_user_id``.

    Overwrites any existing pending code. The returned dict carries
    ``code``, ``expires_at`` (UTC ISO-8601) and a ``bot_username``
    placeholder the API layer can populate from the bot's getMe so the
    dashboard can render a one-click link.
    """
    code = _gen_link_code()
    expires = _now() + LINK_CODE_TTL
    row = await db.fetchrow(
        """
        INSERT INTO telegram_bindings (clerk_user_id, link_code, link_code_expires_at)
        VALUES ($1, $2, $3)
        ON CONFLICT (clerk_user_id) DO UPDATE
          SET link_code            = EXCLUDED.link_code,
              link_code_expires_at = EXCLUDED.link_code_expires_at,
              updated_at           = now()
        RETURNING clerk_user_id, link_code, link_code_expires_at,
                  telegram_user_id, telegram_username, bound_at
        """,
        clerk_user_id, code, expires,
    )
    return _row_to_dict(row)


async def get_binding_by_clerk(clerk_user_id: str) -> dict[str, Any] | None:
    row = await db.fetchrow(
        "SELECT * FROM telegram_bindings WHERE clerk_user_id = $1",
        clerk_user_id,
    )
    return _row_to_dict(row) if row else None


async def get_binding_by_telegram(telegram_user_id: int) -> dict[str, Any] | None:
    row = await db.fetchrow(
        "SELECT * FROM telegram_bindings WHERE telegram_user_id = $1",
        int(telegram_user_id),
    )
    return _row_to_dict(row) if row else None


async def claim_link_code(
    code: str,
    telegram_user_id: int,
    telegram_chat_id: int,
    telegram_username: str | None,
) -> dict[str, Any] | None:
    """Bind ``telegram_user_id`` to whichever Clerk user owns ``code``.

    Returns the resulting row dict on success, ``None`` if the code is
    unknown or expired. Atomic: a second caller racing the first will
    see the code consumed.
    """
    code = (code or "").strip().upper()
    if not code:
        return None
    # Validate the code first. Without this check, a stray `/start
    # WRONGCODE` from an already-bound operator would silently detach
    # their existing binding via the cleanup UPDATE below.
    valid = await db.fetchval(
        """
        SELECT 1 FROM telegram_bindings
         WHERE link_code = $1
           AND link_code_expires_at IS NOT NULL
           AND link_code_expires_at > now()
        """,
        code,
    )
    if not valid:
        return None
    # Detach any prior row owned by this Telegram user so the uniqueness
    # constraint can't fire when the operator re-binds. Safe now that
    # we've confirmed there is a valid code to claim.
    await db.execute(
        """
        UPDATE telegram_bindings
           SET telegram_user_id = NULL,
               telegram_chat_id = NULL,
               telegram_username = NULL,
               bound_at = NULL,
               updated_at = now()
         WHERE telegram_user_id = $1
        """,
        int(telegram_user_id),
    )
    row = await db.fetchrow(
        """
        UPDATE telegram_bindings
           SET telegram_user_id     = $2,
               telegram_chat_id     = $3,
               telegram_username    = $4,
               link_code            = NULL,
               link_code_expires_at = NULL,
               bound_at             = now(),
               last_seen_at         = now(),
               updated_at           = now()
         WHERE link_code = $1
           AND link_code_expires_at IS NOT NULL
           AND link_code_expires_at > now()
        RETURNING *
        """,
        code, int(telegram_user_id), int(telegram_chat_id), telegram_username,
    )
    return _row_to_dict(row) if row else None


async def touch_seen(telegram_user_id: int) -> None:
    try:
        await db.execute(
            "UPDATE telegram_bindings SET last_seen_at = now() WHERE telegram_user_id = $1",
            int(telegram_user_id),
        )
    except Exception as e:  # never fail a command because of a touch
        log.debug("touch_seen failed: %s", e)


async def unbind(clerk_user_id: str) -> bool:
    res = await db.execute(
        "DELETE FROM telegram_bindings WHERE clerk_user_id = $1",
        clerk_user_id,
    )
    # asyncpg returns "DELETE <n>" — true if anything was deleted.
    try:
        return int(res.rsplit(" ", 1)[-1]) > 0
    except Exception:
        return False


def _row_to_dict(row) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = dict(row)
    for k in ("link_code_expires_at", "bound_at", "last_seen_at", "created_at", "updated_at"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat()
    return out
