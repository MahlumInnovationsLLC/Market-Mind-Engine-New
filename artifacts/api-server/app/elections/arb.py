"""Kalshi vs Polymarket arbitrage detector for the elections vertical.

Used by the dashboard side-by-side panel and the prediction_market_arbitrage
swarm persona to flag spreads worth acting on.

A spread is reported as ``arb`` when the absolute Kalshi-vs-Polymarket
probability gap exceeds the configured threshold (default 4pp) AND
both sides have observable prices.  The detector is intentionally
side-agnostic — the consumer decides which side is the buy.
"""
from __future__ import annotations

from dataclasses import dataclass


# Default spread (in absolute probability units) above which the
# detector flags an arb.  Operators can tune via
# `risk.elections_arb_threshold_pp` in `system_settings`.
DEFAULT_ARB_THRESHOLD_PP = 4.0


@dataclass(frozen=True)
class ArbOpportunity:
    market_id: str
    candidate: str
    kalshi_price: float | None
    polymarket_price: float | None
    spread_pp: float | None
    arb: bool
    direction: str  # "kalshi_buy" | "polymarket_buy" | "none"
    threshold_pp: float

    def as_dict(self) -> dict[str, object]:
        return {
            "market_id": self.market_id,
            "candidate": self.candidate,
            "kalshi_price": self.kalshi_price,
            "polymarket_price": self.polymarket_price,
            "spread_pp": self.spread_pp,
            "arb": self.arb,
            "direction": self.direction,
            "threshold_pp": self.threshold_pp,
        }


def detect_arb(
    *,
    market_id: str,
    candidate: str,
    kalshi_price: float | None,
    polymarket_price: float | None,
    threshold_pp: float = DEFAULT_ARB_THRESHOLD_PP,
) -> ArbOpportunity:
    """Compute the spread + arb verdict for a Kalshi/Polymarket pair.

    `kalshi_price` / `polymarket_price` are decimal probabilities
    in [0, 1].  When either is missing the result reports `arb=False`
    and `spread_pp=None` so the dashboard renders a "missing leg"
    state without flapping.

    `direction` reports which side to BUY when the arb fires:
        * "kalshi_buy"     — Kalshi is cheaper (under-priced YES).
        * "polymarket_buy" — Polymarket is cheaper.
        * "none"           — no arb / missing leg.
    """
    if kalshi_price is None or polymarket_price is None:
        return ArbOpportunity(
            market_id=market_id,
            candidate=candidate,
            kalshi_price=kalshi_price,
            polymarket_price=polymarket_price,
            spread_pp=None,
            arb=False,
            direction="none",
            threshold_pp=threshold_pp,
        )
    spread = (float(polymarket_price) - float(kalshi_price)) * 100.0
    arb = abs(spread) >= float(threshold_pp)
    if not arb:
        direction = "none"
    elif spread > 0:
        direction = "kalshi_buy"  # Kalshi is cheaper than Polymarket
    else:
        direction = "polymarket_buy"
    return ArbOpportunity(
        market_id=market_id,
        candidate=candidate,
        kalshi_price=float(kalshi_price),
        polymarket_price=float(polymarket_price),
        spread_pp=round(spread, 2),
        arb=arb,
        direction=direction,
        threshold_pp=float(threshold_pp),
    )
