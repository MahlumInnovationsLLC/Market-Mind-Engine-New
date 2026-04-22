"""Auto-promote logic for the elections Kelly multiplier (task #201).

The elections vertical ships with a deliberately conservative Kelly
multiplier (0.05 — paper-grade) until the live polling-driven model
proves itself.  This module measures the rolling Brier on the most
recent settled elections trades and decides whether the vertical is
eligible to be promoted to a still-conservative Kelly multiplier
(0.08 — distinct from tech-AI's 0.10 because election markets
historically resolve in clusters that punish over-sizing).

The actual write to ``system_settings`` is gated behind an admin
endpoint (``POST /api/admin/elections/promote-kelly``) so promotion
is deliberate, auditable, and reversible.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import db


log = logging.getLogger(__name__)


SETTLED_THRESHOLD = 30
BRIER_THRESHOLD = 0.20
DEFAULT_MULTIPLIER = 0.05
PROMOTED_MULTIPLIER = 0.08
SETTING_KEY = "risk.kelly_multiplier_elections"
VERTICAL = "elections"


@dataclass(frozen=True)
class PromotionStatus:
    settled_count: int
    brier: float | None
    eligible: bool
    current_multiplier: float
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "settled_count": self.settled_count,
            "brier": self.brier,
            "eligible": self.eligible,
            "current_multiplier": self.current_multiplier,
            "reason": self.reason,
        }


async def _read_current_multiplier() -> float:
    """Return the *currently effective* elections Kelly multiplier.

    Falls through to the spec default (0.05) if no override row exists.
    """
    try:
        row = await db.fetchval(
            "SELECT value FROM system_settings WHERE key = $1",
            SETTING_KEY,
        )
        if row is not None:
            return float(row)
    except Exception as e:
        log.debug("read current multiplier failed: %s", e)
    return DEFAULT_MULTIPLIER


async def evaluate_promotion() -> PromotionStatus:
    """Compute the rolling Brier on settled elections trades.

    Eligible iff:
      * settled_count >= SETTLED_THRESHOLD (30)
      * brier < BRIER_THRESHOLD (0.20)
      * current multiplier < PROMOTED_MULTIPLIER (0.08)
    """
    current = await _read_current_multiplier()
    try:
        agg = await db.fetchrow(
            """
            WITH settled AS (
              SELECT
                t.model_prob::float                 AS p_yes,
                lower(coalesce(t.side, ''))         AS side_lc,
                pm.actual_outcome::int              AS y
                FROM trades t
                JOIN trade_postmortems pm
                  ON pm.trade_id = coalesce(t.trade_id, t.id::text)
               WHERE t.vertical = $1
                 AND t.settled_at IS NOT NULL
                 AND t.model_prob IS NOT NULL
                 AND pm.actual_outcome IS NOT NULL
               ORDER BY t.settled_at DESC
               LIMIT 200
            ), aligned AS (
              SELECT
                CASE WHEN side_lc IN ('no','n','sell') THEN 1.0 - p_yes ELSE p_yes END AS p_side,
                CASE WHEN side_lc IN ('no','n','sell') THEN 1 - y       ELSE y       END AS y_side
                FROM settled
            )
            SELECT count(*)::int                                       AS n,
                   avg((p_side - y_side) * (p_side - y_side))::float   AS brier
              FROM aligned
            """,
            VERTICAL,
        )
    except Exception as e:
        log.debug("promotion evaluate failed: %s", e)
        agg = None

    n = int((agg or {}).get("n", 0) or 0) if agg else 0
    brier_raw = (agg or {}).get("brier") if agg else None
    brier = float(brier_raw) if brier_raw is not None else None

    if current >= PROMOTED_MULTIPLIER:
        return PromotionStatus(
            settled_count=n, brier=brier, eligible=False,
            current_multiplier=current,
            reason=f"already at or above {PROMOTED_MULTIPLIER}",
        )
    if n < SETTLED_THRESHOLD:
        return PromotionStatus(
            settled_count=n, brier=brier, eligible=False,
            current_multiplier=current,
            reason=f"need {SETTLED_THRESHOLD} settled trades, have {n}",
        )
    if brier is None or brier >= BRIER_THRESHOLD:
        return PromotionStatus(
            settled_count=n, brier=brier, eligible=False,
            current_multiplier=current,
            reason=f"Brier {brier!r} >= threshold {BRIER_THRESHOLD}",
        )
    return PromotionStatus(
        settled_count=n, brier=brier, eligible=True,
        current_multiplier=current,
        reason="eligible",
    )


async def promote(actor_user_id: str | None = None,
                  actor_email: str | None = None) -> PromotionStatus:
    """Write the production multiplier into ``system_settings``.

    Refuses unless the latest evaluation reports ``eligible=True``.
    Returns a fresh `PromotionStatus` after the write.
    """
    status = await evaluate_promotion()
    if not status.eligible:
        return status
    await db.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET
          value = EXCLUDED.value,
          updated_at = EXCLUDED.updated_at
        """,
        SETTING_KEY, str(PROMOTED_MULTIPLIER),
    )
    try:
        import json
        await db.execute(
            """
            INSERT INTO admin_audit_log (
                actor_user_id, actor_email,
                target_user_id, target_email,
                action, before_value, after_value
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
            """,
            actor_user_id or "system", actor_email,
            "system", None, "promote_elections_kelly",
            json.dumps({"multiplier": status.current_multiplier}),
            json.dumps({"multiplier": PROMOTED_MULTIPLIER,
                        "settled_count": status.settled_count,
                        "brier": status.brier}),
        )
    except Exception as e:
        log.warning("audit log insert (promote elections kelly) failed: %s", e)
    return await evaluate_promotion()
