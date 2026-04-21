"""Paper-mode order lifecycle.

Writes a row into `trades` with trading_mode='paper' and exchange='kalshi'
(so dashboards that filter on exchange continue to surface paper rows).
Live-mode submission is OUT OF SCOPE for this task — when the operator
flips AXIOM_TRADING_MODE=live without the Kalshi client (#4) being
present, this module raises so the cycle records the failure rather
than silently submitting nothing.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from .. import db
from ..config import get_settings
from ..risk.gates import RiskContext


log = logging.getLogger(__name__)


def _gen_paper_trade_id() -> str:
    # Distinguish paper trade ids from live ones at a glance.
    return f"paper_{int(datetime.now(timezone.utc).timestamp())}_{secrets.token_hex(4)}"


async def place_order(
    ctx: RiskContext,
    *,
    debate_id: int | None,
    consensus: float | None,
    edge: float | None,
    kelly: float | None,
) -> dict[str, Any]:
    """Place a paper trade.  Returns a dict the orchestrator can publish
    on the pipeline stream and persist alongside the cycle record.

    Raises NotImplementedError when AXIOM_TRADING_MODE=live (until the
    Kalshi client lands in task #4).  RuntimeError on DB write failure
    so the cycle counts the trade as an error rather than a silent no-op.
    """
    settings = get_settings()
    mode = (settings.trading_mode or "paper").lower()
    if mode == "live":
        # Important: do NOT silently fall back to paper here — operators
        # who set live=ON expect submission attempts (or a clear failure).
        raise NotImplementedError(
            "AXIOM_TRADING_MODE=live but the Kalshi client (task #4) is not yet "
            "available; refusing to silently degrade to paper-mode"
        )

    trade_id = _gen_paper_trade_id()
    action = "buy"
    side_norm = "yes" if ctx.side.upper() == "YES" else "no"
    quantity = max(1, int(ctx.intended_quantity))
    price = float(ctx.current_price or 0.0)

    try:
        new_id = await db.fetchval(
            """
            INSERT INTO trades (
                trade_id, market_id, vertical, side, action,
                quantity, price, fees, pnl,
                trading_mode, exchange, executed_at,
                model_prob, model_edge, kelly_fraction
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, 0, NULL,
                'paper', 'kalshi', now(),
                $8, $9, $10
            )
            RETURNING id
            """,
            trade_id,
            ctx.market_id,
            ctx.vertical or "sports",
            side_norm,
            action,
            quantity,
            price,
            consensus,
            edge,
            kelly,
        )
    except Exception as e:
        log.error("paper trade insert failed: %s", e)
        raise RuntimeError(f"paper trade insert failed: {e}") from e

    return {
        "trade_id": trade_id,
        "trades_id": int(new_id) if new_id is not None else None,
        "market_id": ctx.market_id,
        "side": side_norm,
        "quantity": quantity,
        "price": price,
        "trading_mode": "paper",
        "debate_id": debate_id,
    }
