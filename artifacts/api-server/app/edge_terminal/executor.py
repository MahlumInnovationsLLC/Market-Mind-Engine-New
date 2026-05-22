"""Drain pending Edge Terminal signals → fire via paper.place_order.

Quarter-Kelly sizing off the current bankroll.  Uses the same
:func:`paper.place_order` chokepoint the LLM pipeline does so the
audit trail (lifecycle, slippage guard, trade row insert) is
identical — the only divergence is the ``placed_by='edge_terminal'``
tag we stamp on the resulting trade row.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import db
from ..config import get_settings
from ..orchestrator import cycle as cycle_module
from ..orchestrator import paper as paper_module
from ..risk.gates import RiskContext
from . import market_resolver


log = logging.getLogger(__name__)


# Quarter-Kelly: drop directly into the executor regardless of the
# operator-tunable cap that the pipeline uses, because Edge Terminal
# is operator-curated and we want a predictable, conservative size
# per signal.
KELLY_FRACTION = 0.25
# Hard ceiling so a single mis-priced signal can't blow up the book.
MAX_NOTIONAL_PCT_OF_BANKROLL = 0.05


async def drain_pending(limit: int = 50) -> dict[str, int]:
    """Fire every pending signal, return counts."""
    rows = await db.fetch(
        """
        SELECT id, vertical, matchup, selection, signal_date,
               edge_pct, model_prob, market_price,
               kalshi_ticker, market_title
        FROM edge_terminal_signals
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT $1
        """,
        int(limit),
    )
    placed = 0
    failed = 0
    skipped = 0
    for r in rows:
        sig = dict(r)
        sid = int(sig["id"])
        try:
            outcome = await _fire_one(sig)
        except Exception as e:
            log.exception("edge_terminal fire failed for id=%s: %s", sid, e)
            await _mark(sid, "failed", str(e))
            failed += 1
            continue
        if outcome is None:
            await _mark(sid, "skipped", "no matching kalshi market")
            skipped += 1
            continue
        await _mark(
            sid, "placed", None,
            trades_id=outcome.get("trades_id"),
        )
        placed += 1
    return {"placed": placed, "failed": failed, "skipped": skipped}


async def _fire_one(sig: dict[str, Any]) -> dict[str, Any] | None:
    ticker = await market_resolver.resolve(sig)
    if not ticker:
        return None

    # Pricing — prefer scraped market_price, else assume a near-the-
    # money mid so the lifecycle's clamp keeps us in [0.01, 0.99].
    price = float(sig.get("market_price") or 0.50)
    price = max(0.01, min(0.99, price))

    model_prob = sig.get("model_prob")
    if model_prob is None and sig.get("edge_pct") is not None:
        # If only edge% is known, infer p from edge: edge ≈ p - price.
        try:
            model_prob = max(0.01, min(0.99, price + float(sig["edge_pct"]) / 100.0))
        except (TypeError, ValueError):
            model_prob = None

    # Match paper.place_order's effective-mode resolution so sizing
    # uses the live bankroll when the operator has flipped live mode.
    effective_mode = (get_settings().trading_mode or "paper").strip().lower()
    if effective_mode != "live":
        effective_mode = "paper"
    bankroll = await cycle_module._latest_bankroll(trading_mode=effective_mode) or 0.0
    quantity, kelly_fraction = _quarter_kelly_quantity(
        bankroll=bankroll,
        model_prob=model_prob,
        price=price,
    )
    if quantity < 1:
        return None

    edge_signed = None
    if model_prob is not None:
        edge_signed = float(model_prob) - price
    ctx = RiskContext(
        market_id=ticker,
        side="YES",
        vertical=sig.get("vertical") or "sports",
        league=None,
        market_type=None,
        matchup=sig.get("matchup"),
        current_price=price,
        model_probability=model_prob,
        edge=edge_signed,
        confidence_band_low=None,
        confidence_band_high=None,
        agreement_rate=None,
        swarm_approved=True,
        swarm_recommended_action="BUY_YES",
        debate_id=None,
        intended_quantity=quantity,
        intended_notional_usd=quantity * price,
        bankroll_usd=bankroll,
        open_positions_count=0,
        open_positions_notional_usd=0.0,
        open_positions_per_vertical_usd={},
        operator_overrides={},
        market_cooldown_until=None,
    )
    cand = {
        "kalshi_price": price,
        "source": "edge_terminal",
    }
    result = await paper_module.place_order(
        ctx,
        debate_id=None,
        consensus=model_prob,
        edge=sig.get("edge_pct"),
        kelly=kelly_fraction,
        exchange="kalshi",
        cand=cand,
    )
    # Re-tag the trade row as edge_terminal so the dashboard filter
    # can separate sources.  paper.place_order hard-codes
    # placed_by='orchestrator'; we patch via a follow-up UPDATE since
    # threading a parameter through the entire dispatcher is invasive
    # for a single use-case.
    trades_id = result.get("trades_id")
    if trades_id:
        try:
            await db.execute(
                "UPDATE trades SET placed_by = 'edge_terminal' WHERE id = $1",
                int(trades_id),
            )
        except Exception as e:  # pragma: no cover — defensive
            log.warning(
                "edge_terminal placed_by retag failed (trade_id=%s): %s",
                trades_id, e,
            )
    return result


def _quarter_kelly_quantity(
    *,
    bankroll: float,
    model_prob: float | None,
    price: float,
) -> tuple[int, float]:
    """Return (quantity, fractional kelly) for the YES side.

    Returns (0, 0.0) when the math is unfavourable (kelly <=0) or the
    bankroll is empty.
    """
    if bankroll <= 0 or model_prob is None or price <= 0 or price >= 1:
        return 0, 0.0
    p = float(model_prob)
    b = (1.0 - price) / price
    q = 1.0 - p
    kelly = (b * p - q) / b
    if kelly <= 0:
        return 0, 0.0
    notional_cap = bankroll * MAX_NOTIONAL_PCT_OF_BANKROLL
    notional = min(bankroll * kelly * KELLY_FRACTION, notional_cap)
    qty = int(notional / price)
    return max(0, qty), kelly * KELLY_FRACTION


async def _mark(
    sid: int,
    status: str,
    reason: str | None,
    *,
    trades_id: int | None = None,
) -> None:
    await db.execute(
        """
        UPDATE edge_terminal_signals
        SET status = $2,
            status_reason = $3,
            trades_id = $4,
            placed_at = CASE WHEN $2 = 'placed' THEN now() ELSE placed_at END,
            updated_at = now()
        WHERE id = $1
        """,
        sid, status, reason, trades_id,
    )
