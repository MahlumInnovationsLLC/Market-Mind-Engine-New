"""Per-vertical isotonic calibrator served from the latest *retrained*
``calibration_snapshots`` row.

The online-learning worker (``app.settlement.retrain``) drains the
``online_learning_buffer`` and writes a fresh isotonic fit per vertical
into ``calibration_snapshots`` tagged ``drift_status->>'status' =
'retrained'``.  This module is the consumer side: it caches the latest
such snapshot per vertical in-process and exposes :func:`apply` so the
trading pipeline can route every ``model_prob`` through the freshly
retrained calibrator before the gatekeeper sees it.

Cache contract
--------------
* First :func:`apply` for a vertical lazy-loads the latest retrained
  snapshot from the DB.  Hit / miss is cached so we don't hammer
  Postgres on every cycle.  A miss caches ``None`` so trading
  short-circuits to a pass-through immediately.
* :func:`invalidate` drops the cached entry (per vertical or globally)
  so the next ``apply`` refetches.  The settlement event listener
  invalidates per vertical on every ``retrain_complete`` so a fresh
  refit lands in live decisions on the very next cycle.

Failure mode
------------
Every DB / parsing failure is swallowed with a warning and the call
falls back to returning the input probability unchanged.  A sick
calibrator must NEVER take down the trading loop — the pipeline keeps
running off the cold-storage predictor's raw probability, exactly the
same behavior as before this loader existed.
"""
from __future__ import annotations

import asyncio
import json
import logging
from bisect import bisect_left
from typing import Any

from .. import db
from ..settlement.streams import settlement_event_hub


log = logging.getLogger(__name__)


# Sentinel for "we checked the DB and there is no retrained snapshot
# for this vertical".  We cache the negative result so subsequent
# cycles short-circuit without re-querying.
_MISS = object()


_cache: dict[str, Any] = {}
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _apply_isotonic(p: float, xs: list[float], ys: list[float]) -> float:
    """Linear interpolation across the isotonic knots."""
    if not xs or not ys or len(xs) != len(ys):
        return p
    if p <= xs[0]:
        return float(ys[0])
    if p >= xs[-1]:
        return float(ys[-1])
    i = bisect_left(xs, p)
    if i == 0:
        return float(ys[0])
    if i >= len(xs):
        return float(ys[-1])
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return float(y1)
    t = (p - x0) / (x1 - x0)
    return float(y0 + t * (y1 - y0))


def _normalize_vertical(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_state_blob(blob: Any) -> dict[str, Any] | None:
    if blob is None:
        return None
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, (str, bytes, bytearray)):
        try:
            return json.loads(blob)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    return None


async def _fetch_latest(vertical: str) -> dict[str, Any] | None:
    """Pull the most recent retrained calibrator state for ``vertical``.

    Returns ``None`` when no row exists, the row's drift_status was
    flipped to ``rolled_back``, or the calibrator state is malformed.
    """
    try:
        row = await db.fetchrow(
            """
            SELECT id,
                   calibrator_state,
                   sample_count,
                   extract(epoch FROM created_at)::float AS fit_at
              FROM calibration_snapshots
             WHERE vertical = $1
               AND COALESCE(drift_status->>'status', '') = 'retrained'
               AND calibrator_state IS NOT NULL
               AND calibrator_state::text <> '{}'
             ORDER BY created_at DESC
             LIMIT 1
            """,
            vertical,
        )
    except Exception as e:
        log.warning("[live_calibrator] fetch failed for %s: %s", vertical, e)
        return None
    if row is None:
        return None
    state = _parse_state_blob(row["calibrator_state"])
    if not state or str(state.get("type") or "").lower() != "isotonic":
        return None
    xs = state.get("x") or []
    ys = state.get("y") or []
    if not xs or not ys or len(xs) != len(ys):
        return None
    return {
        "snapshot_id": int(row["id"]) if row["id"] is not None else None,
        "fit_at": float(row["fit_at"] or 0.0),
        "sample_count": int(row["sample_count"] or 0),
        "x": [float(v) for v in xs],
        "y": [float(v) for v in ys],
    }


async def _get_cached(vertical: str) -> dict[str, Any] | None:
    """Return the cached calibrator state for ``vertical`` (or ``None``).

    Lazy-loads on first miss; caches the negative result to keep the
    happy path in the trading loop a single dict lookup.
    """
    cached = _cache.get(vertical, _MISS)
    if cached is not _MISS:
        return cached  # may be None (negative cache) or a state dict
    async with _lock:
        # Re-check under the lock so concurrent apply() calls don't all
        # issue duplicate DB fetches for the same vertical.
        cached = _cache.get(vertical, _MISS)
        if cached is not _MISS:
            return cached
        loaded = await _fetch_latest(vertical)
        _cache[vertical] = loaded
        return loaded


async def apply(vertical: str | None, p: float) -> float:
    """Route a raw model probability through the latest retrained
    isotonic calibrator for ``vertical`` and return the calibrated
    value.

    Cleanly bypasses (returns ``p`` unchanged) when:
      * ``vertical`` is missing,
      * ``p`` is non-numeric or out of [0, 1] after clamping,
      * no retrained snapshot has landed for the vertical yet,
      * the cached state is malformed,
      * the apply call raises (defensive).
    """
    if not isinstance(p, (int, float)):
        # Per the docstring: bypass cleanly on any non-numeric input.
        # Coerce defensively — anything we can't read as a float falls
        # straight through with a 0.0 placeholder so the caller still
        # gets a real number to feed downstream math.
        try:
            return float(p)  # accepts numeric strings, decimals, etc.
        except (TypeError, ValueError):
            return 0.0
    raw = max(0.0, min(1.0, float(p)))
    vert = _normalize_vertical(vertical)
    if vert is None:
        return raw
    try:
        state = await _get_cached(vert)
    except Exception as e:  # pragma: no cover — defensive
        log.warning("[live_calibrator] cache lookup raised for %s: %s", vert, e)
        return raw
    if state is None:
        return raw
    try:
        cp = _apply_isotonic(raw, state["x"], state["y"])
    except Exception as e:  # pragma: no cover — defensive
        log.warning("[live_calibrator] apply raised for %s: %s", vert, e)
        return raw
    return max(0.0, min(1.0, float(cp)))


# ---------------------------------------------------------------------------
# Cache control
# ---------------------------------------------------------------------------


def invalidate(vertical: str | None = None) -> None:
    """Drop the cached calibrator for ``vertical`` (or everything when
    ``vertical`` is ``None``).  Next :func:`apply` for the dropped
    vertical will refetch from the DB.
    """
    if vertical is None:
        _cache.clear()
        return
    vert = _normalize_vertical(vertical)
    if vert is None:
        return
    _cache.pop(vert, None)


def cache_snapshot() -> dict[str, Any]:
    """Inspect the current cache state — exposed for tests + /system
    introspection.  Returns ``{vertical: {snapshot_id, fit_at, ...} | None}``.
    """
    return dict(_cache)


# ---------------------------------------------------------------------------
# Settlement event listener
# ---------------------------------------------------------------------------


async def _consume_invalidations(sub: Any) -> None:
    """Drain ``sub`` forever, dropping cache entries for verticals that
    just completed a retrain.

    A ``retrain_rolled_back`` event also invalidates: the cached entry
    might still be the bad row (rolled_back tag flips after the cache
    was warmed), and refetching yields the previous good snapshot per
    the loader filter.
    """
    try:
        while True:
            ev = await sub.get()
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype not in ("retrain_complete", "retrain_rolled_back"):
                continue
            vertical = ev.get("vertical")
            if not vertical:
                continue
            invalidate(str(vertical))
            log.info(
                "[live_calibrator] invalidated %s on %s (snapshot_id=%s)",
                vertical, etype, ev.get("snapshot_id"),
            )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # pragma: no cover — defensive
        log.warning("[live_calibrator] invalidation listener exited: %s", e)
    finally:
        settlement_event_hub.unsubscribe(sub)


_listener_task: asyncio.Task[None] | None = None
_listener_sub: Any = None


def start_invalidation_listener() -> asyncio.Task[None]:
    """Start (or return the existing) background listener task.

    Subscribes *synchronously* on the calling thread before spawning
    the consumer task so any subsequent ``publish`` is guaranteed to
    reach this subscriber even if the consumer task hasn't been
    scheduled yet.  Idempotent: calling repeatedly returns the same
    task so re-entrant lifespan setup or test harnesses don't spawn
    duplicate listeners.
    """
    global _listener_task, _listener_sub
    if _listener_task is not None and not _listener_task.done():
        return _listener_task
    _listener_sub = settlement_event_hub.subscribe(
        label="live-calibrator-invalidator"
    )
    _listener_task = asyncio.create_task(
        _consume_invalidations(_listener_sub),
        name="live-calibrator-invalidator",
    )
    return _listener_task


def stop_invalidation_listener() -> None:
    """Cancel the background listener (test teardown)."""
    global _listener_task, _listener_sub
    if _listener_task is not None and not _listener_task.done():
        _listener_task.cancel()
    _listener_task = None
    if _listener_sub is not None:
        try:
            settlement_event_hub.unsubscribe(_listener_sub)
        except Exception:
            pass
    _listener_sub = None
