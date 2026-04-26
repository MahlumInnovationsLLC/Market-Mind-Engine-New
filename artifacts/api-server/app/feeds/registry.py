"""In-process registry + per-feed status tracker.

Every feed reports its own liveness here so the operator console and the
`/api/health` + `/api/feeds/status` endpoints can render a unified view
without touching individual feed implementations.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar


log = logging.getLogger("axiom.feeds")


@dataclass
class FeedStatus:
    """Mutable, in-memory status for a single feed."""
    name: str
    cadence_seconds: float
    enabled: bool = True
    skipped_reason: str | None = None
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_duration_ms: float | None = None
    last_ok_at: float | None = None
    last_error_at: float | None = None
    last_error: str | None = None
    last_records_written: int | None = None
    records_written_total: int = 0
    success_count: int = 0
    failure_count: int = 0
    poll_count: int = 0
    rate_limited_count: int = 0
    rate_limited_until: float | None = None
    # Free-form per-feed metadata that individual pollers can stash
    # (e.g. soccer pollers record the cached Sportradar season id and
    # the timestamp of the most recent 404-driven refresh here so the
    # operator console can render a "rolled over" badge / "season id
    # stuck" warning).  Always serialised as part of `to_dict()` so it
    # flows straight into the health-card extras with no extra plumbing.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "cadence_seconds": self.cadence_seconds,
            "enabled": self.enabled,
            "skipped_reason": self.skipped_reason,
            "last_started_at": _ts(self.last_started_at),
            "last_finished_at": _ts(self.last_finished_at),
            "last_duration_ms": self.last_duration_ms,
            "last_ok_at": _ts(self.last_ok_at),
            "last_error_at": _ts(self.last_error_at),
            "last_error": _truncate(self.last_error),
            "last_records_written": self.last_records_written,
            "records_written_total": self.records_written_total,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "poll_count": self.poll_count,
            "rate_limited_count": self.rate_limited_count,
            "rate_limited_until": _ts(self.rate_limited_until),
            "metadata": dict(self.metadata),
        }

    def is_rate_limited(self) -> bool:
        return self.rate_limited_until is not None and time.time() < self.rate_limited_until


def _ts(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _truncate(s: str | None, limit: int = 240) -> str | None:
    if s is None:
        return None
    return s if len(s) <= limit else s[: limit - 1] + "…"


PollFn = Callable[[], Awaitable[int]]
"""A poll function returns the number of records it persisted."""


@dataclass
class Feed:
    """A single pollable data source.

    The `poll` callable is expected to be idempotent and to return the
    number of rows it wrote.  Any exception it raises is captured by the
    scheduler and logged into `FeedStatus.last_error`; the next tick
    proceeds normally.
    """
    name: str
    cadence_seconds: float
    poll: PollFn
    enabled: bool = True
    skipped_reason: str | None = None


class FeedRegistry:
    """Process-wide singleton that owns every Feed + its FeedStatus."""

    _instance: ClassVar["FeedRegistry | None"] = None

    def __init__(self) -> None:
        self._feeds: dict[str, Feed] = {}
        self._status: dict[str, FeedStatus] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def instance(cls) -> "FeedRegistry":
        if cls._instance is None:
            cls._instance = FeedRegistry()
        return cls._instance

    def register(self, feed: Feed) -> None:
        if feed.name in self._feeds:
            raise ValueError(f"feed {feed.name!r} already registered")
        self._feeds[feed.name] = feed
        self._status[feed.name] = FeedStatus(
            name=feed.name,
            cadence_seconds=feed.cadence_seconds,
            enabled=feed.enabled,
            skipped_reason=feed.skipped_reason,
        )
        log.info(
            "registered feed %s (cadence=%.1fs enabled=%s%s)",
            feed.name,
            feed.cadence_seconds,
            feed.enabled,
            f" skipped={feed.skipped_reason}" if feed.skipped_reason else "",
        )

    def feeds(self) -> list[Feed]:
        return list(self._feeds.values())

    def status_of(self, name: str) -> FeedStatus | None:
        return self._status.get(name)

    def snapshot(self) -> list[dict[str, Any]]:
        """Stable, ordered snapshot of every registered feed's status."""
        return [self._status[n].to_dict() for n in sorted(self._status)]

    async def record_start(self, name: str) -> float:
        async with self._lock:
            st = self._status[name]
            now = time.time()
            st.last_started_at = now
            st.poll_count += 1
            return now

    async def record_success(self, name: str, started_at: float, written: int) -> None:
        async with self._lock:
            st = self._status[name]
            now = time.time()
            st.last_finished_at = now
            st.last_ok_at = now
            st.last_duration_ms = round((now - started_at) * 1000.0, 1)
            st.last_records_written = written
            if written > 0:
                st.records_written_total += written
            st.last_error = None
            st.success_count += 1

    async def record_rate_limited(self, name: str, retry_after_seconds: float) -> None:
        """Mark a feed as rate-limited; the scheduler will skip its next poll until the deadline."""
        async with self._lock:
            st = self._status[name]
            st.rate_limited_count += 1
            st.rate_limited_until = time.time() + max(1.0, retry_after_seconds)
            log.warning("feed %s rate-limited; backing off %.1fs", name, retry_after_seconds)

    def set_metadata(self, name: str, **fields: Any) -> None:
        """Merge ``fields`` into the named feed's metadata dict.

        Synchronous + lock-free on purpose: per-feed metadata is updated
        from inside the poll body (already serialized by the scheduler's
        per-feed task) and reads happen via ``snapshot()`` which copies
        the dict.  Fields whose value is ``None`` are stored as-is so a
        caller can explicitly null out a previous value.  Silently no-op
        for unknown feeds so an experimental feed swap can't crash a
        running poll.
        """
        st = self._status.get(name)
        if st is None:
            return
        st.metadata.update(fields)

    async def record_failure(self, name: str, started_at: float, err: BaseException) -> None:
        async with self._lock:
            st = self._status[name]
            now = time.time()
            st.last_finished_at = now
            st.last_error_at = now
            st.last_duration_ms = round((now - started_at) * 1000.0, 1)
            st.last_error = f"{type(err).__name__}: {err}"
            st.failure_count += 1


registry = FeedRegistry.instance()
