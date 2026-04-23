"""Venue router: pick which exchange(s) to route an order to.

The orchestrator used to be hard-wired to Kalshi. Now that a Polymarket
lifecycle exists with the same shape (`app.polymarket.lifecycle.submit_order`),
the orchestrator consults this router per candidate to pick the venue with the
better price/liquidity — or fan out to both.

Modes (operator-controlled via ``AXIOM_VENUE_ROUTING``):

  - ``kalshi``      route every order to Kalshi (default — preserves prior
                    behavior end-to-end).
  - ``polymarket``  route every order to Polymarket.
  - ``both``        fan out to both venues in parallel.
  - ``auto``        pick the venue with the better quoted price/liquidity for
                    the candidate's side. Falls back to Kalshi when the
                    candidate carries no Polymarket signal at all (the common
                    case today, since most feeds are Kalshi-only).

The router is intentionally pure: it takes a candidate dict + mode and returns
a list of exchange names. No DB / network access — that keeps it trivial to
unit-test and safe to call inside the hot per-cycle loop.
"""
from __future__ import annotations

from typing import Iterable

from ..config import get_settings


VALID_MODES: tuple[str, ...] = ("kalshi", "polymarket", "both", "auto")
KALSHI = "kalshi"
POLYMARKET = "polymarket"


def _coerce_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _candidate_field(cand: dict, *names: str) -> object:
    """Return the first present value across ``names`` (top-level only)."""
    for n in names:
        if n in cand and cand[n] is not None:
            return cand[n]
    return None


def _pick_auto(cand: dict, *, side: str | None = None) -> list[str]:
    """Pick the better venue for ``cand`` using whatever signal is present.

    Selection rules, in order:
      1. If only one venue has a quoted price for the candidate, route to it.
      2. If both venues have prices, prefer the venue that gives a cheaper
         entry for the candidate's side (lower price for YES buys, higher
         price for NO buys — both reduce up-front cost per contract).
      3. On a price tie, prefer the venue with the larger liquidity figure
         when present.
      4. Fall back to Kalshi (the historical default) when no signal is
         available on either side.
    """
    kalshi_price = _coerce_float(
        _candidate_field(cand, "kalshi_price", "current_price")
    )
    poly_price = _coerce_float(
        _candidate_field(cand, "polymarket_price")
    )

    has_kalshi = kalshi_price is not None or cand.get("kalshi_market_id")
    has_poly = poly_price is not None or cand.get("polymarket_market_id")

    if has_poly and not has_kalshi:
        return [POLYMARKET]
    if has_kalshi and not has_poly:
        return [KALSHI]
    if not has_kalshi and not has_poly:
        return [KALSHI]

    # Both venues available — compare prices for the candidate's side.
    if kalshi_price is not None and poly_price is not None:
        side_norm = (side or "YES").upper()
        if side_norm == "YES":
            # Cheaper YES = lower price.
            if poly_price < kalshi_price:
                return [POLYMARKET]
            if kalshi_price < poly_price:
                return [KALSHI]
        else:
            # Cheaper NO = higher YES price (NO = 1 - YES).
            if poly_price > kalshi_price:
                return [POLYMARKET]
            if kalshi_price > poly_price:
                return [KALSHI]

    # Tie on price — break with liquidity if either side reports it.
    kalshi_liq = _coerce_float(
        _candidate_field(cand, "kalshi_liquidity", "liquidity")
    )
    poly_liq = _coerce_float(_candidate_field(cand, "polymarket_liquidity"))
    if kalshi_liq is not None or poly_liq is not None:
        if (poly_liq or 0.0) > (kalshi_liq or 0.0):
            return [POLYMARKET]
        if (kalshi_liq or 0.0) > (poly_liq or 0.0):
            return [KALSHI]

    # No way to discriminate — keep historical default.
    return [KALSHI]


def normalize_mode(mode: str | None) -> str:
    """Normalize an operator-supplied mode, raising on unknown values."""
    norm = (mode or "kalshi").strip().lower()
    if norm not in VALID_MODES:
        raise ValueError(
            f"venue_routing must be one of {VALID_MODES} (got {mode!r})"
        )
    return norm


def _has_polymarket_signal(cand: dict) -> bool:
    return bool(
        cand.get("polymarket_market_id")
        or cand.get("polymarket_condition_id")
        or cand.get("polymarket_token_id")
        or cand.get("polymarket_price") is not None
    )


def _has_kalshi_signal(cand: dict) -> bool:
    return bool(
        cand.get("kalshi_market_id")
        or cand.get("kalshi_price") is not None
        or cand.get("current_price") is not None
    )


def route_for_candidate(
    cand: dict,
    *,
    mode: str | None = None,
    side: str | None = None,
) -> list[str]:
    """Return the ordered list of exchanges to route ``cand`` to.

    ``mode`` overrides the operator setting (used by tests and by callers
    that want to force a venue). ``side`` is the YES/NO direction the
    orchestrator intends to place — only relevant for ``auto``.

    In ``both`` mode we drop a venue that has no signal on the candidate
    (e.g. Polymarket when only Kalshi mapped this market), so the cycle
    doesn't waste a submission on a guaranteed lifecycle reject.
    """
    if mode is None:
        mode = getattr(get_settings(), "venue_routing", "kalshi")
    norm = normalize_mode(mode)
    if norm == KALSHI:
        return [KALSHI]
    if norm == POLYMARKET:
        return [POLYMARKET]
    if norm == "both":
        venues = []
        if _has_kalshi_signal(cand):
            venues.append(KALSHI)
        if _has_polymarket_signal(cand):
            venues.append(POLYMARKET)
        # Fall back to Kalshi-only when neither side has signal so we
        # never return an empty venue list (the cycle would treat that
        # as a place_error).
        return venues or [KALSHI]
    return _pick_auto(cand, side=side)


def dedupe(venues: Iterable[str]) -> list[str]:
    """Order-preserving dedupe used by the cycle when callers stack venues."""
    seen: set[str] = set()
    out: list[str] = []
    for v in venues:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out
