"""Resolve the effective trading mode (paper / live / halt).

The Elite-only ``POST /api/orchestrator/live-execute`` endpoint writes
``system_settings.risk.trading_mode`` and the kill-switch risk gate
already honours it.  This helper exposes the same lookup to the order
lifecycle modules so the *actual* paper-vs-live decision flips with the
toggle instead of staring at a stale env var.

Operator override beats env (mirrors the gate's precedence).  The lookup
is best-effort: any DB error falls back to the env-derived value so a
flaky Postgres can't accidentally silence live trading or vice-versa.
"""
from __future__ import annotations

import logging

from .. import db
from ..config import get_settings

log = logging.getLogger(__name__)


async def resolve_trading_mode(env_default: str | None = None) -> str:
    """Return the effective trading mode, lower-cased.

    Precedence:
      1. ``system_settings.risk.trading_mode`` (operator override)
      2. ``env_default`` if supplied — caller already resolved settings
      3. ``settings.trading_mode`` (env var, default "paper")
    """
    db_mode: str | None = None
    try:
        v = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = 'risk.trading_mode'"
        )
        if v is not None:
            db_mode = str(v).lower()
    except Exception as e:  # noqa: BLE001 — best-effort, fall back to env
        log.debug("trading_mode override read failed (best-effort): %s", e)
    if db_mode:
        return db_mode
    if env_default is not None:
        return env_default.lower()
    return (get_settings().trading_mode or "paper").lower()
