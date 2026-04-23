"""Shared async-HTTP retry primitive used by every outbound REST client.

Goal: keep the trading loop alive across transient upstream blips
(Kalshi/Polymarket 502/503, SportsRadar 504, Postmark 503, Clerk 504,
random TCP resets).  Mutating endpoints (order POSTs) should NOT use
this helper because retrying a duplicate `POST /portfolio/orders`
would risk double-fills.

Usage::

    from .http_retry import retry_request, register_attempt

    async def list_markets(self) -> Any:
        async def _do() -> httpx.Response:
            return await self._http.get(url, headers=headers)
        r = await retry_request(_do, client_name="kalshi", max_attempts=4)
        ...

The helper records every attempt + outcome on a small in-memory
registry so :mod:`app.health` can surface per-client retry / failure
counters in the snapshot payload.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx


log = logging.getLogger("axiom.http_retry")


# ---------------------------------------------------------------------------
# Counter registry (in-memory, per-client)
# ---------------------------------------------------------------------------


@dataclass
class _ClientCounters:
    total_attempts: int = 0
    total_retries: int = 0
    total_failures: int = 0
    last_failure_at: float | None = None
    last_failure_status: int | None = None
    last_failure_error: str | None = None
    last_success_at: float | None = None


_COUNTERS: dict[str, _ClientCounters] = {}


def _bucket(name: str) -> _ClientCounters:
    bucket = _COUNTERS.get(name)
    if bucket is None:
        bucket = _ClientCounters()
        _COUNTERS[name] = bucket
    return bucket


def register_attempt(name: str) -> None:
    _bucket(name).total_attempts += 1


def register_retry(name: str) -> None:
    _bucket(name).total_retries += 1


def register_success(name: str) -> None:
    _bucket(name).last_success_at = time.time()


def register_failure(name: str, *, status: int | None, error: str) -> None:
    b = _bucket(name)
    b.total_failures += 1
    b.last_failure_at = time.time()
    b.last_failure_status = status
    b.last_failure_error = error[:300]


def snapshot_counters() -> dict[str, dict]:
    """Return a JSON-safe copy of every client's counters for /api/health."""
    out: dict[str, dict] = {}
    for name, b in _COUNTERS.items():
        out[name] = {
            "total_attempts": b.total_attempts,
            "total_retries": b.total_retries,
            "total_failures": b.total_failures,
            "last_failure_at": b.last_failure_at,
            "last_failure_status": b.last_failure_status,
            "last_failure_error": b.last_failure_error,
            "last_success_at": b.last_success_at,
        }
    return out


def reset_counters() -> None:
    """Test helper — clears all counters."""
    _COUNTERS.clear()


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Best-effort parse of a `Retry-After` header (seconds or HTTP date)."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        delta = dt.timestamp() - time.time()
        return max(0.0, delta)
    except Exception:
        return None


def _backoff_seconds(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter."""
    expo = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0.0, expo)


async def retry_request(
    do: Callable[[], Awaitable[httpx.Response]],
    *,
    client_name: str,
    max_attempts: int = 4,
    backoff_base: float = 0.4,
    backoff_cap: float = 8.0,
    retryable_statuses: frozenset[int] = RETRYABLE_STATUSES,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> httpx.Response:
    """Run ``do()`` with retry-on-transient-failure semantics.

    Retries when the call raises :class:`httpx.TransportError` or returns
    an HTTP status in ``retryable_statuses``.  Honours the upstream
    ``Retry-After`` header when present, otherwise uses exponential
    backoff with full jitter.  Non-retryable 4xx responses are returned
    to the caller immediately so the caller can raise its own typed
    error.

    Counters are recorded on every attempt regardless of outcome so the
    health snapshot can surface "this client has been struggling".
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    sleeper = sleep or asyncio.sleep
    last_error: BaseException | None = None
    last_response: httpx.Response | None = None

    for attempt in range(1, max_attempts + 1):
        register_attempt(client_name)
        try:
            resp = await do()
        except httpx.TransportError as e:
            last_error = e
            register_failure(client_name, status=None, error=f"{type(e).__name__}: {e}")
            if attempt >= max_attempts:
                raise
            register_retry(client_name)
            delay = _backoff_seconds(attempt, backoff_base, backoff_cap)
            log.warning(
                "%s transport error (attempt %d/%d): %s — retrying in %.2fs",
                client_name, attempt, max_attempts, e, delay,
            )
            await sleeper(delay)
            continue

        if resp.status_code in retryable_statuses and attempt < max_attempts:
            last_response = resp
            register_failure(
                client_name,
                status=resp.status_code,
                error=f"HTTP {resp.status_code}",
            )
            register_retry(client_name)
            ra = _retry_after_seconds(resp)
            delay = ra if ra is not None else _backoff_seconds(attempt, backoff_base, backoff_cap)
            log.warning(
                "%s retryable HTTP %d (attempt %d/%d) — retrying in %.2fs",
                client_name, resp.status_code, attempt, max_attempts, delay,
            )
            try:
                await resp.aclose()
            except Exception:  # noqa: BLE001
                pass
            await sleeper(delay)
            continue

        # Non-retryable response (success or terminal 4xx) — return.
        if resp.status_code < 400:
            register_success(client_name)
        else:
            register_failure(
                client_name,
                status=resp.status_code,
                error=f"HTTP {resp.status_code}",
            )
        return resp

    # Loop exhausted with a retryable response still pending.
    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{client_name}: retry loop exhausted with no response")
