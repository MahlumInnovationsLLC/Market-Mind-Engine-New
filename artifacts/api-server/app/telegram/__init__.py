"""Telegram concierge bot.

Per task #111. The concierge gives an operator off-laptop awareness of
the trading loop (today's P&L, open positions, recent halts) and a
confirm-protected ``/estop`` command. The bot is bound to a single
Clerk user — group chats and multi-tenant routing are explicitly out
of scope.

Module layout:
* :mod:`app.telegram.bindings` — DB schema, link codes, lookups.
* :mod:`app.telegram.queries`  — read-only data formatters reused by
  the ``/status``, ``/positions`` and ``/halts`` commands.
* :mod:`app.telegram.estop`    — the confirm flow that wraps the same
  logic as the ``/api/admin/emergency-stop`` HTTP handler.
* :mod:`app.telegram.bot`      — long-polling getUpdates loop +
  command dispatcher. Wired into ``app.main.lifespan``.
"""
