"""Bounded backpressure primitives for in-process WS fan-out hubs.

Each hub (`pipeline_event_hub`, `settlement_event_hub`, `kalshi_bus`,
`polymarket_bus`, `llm_log_hub`) hands every WebSocket subscriber its
own bounded `asyncio.Queue` so a slow / stuck client cannot pin
unbounded memory on the publisher.

When a subscriber's queue fills we apply one of two policies:

* ``"drop_oldest"`` — pop the oldest queued event and enqueue the new
  one (default; keeps the producer cheap and the consumer eventually
  consistent — only the in-flight backlog is lost).
* ``"close"`` — refuse the new event and push a close sentinel so the
  consumer wakes up, observes ``overflowed = True``, and tears down
  the connection.  Use this for channels where stale events are worse
  than no events (e.g. live orderbook).

The full subscriber registry is exposed via :func:`registry_snapshot`
so /api/health/snapshot can render per-connection queue depth, drop
counts, and which channels are stuck.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
import weakref
from typing import Any


DROP_OLDEST = "drop_oldest"
CLOSE = "close"

# Legacy hard-coded ceiling from task #248.  Used as the floor when the
# operator has not set ``AXIOM_WS_QUEUE_MAXSIZE`` so behaviour is
# unchanged on existing deployments.
_LEGACY_DEFAULT_MAXSIZE = 256

# Recognised hubs.  Each accepts a per-hub override env var of the form
# ``AXIOM_WS_QUEUE_MAXSIZE_<HUB>`` (uppercased, dashes -> underscores)
# that takes precedence over the global ``AXIOM_WS_QUEUE_MAXSIZE``.
HUB_NAMES: tuple[str, ...] = (
    "pipeline",
    "settlement",
    "kalshi",
    "polymarket",
    "llm-logs",
)

_GLOBAL_ENV = "AXIOM_WS_QUEUE_MAXSIZE"

_log = logging.getLogger(__name__)


def _coerce_maxsize(raw: str | None, *, source: str) -> int | None:
    """Parse an env var value into a positive int, or return ``None`` if
    unset / invalid.  An invalid value is logged once and ignored so a
    typo doesn't take the API down at import time.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        _log.warning(
            "ignoring %s=%r (not an integer); falling back to default", source, raw
        )
        return None
    if value < 1:
        _log.warning(
            "ignoring %s=%r (must be >= 1); falling back to default", source, raw
        )
        return None
    return value


def _hub_env_name(hub: str) -> str:
    return f"{_GLOBAL_ENV}_{hub.upper().replace('-', '_')}"


def resolve_maxsize(hub: str) -> int:
    """Resolve the per-hub queue ceiling from env vars at call time.

    Precedence: ``AXIOM_WS_QUEUE_MAXSIZE_<HUB>`` then
    ``AXIOM_WS_QUEUE_MAXSIZE`` then the legacy default of 256.
    """
    per_hub = _coerce_maxsize(os.environ.get(_hub_env_name(hub)), source=_hub_env_name(hub))
    if per_hub is not None:
        return per_hub
    glb = _coerce_maxsize(os.environ.get(_GLOBAL_ENV), source=_GLOBAL_ENV)
    if glb is not None:
        return glb
    return _LEGACY_DEFAULT_MAXSIZE


# Resolved at import time so call sites can keep referring to a plain
# module-level constant.  Per-hub values are resolved lazily by each
# hub on construction (see ``resolve_maxsize``) — the global default
# only honours ``AXIOM_WS_QUEUE_MAXSIZE`` (no per-hub suffix).
DEFAULT_MAXSIZE = (
    _coerce_maxsize(os.environ.get(_GLOBAL_ENV), source=_GLOBAL_ENV)
    or _LEGACY_DEFAULT_MAXSIZE
)


def queue_config_snapshot() -> dict[str, Any]:
    """Per-hub configured queue ceiling for /api/health/snapshot.

    Lets operators confirm the env-var override actually took effect on
    the running deployment without grepping logs.
    """
    return {
        "default": DEFAULT_MAXSIZE,
        "global_env": _GLOBAL_ENV,
        "global_env_set": os.environ.get(_GLOBAL_ENV) is not None,
        "by_hub": {
            hub: {
                "maxsize": resolve_maxsize(hub),
                "env": _hub_env_name(hub),
                "env_set": os.environ.get(_hub_env_name(hub)) is not None,
            }
            for hub in HUB_NAMES
        },
    }

# Standard WebSocket close code + reason for backpressure overflow.
# 1011 = "internal server error" in the RFC 6455 / IANA registry; we
# pair it with a stable reason string so client telemetry can group
# overflow disconnects without parsing log lines.
OVERFLOW_CLOSE_CODE = 1011
OVERFLOW_CLOSE_REASON = "subscriber_backlog_overflow"

_VALID_POLICIES = frozenset({DROP_OLDEST, CLOSE})
_id_counter = itertools.count(1)


class _CloseSentinel:
    __slots__ = ()


_CLOSE = _CloseSentinel()


class SubscriberOverflow(Exception):
    """Raised by Subscription.get() when the close-on-overflow policy
    fires.  Caller should close the underlying transport (WebSocket).
    """


class Subscription:
    """Per-subscriber bounded queue plus observability counters.

    Quacks like ``asyncio.Queue`` for legacy call sites
    (``await sub.get()`` / ``sub.qsize()``).  Use object identity to
    pass back to ``hub.unsubscribe(sub)``.
    """

    __slots__ = (
        "id",
        "hub",
        "label",
        "policy",
        "maxsize",
        "queue",
        "created_at",
        "dropped",
        "overflowed",
        "__weakref__",
    )

    def __init__(
        self, *, hub: str, label: str, maxsize: int, policy: str
    ) -> None:
        if policy not in _VALID_POLICIES:
            raise ValueError(f"unknown policy: {policy!r}")
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.id = next(_id_counter)
        self.hub = hub
        self.label = label
        self.policy = policy
        self.maxsize = maxsize
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self.created_at = time.time()
        self.dropped = 0
        self.overflowed = False
        _REGISTRY.add(self)

    # ------------------------------------------------------------------
    # producer side — only the hub calls these
    # ------------------------------------------------------------------
    def offer(self, event: Any) -> None:
        """Enqueue ``event`` honouring the configured drop policy.

        Never raises and never blocks the publisher.
        """
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        if self.policy == CLOSE:
            self.overflowed = True
            # Evict the oldest buffered event so the close sentinel is
            # guaranteed to reach the consumer's next ``await get()``;
            # otherwise the consumer would deadlock on a full queue with
            # no way to learn it was supposed to disconnect.
            try:
                self.queue.get_nowait()
            except Exception:
                pass
            try:
                self.queue.put_nowait(_CLOSE)
            except asyncio.QueueFull:
                pass
            return
        # drop_oldest
        try:
            self.queue.get_nowait()
        except Exception:
            pass
        try:
            self.queue.put_nowait(event)
            self.dropped += 1
        except Exception:
            pass

    def release(self) -> None:
        _REGISTRY.discard(self)

    # ------------------------------------------------------------------
    # consumer side
    # ------------------------------------------------------------------
    async def get(self) -> Any:
        item = await self.queue.get()
        if isinstance(item, _CloseSentinel):
            raise SubscriberOverflow(
                f"{self.hub}/{self.label}: subscriber backlog exceeded {self.maxsize}"
            )
        return item

    def qsize(self) -> int:
        return self.queue.qsize()

    def empty(self) -> bool:
        return self.queue.empty()

    def full(self) -> bool:
        return self.queue.full()

    def get_nowait(self) -> Any:
        item = self.queue.get_nowait()
        if isinstance(item, _CloseSentinel):
            raise SubscriberOverflow(
                f"{self.hub}/{self.label}: subscriber backlog exceeded {self.maxsize}"
            )
        return item

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "hub": self.hub,
            "label": self.label,
            "policy": self.policy,
            "maxsize": self.maxsize,
            "qsize": self.queue.qsize(),
            "dropped": self.dropped,
            "overflowed": self.overflowed,
            "age_seconds": round(time.time() - self.created_at, 1),
        }


# Use a WeakSet so a forgotten subscriber doesn't leak forever in the
# registry — once the WS handler drops its reference the Subscription
# is collected and falls out of the snapshot.
_REGISTRY: "weakref.WeakSet[Subscription]" = weakref.WeakSet()


def registry_snapshot() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sub in list(_REGISTRY):
        try:
            out.append(sub.snapshot())
        except Exception:
            continue
    out.sort(key=lambda s: (-s["qsize"], s["hub"], s["id"]))
    return out


def registry_summary() -> dict[str, Any]:
    snaps = registry_snapshot()
    by_hub: dict[str, dict[str, int]] = {}
    for s in snaps:
        h = by_hub.setdefault(s["hub"], {"count": 0, "qsize_total": 0,
                                          "dropped_total": 0, "overflowed": 0})
        h["count"] += 1
        h["qsize_total"] += int(s["qsize"])
        h["dropped_total"] += int(s["dropped"])
        if s["overflowed"]:
            h["overflowed"] += 1
    return {
        "total_subscribers": len(snaps),
        "by_hub": by_hub,
        "subscribers": snaps,
    }
