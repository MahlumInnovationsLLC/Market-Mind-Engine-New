"""Honest paper-fill simulator.

Replaces the legacy paper-mode short-circuit in
``app.kalshi.lifecycle._submit_after_dedupe`` (which filled every paper
order at the limit price for 100% of the requested quantity) with a
realistic walk over the live Kalshi half-book.

Why
---
The 100%-at-limit shortcut inflated paper-mode Brier scores by 5–15 %
relative to live: every paper trade got the *best* feasible price plus
the *full* requested size, regardless of resting depth. The
``online_learning_buffer`` then trained the live calibrator on those
optimistic numbers, so live performance under-shot dashboards on cutover.

Inputs / outputs
----------------
This module is intentionally pure and synchronous: it takes a snapshot of
the half-book (the same shape that
:meth:`app.kalshi.orderbook.OrderbookCache.snapshot` returns) plus the
order request, and returns ``PaperFillResult``. No DB writes, no event
emits, no module-level state — the lifecycle module is responsible for
sequencing audit rows and bus events. Keeping this layer side-effect-free
makes the unit tests below trivial and lets backtests drive the same code
path with a synthetic book.

Binary-book transform
---------------------
Kalshi quotes a binary contract as two half-books, both expressed as
RESTING BIDS. A taker who wants to BUY a side has to cross the OPPOSITE
half-book (yes_price + no_price = 100¢ on a binary contract):

* BUY YES of N → walk the no-book; per-contract cost = ``100 - no_bid_price``.
* BUY NO of N  → walk the yes-book; per-contract cost = ``100 - yes_bid_price``.
* SELL YES of N → walk the yes-book directly; receive ``yes_bid_price``.
* SELL NO of N  → walk the no-book directly; receive ``no_bid_price``.

In every case the taker eats the *highest* resting bid first (best price
for the resting side ⇒ best price for the taker), so we walk levels
sorted descending by bid price and convert to taker units when the action
is BUY.

Limit-price semantics
---------------------
* For BUY: ``limit_price_cents`` is the highest per-contract cost the
  taker is willing to pay. Levels whose taker cost exceeds the limit are
  not consumed; the remaining requested quantity becomes the residual.
* For SELL: ``limit_price_cents`` is the lowest per-contract proceeds the
  taker is willing to accept. Levels below the floor are not consumed.
* ``None`` means "market order" — walk to whatever depth is required.

Slippage tolerance
------------------
``slippage_tolerance_cents`` is an OPTIONAL ceiling on how far the
weighted-average fill price may drift from the *top-of-book* level the
order touched. The reference price is the BEST level walked (lowest
taker cost for BUY, highest proceeds for SELL), NOT the limit price —
because VWAP can never exceed a hard limit (we don't walk past it),
so a limit-relative gate would always be inert. When the projected
VWAP slips past ``best ± tolerance`` the simulator returns a zero-fill
rejection; the lifecycle layer maps that to a ``rejected`` lifecycle
row with ``reason='paper_slippage_exceeded'``. Defaults to ``None``
(no slippage gate beyond the limit-price ceiling).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


__all__ = ["PaperFillResult", "PaperFillStatus", "simulate_paper_fill"]


Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
PaperFillStatus = Literal[
    "filled",
    "partial",
    "no_book",
    "no_liquidity",
    "limit_unfillable",
    "slippage_exceeded",
]


@dataclass(frozen=True)
class PaperFillResult:
    """Outcome of one simulated paper order against a half-book snapshot.

    Attributes
    ----------
    filled_qty
        Contracts the simulator was able to consume. ``0`` for any
        rejection-shaped status.
    vwap_price_cents
        Volume-weighted average per-contract price actually paid (BUY)
        or received (SELL), expressed in the taker's units (yes-cents
        for ``side='yes'``, no-cents for ``side='no'``). ``None`` when
        ``filled_qty == 0`` because no level was crossed.
    residual_qty
        ``requested_count - filled_qty``. For ``status='filled'`` this
        is always ``0``.
    status
        Categorical reason for the outcome — see ``PaperFillStatus``.
        Used by the lifecycle layer to pick the right ``rejected`` /
        ``filled`` event and metadata.
    levels_walked
        ``[(price_cents_in_taker_units, contracts_taken), ...]`` ordered
        from best to worst price. Empty when nothing was filled. The
        VWAP can be reconstructed as
        ``sum(p*q for p, q in levels_walked) / filled_qty``.
    """

    filled_qty: int
    vwap_price_cents: int | None
    residual_qty: int
    status: PaperFillStatus
    levels_walked: list[tuple[int, int]] = field(default_factory=list)


def _select_half_book(
    book: dict[str, dict[int, int]] | None,
    *,
    action: Action,
    side: Side,
) -> tuple[dict[int, int] | None, bool]:
    """Pick the half-book a taker would actually cross.

    Returns ``(levels_dict, cross)`` where ``cross=True`` means the
    resting prices need the ``100 - p`` binary transform to express
    them in the taker's per-contract cost units (BUY case). On invalid
    inputs returns ``(None, False)``.
    """
    if book is None:
        return None, False
    if side not in ("yes", "no") or action not in ("buy", "sell"):
        return None, False
    if action == "buy":
        # BUY this side -> cross the OPPOSITE half-book.
        opposite = "no" if side == "yes" else "yes"
        return dict(book.get(opposite) or {}), True
    # SELL this side -> hit the same-side resting bids directly.
    return dict(book.get(side) or {}), False


def simulate_paper_fill(
    book: dict[str, dict[int, int]] | None,
    *,
    action: Action,
    side: Side,
    count: int,
    limit_price_cents: int | None,
    slippage_tolerance_cents: int | None = None,
) -> PaperFillResult:
    """Walk the half-book and project an honest paper fill.

    Returns a :class:`PaperFillResult`. See module docstring for the
    binary-book transform, limit-price semantics, and slippage rules.

    Parameters
    ----------
    book
        Snapshot of the form ``{"yes": {price_cents: size}, "no": {...}}``
        as returned by ``OrderbookCache.snapshot``. ``None`` (the cache
        has never seen a frame for this ticker) yields ``status='no_book'``.
    action
        ``'buy'`` or ``'sell'``. Anything else returns ``status='no_book'``.
    side
        ``'yes'`` or ``'no'`` — the contract side the *taker* is
        targeting (NOT the resting half-book).
    count
        Requested contract count. Must be positive; non-positive returns
        an empty ``filled`` result so the caller can short-circuit.
    limit_price_cents
        Per-contract price cap (BUY) / floor (SELL) in taker units.
        ``None`` means "market order" — walk all available depth.
    slippage_tolerance_cents
        Optional extra ceiling on how far the realised VWAP may drift
        from ``limit_price_cents`` (or from the best available level
        when no limit was given). ``None`` disables the check.
    """
    n = int(count)
    if n <= 0:
        return PaperFillResult(
            filled_qty=0, vwap_price_cents=None, residual_qty=0,
            status="filled",
        )

    levels, cross = _select_half_book(book, action=action, side=side)
    if levels is None:
        return PaperFillResult(
            filled_qty=0, vwap_price_cents=None, residual_qty=n,
            status="no_book",
        )
    if not levels:
        return PaperFillResult(
            filled_qty=0, vwap_price_cents=None, residual_qty=n,
            status="no_liquidity",
        )

    # Best price first for either side: the *highest* resting bid wins,
    # because BUY-taker pays (100 - bid) so high-bid → low cost, and
    # SELL-taker receives the bid directly so high-bid → high proceeds.
    sorted_bids = sorted(levels.items(), key=lambda kv: kv[0], reverse=True)

    walked: list[tuple[int, int]] = []
    remaining = n
    notional_cents = 0
    filled = 0
    blocked_by_limit = False
    for bid_price, depth in sorted_bids:
        # Convert resting bid → per-contract taker price.
        taker_price = (100 - int(bid_price)) if cross else int(bid_price)
        if limit_price_cents is not None:
            limit = int(limit_price_cents)
            if action == "buy" and taker_price > limit:
                blocked_by_limit = True
                break
            if action == "sell" and taker_price < limit:
                blocked_by_limit = True
                break
        take = min(int(depth), remaining)
        if take <= 0:
            continue
        walked.append((taker_price, take))
        notional_cents += taker_price * take
        filled += take
        remaining -= take
        if remaining <= 0:
            break

    if filled <= 0:
        # Nothing crossed: distinguish "limit unreachable" from "side
        # is empty" for the audit trail. ``no_liquidity`` is reported
        # above when the side itself is empty; reaching here means we
        # had bids but every one violated the limit.
        return PaperFillResult(
            filled_qty=0, vwap_price_cents=None, residual_qty=n,
            status="limit_unfillable" if blocked_by_limit else "no_liquidity",
        )

    # Bankers'-rounding-free integer VWAP: round half-up so the printed
    # cents matches what a human would compute from the levels list.
    vwap = (notional_cents + filled // 2) // filled

    # Slippage gate (optional). Reference price is the BEST level walked
    # (lowest taker cost for BUY, highest proceeds for SELL). Using the
    # limit as the reference would make this inert — we never walk past
    # the limit, so VWAP <= limit by construction.
    if slippage_tolerance_cents is not None and walked:
        band = max(0, int(slippage_tolerance_cents))
        reference = walked[0][0]  # first level walked = best price
        if action == "buy" and vwap > reference + band:
            return PaperFillResult(
                filled_qty=0, vwap_price_cents=None, residual_qty=n,
                status="slippage_exceeded",
            )
        if action == "sell" and vwap < reference - band:
            return PaperFillResult(
                filled_qty=0, vwap_price_cents=None, residual_qty=n,
                status="slippage_exceeded",
            )

    status: PaperFillStatus = "filled" if remaining == 0 else "partial"
    return PaperFillResult(
        filled_qty=filled,
        vwap_price_cents=int(vwap),
        residual_qty=remaining,
        status=status,
        levels_walked=walked,
    )
