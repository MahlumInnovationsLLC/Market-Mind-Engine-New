"""Process-wide trading kill switch.

Originally lived inside `app/kalshi/lifecycle.py` but was extracted so that
multiple exchange clients (Kalshi, Polymarket, ...) trip and observe the
same halt state. Tripping the switch from any exchange's `/halt` endpoint
stops order submission across **every** venue.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


class _KillSwitch:
    def __init__(self) -> None:
        self._halted = False
        self._reason: str | None = None
        self._at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def state(self) -> dict[str, Any]:
        return {
            "halted": self._halted,
            "reason": self._reason,
            "since": self._at.isoformat() if self._at else None,
        }

    def trip(self, reason: str | None = None) -> dict[str, Any]:
        was_halted = self._halted
        self._halted = True
        self._reason = reason or "operator halt"
        self._at = datetime.now(timezone.utc)
        log.warning("KILL SWITCH tripped: %s", self._reason)
        # Fire halt alert only on the rising edge so re-tripping doesn't spam.
        # Also gate on persistent dedupe so a process restart while halted
        # doesn't re-email the operator (in-memory `_halted` is lost on
        # boot but the dedupe row in Postgres survives).
        if not was_halted:
            try:
                from .notifications import alerts as _alerts
                reason = self._reason
                _alerts._schedule_dedupe_dispatch(
                    "halt",
                    f"reason:{_alerts._hash_reason(reason)}",
                    3600,
                    lambda: _alerts.halt(reason=reason),
                )
            except Exception as e:  # pragma: no cover — notification must not break halt
                log.warning("halt alert dispatch failed: %s", e)
        return self.state

    def reset(self) -> dict[str, Any]:
        self._halted = False
        self._reason = None
        self._at = None
        log.info("KILL SWITCH reset")
        return self.state


_kill = _KillSwitch()


def is_halted() -> bool:
    return _kill.halted


def halt(reason: str | None = None) -> dict[str, Any]:
    return _kill.trip(reason)


def resume() -> dict[str, Any]:
    return _kill.reset()


def kill_switch_state() -> dict[str, Any]:
    return _kill.state
