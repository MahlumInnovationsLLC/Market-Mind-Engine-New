"""Auto-promote logic for the entertainment Kelly multiplier (task #203).

Ships at Kelly multiplier 0.05 (paper-grade, lower than tech-AI's 0.06
because subjective awards votes resolve in correlated clusters).
Promotion bumps it to 0.08 once the model proves itself: 30 settled
trades and a rolling Brier < 0.22.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import db


log = logging.getLogger(__name__)


SETTLED_THRESHOLD = 30
BRIER_THRESHOLD = 0.22
DEFAULT_MULTIPLIER = 0.05
PROMOTED_MULTIPLIER = 0.08
SETTING_KEY = "risk.kelly_multiplier_entertainment"
VERTICAL = "entertainment"


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
            "system", None, "promote_entertainment_kelly",
            json.dumps({"multiplier": status.current_multiplier}),
            json.dumps({"multiplier": PROMOTED_MULTIPLIER,
                        "settled_count": status.settled_count,
                        "brier": status.brier}),
        )
    except Exception as e:
        log.warning("audit log insert (promote entertainment kelly) failed: %s", e)
    return await evaluate_promotion()
