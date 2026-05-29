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

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .. import db


log = logging.getLogger("axiom.risk.canary")


_KEY_REMAINING = "risk.canary_remaining"
_KEY_INIT_AT = "risk.canary_init_at"
_KEY_RAMP_REMAINING = "risk.ramp_remaining"


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
# Ramp ladder config (Task #952) — the stage *after* the qty=1 canary.
#
# The canary proves the live path works at qty=1; the ramp then steps the
# allowed size up in tiers (default 5 -> 20 -> full) instead of jumping
# straight from 1 to full Kelly size.  Each tier lasts ``AXIOM_RAMP_TIER_
# TRADES`` *settled* live trades, so we only graduate to a bigger cap after
# actually observing settlements at the current cap.
# ---------------------------------------------------------------------------


def _ramp_caps() -> list[int]:
    """Ascending list of per-tier quantity ceilings for the post-canary
    ramp.  ``AXIOM_RAMP_CAPS`` is a comma list (default ``5,20``);
    garbage / non-positive entries are dropped and the result is sorted
    and de-duped.  Always returns at least one cap."""
    raw = os.environ.get("AXIOM_RAMP_CAPS")
    if not raw:
        return [5, 20]
    caps: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except (ValueError, TypeError):
            continue
        if v > 0:
            caps.append(v)
    caps = sorted(set(caps))
    return caps or [5, 20]


def _ramp_tier_trades() -> int:
    """Settled live trades required to graduate one ramp tier.

    ``AXIOM_RAMP_TIER_TRADES`` (default 3); non-positive / garbage falls
    back to 3."""
    raw = os.environ.get("AXIOM_RAMP_TIER_TRADES")
    if raw is None:
        return 3
    try:
        v = int(raw)
        return v if v >= 1 else 3
    except (ValueError, TypeError):
        return 3


def _ramp_total() -> int:
    """Total settled live trades the ramp spans (all tiers combined)."""
    return len(_ramp_caps()) * _ramp_tier_trades()


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


async def ensure_ramp_initialized() -> None:
    """Idempotently seed the post-canary ramp ceiling.

    Writes ``risk.ramp_remaining = _ramp_total()`` only if the row does
    not already exist (``DO NOTHING``), so it is safe to call on every
    boot and on every live cutover.  A system that went live *before* the
    ramp landed (Task #952) — i.e. the row is missing — picks up the ramp
    budget the first time this runs, which is what lets the placement-side
    fail-closed branch (smallest cap on a missing row) recover into a
    proper, decrementing ladder instead of staying pinned at tier 0.

    Raises on DB failure so an operator-facing caller can surface it; the
    boot caller swallows the exception (best effort).
    """
    await db.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO NOTHING
        """,
        _KEY_RAMP_REMAINING,
        str(_ramp_total()),
    )


async def ensure_initialized() -> int:
    """Idempotently initialize the canary counter.

    On first call writes ``AXIOM_CANARY_TRADES`` (default 5) to
    ``system_settings`` and stamps ``risk.canary_init_at``.  Subsequent
    calls observe the existing value and return it unchanged.

    Returns the current counter (post-init).  Raises on DB failure so
    the caller (operator endpoint) can surface a 502.
    """
    # Seed the post-canary ramp ceiling regardless of whether the canary
    # counter already exists — a system that initialized the canary before
    # the ramp landed (Task #952) must still pick up the ramp budget.
    await ensure_ramp_initialized()
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
        json.dumps(now),
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
        json.dumps(now),
    )
    # Restart the post-canary ramp ceiling too so a re-flip gets the full
    # qty=1 -> tier -> full graduation, not just the qty=1 canary.
    await db.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at
        """,
        _KEY_RAMP_REMAINING,
        str(_ramp_total()),
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


async def ramp_quantity_for_live(intended_quantity: int) -> tuple[int, bool]:
    """Apply the post-canary ramp ceiling to a live order.

    Called only *after* the qty=1 canary has lifted.  Returns
    ``(qty, ramp_active)``:

    * If ``ramp_active`` is True the caller MUST stamp
      ``metadata.ramp = True`` so the settlement pipeline decrements
      ``risk.ramp_remaining`` once the trade settles — even when the
      intended quantity was already under the current tier ceiling (the
      tier still has to *graduate* on observed settlements).
    * If ``ramp_active`` is False, ``qty == intended_quantity``.

    The ceiling grows as live trades settle: with the default ladder
    (``5,20`` over 3 trades/tier) sizing graduates qty=1 -> 5 -> 20 ->
    full instead of jumping straight from the canary to full Kelly size.

    Failure bias — every "can't read a trustworthy positive remaining"
    case fails **closed** to the smallest cap, because this function only
    runs on a *live* order (the canary has already lifted) where a silent
    full-size trade is the expensive mistake:

    * Never-seeded row (``None`` value) -> ``(min(intended, smallest
      cap), True)``.  A system that went live before the ramp landed has
      no row; rather than letting full-size orders through we cap to the
      smallest tier.  Boot/cutover seeding (:func:`ensure_ramp_initialized`)
      then creates the row so the ladder can actually graduate.
    * DB read error / non-integer value -> ``(min(intended, smallest
      cap), True)``: a transient outage can only *narrow* sizing.

    The only path back to full size is an explicit, persisted
    ``remaining <= 0`` — i.e. the ramp ran to completion.
    """
    caps = _ramp_caps()
    try:
        v = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = $1",
            _KEY_RAMP_REMAINING,
        )
    except Exception as e:
        log.warning("[ramp] read failed, biasing to smallest cap: %s", e)
        return (min(max(1, int(intended_quantity)), caps[0]), True)
    if v is None:
        log.warning(
            "[ramp] counter row missing on a live order; capping to "
            "smallest tier (%s) until seeding creates the row.", caps[0],
        )
        return (min(max(1, int(intended_quantity)), caps[0]), True)
    try:
        remaining = int(v)
    except (ValueError, TypeError):
        log.warning("[ramp] non-integer value in system_settings: %r", v)
        return (min(max(1, int(intended_quantity)), caps[0]), True)
    if remaining <= 0:
        return (max(1, int(intended_quantity)), False)
    tier_trades = _ramp_tier_trades()
    total = len(caps) * tier_trades
    consumed = max(0, total - remaining)
    tier_index = min(consumed // tier_trades, len(caps) - 1)
    cap = caps[tier_index]
    return (min(max(1, int(intended_quantity)), cap), True)


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


def is_ramp_trade(trade_metadata: Any) -> bool:
    """Whether ``trade_metadata`` carries the post-canary ramp flag we
    stamp on placement.  Same tolerant coercion as
    :func:`is_canary_trade`."""
    if not isinstance(trade_metadata, dict):
        return False
    raw = trade_metadata.get("ramp")
    if raw is True:
        return True
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes")
    if isinstance(raw, (int, float)):
        return raw != 0
    return False


async def decrement_ramp_on_settle(trade: dict[str, Any]) -> bool:
    """Decrement the ramp counter iff ``trade`` was placed under the ramp.

    Mirrors :func:`decrement_on_settle` but targets ``risk.ramp_remaining``
    and the ``metadata.ramp`` flag.  Returns True iff a decrement was
    applied.  Safe to call on every settled trade — the metadata check
    short-circuits non-ramp trades.
    """
    if not is_ramp_trade(trade.get("metadata")):
        return False
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
            _KEY_RAMP_REMAINING,
        )
    except Exception as e:
        log.warning(
            "[ramp] decrement failed for trade %s: %s",
            trade.get("id"), e,
        )
        return False
    log.info(
        "ramp decrement: trade=%s new_remaining=%s",
        trade.get("id"), new_val,
    )
    return True
