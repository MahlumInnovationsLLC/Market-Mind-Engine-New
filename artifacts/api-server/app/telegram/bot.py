"""Telegram concierge bot — long-poll loop and command dispatcher.

The bot is single-tenant: only Telegram users with a row in
``telegram_bindings`` are recognised, and only the Clerk user that
owns each binding can drive their own session. Group chats are
ignored (we never reply unless the chat type is ``private``).

Boot flow:
* ``start()`` is called from ``app.main.lifespan``. If
  ``TELEGRAM_BOT_TOKEN`` is unset the bot stays asleep — this is the
  expected configuration in dev / CI.
* The poller runs as a single asyncio task. Errors are logged and the
  loop sleeps 5s before retrying so a transient Telegram outage can't
  hot-loop the process.
* ``stop()`` cancels the task and returns; the next process boot
  starts a fresh loop.

Supported commands (after ``/start <code>`` binding):
* ``/help``       — list commands
* ``/whoami``     — show which Clerk operator the chat is bound to
* ``/status``     — same overview the dashboard renders
* ``/positions``  — currently-open positions
* ``/halts``      — kill-switch state + recent emergency stops
* ``/estop [reason]`` — begin a confirm-protected emergency stop
* ``/confirm <code>`` — confirm the pending /estop
* ``/cancel``     — drop a pending /estop without firing
* ``/unlink``     — remove the binding (operator can also remove from
  the dashboard)

Admin gating: ``/estop`` is only honoured when the bound Clerk user is
a platform admin per :func:`app.auth.check_admin`. The read-only
commands are open to any approved operator with a binding.
"""
from __future__ import annotations

import asyncio
import logging
import os  # noqa: F401  — used by the defensive fallback in _resolve_token
from typing import Any

import httpx

from .. import auth as auth_mod
from ..config import get_settings
from . import bindings, estop, queries


log = logging.getLogger(__name__)


_TELEGRAM_API = "https://api.telegram.org"
_LONG_POLL_TIMEOUT = 25  # seconds Telegram holds the request open
_HTTP_TIMEOUT = _LONG_POLL_TIMEOUT + 10
_RETRY_DELAY_SECONDS = 5
# Hard cap on the message length we ship back. Telegram's limit is 4096
# but we keep some headroom for the per-line prefix the formatters add.
_MAX_MESSAGE_LEN = 3800


_task: asyncio.Task | None = None
_token: str | None = None
_bot_username: str | None = None


def _resolve_token() -> str | None:
    # Centralised through Settings so all env-var aliasing lives in
    # one place; falls back to a direct env read if Settings is
    # unavailable (defensive — the bot must never crash boot).
    try:
        tok = get_settings().telegram_bot_token
        if tok:
            return tok
    except Exception:
        pass
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip() or None


async def start() -> None:
    """Start the long-poll loop if a bot token is configured."""
    global _task, _token
    if _task is not None and not _task.done():
        return
    _token = _resolve_token()
    if not _token:
        log.info("telegram bot disabled — TELEGRAM_BOT_TOKEN is not set")
        return
    _task = asyncio.create_task(_run(), name="telegram-bot")
    log.info("telegram bot poller started")


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    _task = None
    log.info("telegram bot poller stopped")


def is_running() -> bool:
    return _task is not None and not _task.done()


def bot_username() -> str | None:
    """Best-effort cached username (e.g. ``@AxiomConciergeBot``).

    Populated on the first successful ``getMe`` call inside the poller.
    Useful for the dashboard so it can render a one-tap deep link.
    """
    return _bot_username


async def _run() -> None:
    """Long-poll Telegram for updates and dispatch them.

    Tracks the last seen ``update_id`` so each batch of ``getUpdates``
    only returns new events. A Telegram-side error or network blip
    sleeps ``_RETRY_DELAY_SECONDS`` and retries instead of unwinding
    the lifespan task.
    """
    global _bot_username
    assert _token, "_run called without a token"
    offset: int | None = None
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        # Best-effort getMe to cache the username for dashboards.
        try:
            r = await client.get(_url("getMe"))
            data = r.json()
            if data.get("ok"):
                u = data.get("result", {}).get("username")
                if isinstance(u, str) and u:
                    _bot_username = u
        except Exception as e:
            log.warning("telegram getMe failed: %s", e)

        while True:
            try:
                params: dict[str, Any] = {
                    "timeout": _LONG_POLL_TIMEOUT,
                    "allowed_updates": ["message"],
                }
                if offset is not None:
                    params["offset"] = offset
                r = await client.get(_url("getUpdates"), params=params)
                if r.status_code != 200:
                    log.warning("telegram getUpdates HTTP %s: %s", r.status_code, r.text[:200])
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                data = r.json()
                if not data.get("ok"):
                    log.warning("telegram getUpdates not-ok: %s", data)
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                for update in data.get("result", []) or []:
                    try:
                        await _handle_update(client, update)
                    except Exception:
                        log.exception("telegram update handler crashed")
                    upd_id = update.get("update_id")
                    if isinstance(upd_id, int):
                        offset = upd_id + 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram poll loop error: %s", e)
                await asyncio.sleep(_RETRY_DELAY_SECONDS)


def _url(method: str) -> str:
    return f"{_TELEGRAM_API}/bot{_token}/{method}"


async def _handle_update(client: httpx.AsyncClient, update: dict[str, Any]) -> None:
    msg = update.get("message")
    if not isinstance(msg, dict):
        return
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        # Ignore group / channel chats — out of scope per task spec.
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    sender = msg.get("from") or {}
    tg_user_id = sender.get("id")
    chat_id = chat.get("id")
    if not isinstance(tg_user_id, int) or not isinstance(chat_id, int):
        return
    username = sender.get("username")
    parts = text.split()
    cmd = parts[0].lower()
    # Strip @botname suffix Telegram appends in some clients.
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    args = parts[1:]

    # /start and /help are open — every other command requires a binding.
    if cmd == "/start":
        await _cmd_start(client, chat_id, tg_user_id, username, args)
        return
    if cmd == "/help":
        await _send(client, chat_id, _help_text())
        return

    binding = await bindings.get_binding_by_telegram(tg_user_id)
    if not binding:
        await _send(
            client, chat_id,
            "This chat isn't linked to an Axiom operator yet.\n"
            "From the operator dashboard, generate a Telegram link code "
            "and send it here as:  /start CODE",
        )
        return
    await bindings.touch_seen(tg_user_id)

    if cmd == "/whoami":
        await _send(client, chat_id, _whoami_text(binding))
    elif cmd == "/status":
        await _send(client, chat_id, await queries.status_text())
    elif cmd == "/positions":
        await _send(client, chat_id, await queries.positions_text())
    elif cmd == "/halts":
        await _send(client, chat_id, await queries.halts_text())
    elif cmd == "/estop":
        await _cmd_estop(client, chat_id, tg_user_id, binding, args)
    elif cmd == "/confirm":
        await _cmd_confirm(client, chat_id, tg_user_id, binding, args)
    elif cmd == "/cancel":
        dropped = await estop.cancel_pending(tg_user_id)
        await _send(
            client, chat_id,
            "Pending /estop cancelled." if dropped else "No pending /estop to cancel.",
        )
    elif cmd == "/unlink":
        ok = await bindings.unbind(binding["clerk_user_id"])
        await _send(
            client, chat_id,
            "Binding removed. This chat is no longer connected to Axiom."
            if ok else "Binding could not be removed.",
        )
    else:
        await _send(client, chat_id, "Unknown command.\n\n" + _help_text())


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


async def _cmd_start(
    client: httpx.AsyncClient,
    chat_id: int,
    tg_user_id: int,
    username: str | None,
    args: list[str],
) -> None:
    if not args:
        await _send(
            client, chat_id,
            "Welcome to the Axiom concierge.\n"
            "From the operator dashboard, issue a Telegram link code and send "
            "it here as:  /start CODE",
        )
        return
    code = args[0]
    bound = await bindings.claim_link_code(
        code=code,
        telegram_user_id=tg_user_id,
        telegram_chat_id=chat_id,
        telegram_username=username,
    )
    if bound is None:
        await _send(
            client, chat_id,
            "That link code is invalid or has expired. Generate a new one "
            "from the operator dashboard and try again.",
        )
        return
    await _send(
        client, chat_id,
        "Linked. You can now use:\n" + _help_text(),
    )


async def _cmd_estop(
    client: httpx.AsyncClient,
    chat_id: int,
    tg_user_id: int,
    binding: dict[str, Any],
    args: list[str],
) -> None:
    if not await _is_admin(binding):
        await _send(
            client, chat_id,
            "/estop is restricted to platform admins.",
        )
        return
    reason = " ".join(args).strip() or "operator emergency stop (telegram)"
    pending = await estop.begin_confirm(tg_user_id, reason)
    await _send(
        client, chat_id,
        "EMERGENCY STOP requested.\n"
        f"Reason: {pending.reason}\n"
        f"To confirm within {estop.CONFIRM_TTL_SECONDS}s, send:\n"
        f"  /confirm {pending.code}\n"
        "To abort, send /cancel.",
    )


async def _cmd_confirm(
    client: httpx.AsyncClient,
    chat_id: int,
    tg_user_id: int,
    binding: dict[str, Any],
    args: list[str],
) -> None:
    if not args:
        await _send(client, chat_id, "Usage: /confirm CODE")
        return
    if not await _is_admin(binding):
        await _send(client, chat_id, "/confirm is restricted to platform admins.")
        return
    pending = await estop.consume_confirm(tg_user_id, args[0])
    if pending is None:
        await _send(
            client, chat_id,
            "No matching pending /estop, or the code expired. Send /estop again to retry.",
        )
        return
    try:
        result = await estop.execute_emergency_stop(
            actor_user_id=binding["clerk_user_id"],
            actor_email=None,
            reason=pending.reason,
        )
    except Exception:
        log.exception("telegram emergency-stop execution failed")
        await _send(
            client, chat_id,
            "Emergency stop hit an unexpected error. Check the operator console "
            "and the kill-switch status before assuming trading is halted.",
        )
        return
    canceled = result.get("canceled_orders")
    await _send(
        client, chat_id,
        "EMERGENCY STOP fired.\n"
        f"  trading_mode: {result.get('trading_mode')}\n"
        f"  canceled orders: {canceled if canceled is not None else 'n/a'}\n"
        f"  reason: {result.get('reason')}",
    )


async def _is_admin(binding: dict[str, Any]) -> bool:
    """Check the bound Clerk user against the admin gate.

    The bot doesn't have a JWT to feed into ``require_admin`` so we
    construct a minimal :class:`AuthedUser` and call the underlying
    :func:`auth.check_admin` (which already supports an
    ``app_users``-only check + bootstrap fallback).
    """
    try:
        user = auth_mod.AuthedUser(
            user_id=str(binding["clerk_user_id"]),
            session_id=None,
            claims={},
        )
        return await auth_mod.check_admin(user)
    except Exception as e:
        log.warning("admin check failed for tg binding %s: %s", binding.get("clerk_user_id"), e)
        return False


def _whoami_text(binding: dict[str, Any]) -> str:
    bound_at = binding.get("bound_at") or "?"
    return (
        "Telegram binding\n"
        f"  clerk user: {binding.get('clerk_user_id')}\n"
        f"  telegram user: {binding.get('telegram_user_id')}\n"
        f"  username: @{binding.get('telegram_username') or '?'}\n"
        f"  bound at: {bound_at}"
    )


def _help_text() -> str:
    return (
        "Commands:\n"
        "  /status      — today's P&L, open positions, kill-switch state\n"
        "  /positions   — currently-open positions across exchanges\n"
        "  /halts       — kill switch + recent emergency stops\n"
        "  /estop [why] — start a confirm-protected emergency stop (admin)\n"
        "  /confirm CODE— confirm a pending /estop within 60s\n"
        "  /cancel      — abort a pending /estop\n"
        "  /whoami      — show this chat's binding\n"
        "  /unlink      — remove this chat's binding\n"
        "  /help        — this message"
    )


# ---------------------------------------------------------------------------
# HTTP shim
# ---------------------------------------------------------------------------


async def send_to_chat(chat_id: int, text: str) -> bool:
    """Send a one-off message to ``chat_id`` from the bot.

    Used by the alert dispatch path (Elite-only ``telegram_live_trade``
    channel) to push notifications outside the polling loop. Returns
    ``True`` if the message was accepted by Telegram, ``False`` if the
    bot isn't configured or the API rejected the call. Never raises so
    the trading loop can't be derailed by a Telegram outage.
    """
    token = _resolve_token()
    if not token:
        log.info("telegram send_to_chat skipped (no token): chat=%s", chat_id)
        return False
    global _token
    _token = token
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await _send(client, int(chat_id), text)
        return True
    except Exception as e:  # pragma: no cover
        log.warning("telegram send_to_chat failed: %s", e)
        return False


def send_to_chats_in_background(chat_ids: list[int], text: str) -> None:
    """Fire-and-forget fan-out — schedule a Telegram send to every chat."""
    if not chat_ids or not text:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("telegram fan-out called with no loop; dropping")
        return
    async def _runner() -> None:
        await asyncio.gather(*[send_to_chat(c, text) for c in chat_ids])
    loop.create_task(_runner())


async def _send(client: httpx.AsyncClient, chat_id: int, text: str) -> None:
    if not text:
        return
    if len(text) > _MAX_MESSAGE_LEN:
        text = text[: _MAX_MESSAGE_LEN - 20] + "\n… (truncated)"
    try:
        r = await client.post(
            _url("sendMessage"),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        if r.status_code != 200:
            log.warning("telegram sendMessage HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("telegram sendMessage failed: %s", e)


# ---------------------------------------------------------------------------
# Test seam — used by the unit tests to drive the dispatcher without
# spinning up a real httpx client / poller.
# ---------------------------------------------------------------------------


async def handle_update_for_test(
    update: dict[str, Any],
    *,
    client: httpx.AsyncClient,
    token: str = "test-token",
) -> None:  # pragma: no cover — exercised by tests
    global _token
    _token = token
    await _handle_update(client, update)
