"""Proactive Telegram push helpers (task #186).

Today the concierge bot (`app.telegram.bot`) is pull-only — operators
have to ask `/status`, `/halts`, etc. This module adds the push side:
a `broadcast(text)` helper that fans a one-off message out to every
bound operator chat that hasn't muted push.

Wired into the halt / emergency-stop alert paths in
`app.notifications.alerts` so the kill switch tripping or an
emergency-stop firing now DMs every opted-in operator in addition to
the existing email broadcast.

Per-binding opt-out lives on `telegram_bindings.notify_push` (added by
the ``0015_telegram_notify_push`` migration). Bindings missing the
column (older rows) fall through as opted-in by virtue of the column
default.
"""
from __future__ import annotations

import asyncio
import logging

from .. import db
from . import bot as _bot


log = logging.getLogger(__name__)


async def push_enabled_chat_ids() -> list[int]:
    """Return every bound `telegram_chat_id` whose binding has
    `notify_push = true`.

    Fails soft (returns empty list) on any DB error so a Postgres blip
    can't take the alert dispatch with it.
    """
    try:
        rows = await db.fetch(
            """
            SELECT telegram_chat_id
              FROM telegram_bindings
             WHERE telegram_chat_id IS NOT NULL
               AND bound_at IS NOT NULL
               AND COALESCE(notify_push, true) = true
            """
        )
    except Exception as e:
        log.warning("push_enabled_chat_ids query failed: %s", e)
        return []
    out: list[int] = []
    for r in rows:
        cid = r["telegram_chat_id"]
        if cid is None:
            continue
        try:
            out.append(int(cid))
        except (TypeError, ValueError):
            continue
    return out


async def broadcast(text: str) -> int:
    """Fan a Telegram message out to every opted-in operator chat.

    Returns the count of chats that were targeted (regardless of
    Telegram's per-chat result — the underlying `send_to_chat` swallows
    its own failures so one bad chat can't block the rest).

    No-op (returns 0) when:
    - `text` is empty/whitespace
    - the bot token isn't configured (`bot.send_to_chat` short-circuits)
    - no bindings have `notify_push = true`
    """
    if not text or not text.strip():
        return 0
    chats = await push_enabled_chat_ids()
    if not chats:
        return 0
    await asyncio.gather(*(_bot.send_to_chat(c, text) for c in chats))
    return len(chats)


def broadcast_in_background(text: str) -> None:
    """Fire-and-forget version of :func:`broadcast` for sync callers
    (e.g. the kill-switch trip path which lives outside async code).

    Schedules the broadcast on the running event loop. If there is no
    running loop (sync test), runs the coroutine inline.
    """
    if not text or not text.strip():
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(broadcast(text))
    except RuntimeError:
        try:
            asyncio.run(broadcast(text))
        except Exception as e:  # pragma: no cover
            log.warning("inline telegram broadcast failed: %s", e)


__all__ = (
    "broadcast",
    "broadcast_in_background",
    "push_enabled_chat_ids",
)
