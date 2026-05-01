"""Canary mode for live cutover (Task #752 step 5 — Round-11 audit Bug #8).

Going from paper to live carries non-trivial systemic risk: latency to
real exchanges, slippage assumptions, account-level reject codes, and
all the long-tail "the test environment lied to us" surprises.  The
canary is a hard cap on the *first N* live trades after a paper→live
flip: each is forced to ``quantity = 1`` regardless of what the Kelly
sizer / risk gates wanted.  After ``N`` of those trades have settled
(not been *placed* — settled, so we've actually observed exchange
behavior end-to-end), the canary lifts and full sizing returns.

State lives in ``system_settings`` under two keys:

* ``risk.canary_remaining`` — integer count of live trades that must
  still go out with quantity=1.  Initialized to ``AXIOM_CANARY_TRADES``
  (default 5) on first read if absent.  Decremented by the settlement
  pipeline only when a trade tagged ``metadata.canary = True`` settles.
* ``risk.canary_init_at`` — ISO timestamp of when the counter was
  initialized.  Operator-facing only; not consulted by the runtime.

The two state-mutating helpers (:func:`force_quantity_for_live` and
:func:`decrement_on_settle`) both swallow DB errors — a transient
read failure must not block placement, and a transient write failure
on settlement must not block accounting.  The bias is **safer**:
on read failure we behave as if the canary is active (force qty=1);
on write failure on settlement we leave the counter at its previous
value so the canary stays active a bit longer rather than ending
prematurely.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from .. import db


log = logging.getLogger("axiom.risk.canary")


_KEY_REMAINING = "risk.canary_remaining"
_KEY_INIT_AT = "risk.canary_init_at"


def _default_canary_count() -> int:
    """Number of live trades to force qty=1 after a paper→live flip."""
    raw = os.environ.get("AXIOM_CANARY_TRADES")
    if raw is None:
        return 5
    try:
        v = int(raw)
        return v if v >= 0 else 5
    except (ValueError, TypeError):
        return 5


# ---------------------------------------------------------------------------
# Read / write the counter
# ---------------------------------------------------------------------------


async def get_remaining() -> int | None:
    """Return the current canary counter, or ``None`` on read failure.

    Callers MUST treat ``None`` as "canary still active" — see
    :func:`force_quantity_for_live` for the safer-default semantics.
    """
    try:
        v = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = $1",
            _KEY_REMAINING,
        )
    except Exception as e:
        log.warning("[canary] get_remaining read failed: %s", e)
        return None
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        log.warning("[canary] non-integer value in system_settings: %r", v)
        return None


async def ensure_initialized() -> int:
    """Idempotently initialize the canary counter.

    On first call writes ``AXIOM_CANARY_TRADES`` (default 5) to
    ``system_settings`` and stamps ``risk.canary_init_at``.  Subsequent
    calls observe the existing value and return it unchanged.

    Returns the current counter (post-init).  Raises on DB failure so
    the caller (operator endpoint) can surface a 502.
    """
    existing = await get_remaining()
    if existing is not None:
        return existing
    seed = _default_canary_count()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO NOTHING
        """,
        _KEY_REMAINING,
        str(seed),
    )
    await db.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at
        """,
        _KEY_INIT_AT,
        now,
    )
    log.info("canary initialized: remaining=%s init_at=%s", seed, now)
    return seed


async def reset(count: int | None = None) -> int:
    """Operator hook: restart the canary at ``count`` (or env-default).

    Used after a paper→live re-flip — the operator may want a fresh
    5-trade canary even if the previous one already settled.  Always
    overwrites; never errors on a missing row.
    """
    seed = int(count) if count is not None else _default_canary_count()
    seed = max(0, seed)
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at
        """,
        _KEY_REMAINING,
        str(seed),
    )
    await db.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at
        """,
        _KEY_INIT_AT,
        now,
    )
    log.info("canary reset: remaining=%s init_at=%s", seed, now)
    return seed


# ---------------------------------------------------------------------------
# Placement-side: force qty=1 while canary > 0
# ---------------------------------------------------------------------------


async def force_quantity_for_live(intended_quantity: int) -> tuple[int, bool]:
    """Decide the effective quantity for a live trade about to be placed.

    Returns ``(qty, canary_active)``:

    * If ``canary_active`` is True, the caller MUST stamp
      ``metadata.canary = True`` on the trade row so the settlement
      pipeline can decrement the counter when the trade settles.
    * If ``canary_active`` is False, ``qty == intended_quantity`` and
      no special metadata is needed.

    Read errors bias toward **safer**: we return ``(1, True)`` so a
    transient ``system_settings`` read failure does NOT silently let a
    full-size live trade through during cutover.
    """
    remaining = await get_remaining()
    if remaining is None:
        log.warning(
            "[canary] forcing qty=1 because counter is unreadable; "
            "operator should investigate system_settings."
        )
        return (1, True)
    if remaining <= 0:
        return (max(1, int(intended_quantity)), False)
    return (1, True)


# ---------------------------------------------------------------------------
# Settlement-side: decrement only when a *canary* trade settles
# ---------------------------------------------------------------------------


def is_canary_trade(trade_metadata: Any) -> bool:
    """Whether ``trade_metadata`` carries the canary flag we stamp on
    placement.  Tolerant of None/non-dict shapes so a malformed
    metadata blob doesn't crash settlement."""
    if not isinstance(trade_metadata, dict):
        return False
    raw = trade_metadata.get("canary")
    if raw is True:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes")
    if isinstance(raw, (int, float)):
        return raw != 0
    return False


async def decrement_on_settle(trade: dict[str, Any]) -> bool:
    """Decrement the canary counter iff ``trade`` was placed under canary.

    Returns True if a decrement was applied; False otherwise (non-canary
    trade, counter already zero, or DB error).  Safe to call on every
    settled trade — the metadata check short-circuits.
    """
    if not is_canary_trade(trade.get("metadata")):
        return False
    # Atomic decrement floored at zero.  We do an UPDATE that pulls the
    # current value, decrements via a CTE, and refuses to go below 0
    # so a race between two settlements can't drive remaining negative.
    try:
        new_val = await db.fetchval(
            """
            UPDATE system_settings
               SET value = (
                       greatest(0, COALESCE(NULLIF(value, '')::int, 0) - 1)
                   )::text,
                   updated_at = now()
             WHERE key = $1
            RETURNING value
            """,
            _KEY_REMAINING,
        )
    except Exception as e:
        log.warning(
            "[canary] decrement failed for trade %s: %s",
            trade.get("id"), e,
        )
        return False
    log.info(
        "canary decrement: trade=%s new_remaining=%s",
        trade.get("id"), new_val,
    )
    return True
