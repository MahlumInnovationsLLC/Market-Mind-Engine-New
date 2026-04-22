"""READY gate.

A single asyncio.Event the trading orchestrator (and any other
background loop that must wait for a healthy boot) parks on. Set only
after:

  * Alembic migrations have applied
  * required application secrets are present
  * the model artifacts have loaded successfully
  * every enabled external feed has responded at least once (success or
    failure — the gate cares only that the polling pipeline is alive)

The HTTP layer surfaces the gate state via ``GET /api/health/ready``,
which returns 503 until the gate flips. Liveness (``/api/healthz``)
is unaffected.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any


log = logging.getLogger(__name__)


@dataclass
class _CheckResult:
    name: str
    ok: bool
    detail: str = ""
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


_event: asyncio.Event | None = None
_checks: dict[str, _CheckResult] = {}
_started_at: float = 0.0
_completed_at: float | None = None


def _ensure_event() -> asyncio.Event:
    global _event
    if _event is None:
        _event = asyncio.Event()
    return _event


def is_ready() -> bool:
    return _event is not None and _event.is_set()


async def wait() -> None:
    """Block until the ready gate has flipped."""
    await _ensure_event().wait()


def mark_check(name: str, ok: bool, detail: str = "") -> None:
    _checks[name] = _CheckResult(name=name, ok=ok, detail=detail)
    log.info("ready-gate check %s = %s (%s)", name, "ok" if ok else "FAIL", detail)


def begin() -> None:
    """Reset the gate at the start of a lifespan."""
    global _checks, _started_at, _completed_at, _event
    _checks = {}
    _started_at = time.time()
    _completed_at = None
    _event = asyncio.Event()


def set_ready() -> None:
    global _completed_at
    _completed_at = time.time()
    _ensure_event().set()
    log.info(
        "READY gate flipped after %.2fs — orchestrator may unpark",
        (_completed_at - _started_at) if _started_at else 0.0,
    )


def status() -> dict[str, Any]:
    return {
        "ready": is_ready(),
        "started_at": _started_at or None,
        "completed_at": _completed_at,
        "elapsed_seconds": (
            round((_completed_at or time.time()) - _started_at, 2)
            if _started_at
            else None
        ),
        "checks": {name: c.to_dict() for name, c in _checks.items()},
    }
