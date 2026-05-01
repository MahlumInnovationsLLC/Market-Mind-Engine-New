"""Unit tests for ``app.kalshi.paper_fill_sim``.

Pure-function tests against synthetic half-books. No DB, no event bus,
no network. The simulator is the workhorse behind the honest paper-fill
pipeline that replaced the legacy 100 %-at-limit shortcut, so these
tests cover the four reject paths called out in task #751
(no-book / no-liquidity / limit-unfillable / slippage-exceeded) plus
the binary-book transform on every action × side combination.
"""
from __future__ import annotations

import pytest

from app.kalshi.paper_fill_sim import simulate_paper_fill


# ---------------------------------------------------------------------------
# Book-shape helpers — `{price_cents: size}` per side, same shape that
# OrderbookCache.snapshot() returns.
# ---------------------------------------------------------------------------

def _book(yes: dict[int, int] | None = None, no: dict[int, int] | None = None):
    return {"yes": dict(yes or {}), "no": dict(no or {})}


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------

def test_no_book_at_all_returns_no_book_status():
    """Cache cold for this ticker → ``status='no_book'``.

    Lifecycle treats this as a degraded-allow fallback to the legacy
    limit-price shortcut so paper P&L doesn't go silent on cold start.
    """
    out = simulate_paper_fill(
        None, action="buy", side="yes", count=10, limit_price_cents=42,
    )
    assert out.status == "no_book"
    assert out.filled_qty == 0
    assert out.residual_qty == 10
    assert out.vwap_price_cents is None
    assert out.levels_walked == []


def test_zero_count_short_circuits_without_walking():
    out = simulate_paper_fill(
        _book(no={50: 100}), action="buy", side="yes", count=0,
        limit_price_cents=60,
    )
    assert out.filled_qty == 0
    assert out.residual_qty == 0
    assert out.status == "filled"


@pytest.mark.parametrize("bad_action", ["", "BUY", "hold", None])
def test_invalid_action_returns_no_book(bad_action):
    out = simulate_paper_fill(
        _book(no={50: 100}), action=bad_action, side="yes",  # type: ignore[arg-type]
        count=5, limit_price_cents=60,
    )
    assert out.status == "no_book"
    assert out.filled_qty == 0


@pytest.mark.parametrize("bad_side", ["", "YES", "long", None])
def test_invalid_side_returns_no_book(bad_side):
    out = simulate_paper_fill(
        _book(no={50: 100}), action="buy", side=bad_side,  # type: ignore[arg-type]
        count=5, limit_price_cents=60,
    )
    assert out.status == "no_book"


# ---------------------------------------------------------------------------
# Reject paths called out in the task plan
# ---------------------------------------------------------------------------

def test_no_liquidity_when_target_side_is_empty():
    """BUY YES walks the no-book; if the no-book is empty we report
    ``no_liquidity`` (a real reject, NOT the cold-cache fallback)."""
    out = simulate_paper_fill(
        _book(yes={70: 100}, no={}),  # no-book empty
        action="buy", side="yes", count=10, limit_price_cents=60,
    )
    assert out.status == "no_liquidity"
    assert out.filled_qty == 0
    assert out.residual_qty == 10


def test_limit_unfillable_when_every_level_violates_ceiling():
    """BUY YES with every no-bid so high (=> taker-cost so high) that the
    limit price is unreachable."""
    # Best no-bid is 30 → taker yes-cost is 70¢. Limit 60¢ rejects.
    out = simulate_paper_fill(
        _book(no={30: 100, 25: 50}),
        action="buy", side="yes", count=10, limit_price_cents=60,
    )
    assert out.status == "limit_unfillable"
    assert out.filled_qty == 0


def test_slippage_exceeded_blocks_fill_when_vwap_drifts_past_band():
    """VWAP across walked levels drifts more than ``tolerance`` past the
    BEST level walked (top of book) → reject."""
    # No-bids: 35 (size 1) → taker pays 65¢; 20 (size 100) → 80¢.
    # Limit 80¢ allows both; band 5¢ around the best (65¢) caps at 70¢.
    # Walking 5 contracts: 1 @ 65 + 4 @ 80 = 385/5 = 77¢ → 77 > 70 → reject.
    out = simulate_paper_fill(
        _book(no={35: 1, 20: 100}),
        action="buy", side="yes", count=5,
        limit_price_cents=80,
        slippage_tolerance_cents=5,
    )
    assert out.status == "slippage_exceeded"
    assert out.filled_qty == 0


def test_slippage_band_passes_when_vwap_within_tolerance():
    """Same book, generous band → fill goes through."""
    out = simulate_paper_fill(
        _book(no={35: 1, 20: 100}),
        action="buy", side="yes", count=5,
        limit_price_cents=80,
        slippage_tolerance_cents=20,
    )
    assert out.status == "filled"
    assert out.filled_qty == 5


def test_slippage_gate_for_sell_uses_best_proceeds_as_reference():
    """SELL gate: trip when realised VWAP is more than ``tolerance``
    cents BELOW the highest bid walked."""
    # yes-bids: 75 (size 1), 60 (size 100). Sell 5: 1 @ 75 + 4 @ 60 = 315/5 = 63.
    # Best proceeds = 75; band 5 → floor 70. 63 < 70 → reject.
    out = simulate_paper_fill(
        _book(yes={75: 1, 60: 100}),
        action="sell", side="yes", count=5,
        limit_price_cents=50,
        slippage_tolerance_cents=5,
    )
    assert out.status == "slippage_exceeded"
    assert out.filled_qty == 0


# ---------------------------------------------------------------------------
# Happy paths — full and partial fills, all four (action, side) combos
# ---------------------------------------------------------------------------

def test_buy_yes_full_fill_uses_no_book_with_binary_transform():
    """BUY YES walks the no-book. Best resting no-bid 40 → taker pays
    100-40 = 60¢ per contract."""
    out = simulate_paper_fill(
        _book(no={40: 5, 35: 10}),
        action="buy", side="yes", count=5, limit_price_cents=70,
    )
    assert out.status == "filled"
    assert out.filled_qty == 5
    assert out.vwap_price_cents == 60         # all 5 from the 40-cent bid
    assert out.residual_qty == 0
    assert out.levels_walked == [(60, 5)]


def test_buy_yes_partial_fill_when_depth_runs_out_at_limit():
    """Walks levels best→worst but stops when the next bid would push
    taker-cost over the limit; remaining quantity becomes residual."""
    # no-bids: 40 (size 3) -> taker 60¢, 30 (size 100) -> taker 70¢.
    # Limit 65¢ -> only the first level qualifies, fills 3 of 10.
    out = simulate_paper_fill(
        _book(no={40: 3, 30: 100}),
        action="buy", side="yes", count=10, limit_price_cents=65,
    )
    assert out.status == "partial"
    assert out.filled_qty == 3
    assert out.vwap_price_cents == 60
    assert out.residual_qty == 7
    assert out.levels_walked == [(60, 3)]


def test_buy_yes_full_fill_walks_two_levels_with_vwap():
    """Walks two no-bid levels and reports the volume-weighted VWAP."""
    # no-bids 45 (size 4) → taker 55¢, 40 (size 10) → taker 60¢.
    # Buying 6 → 4 @ 55 + 2 @ 60 = 340¢ over 6 contracts → 56.67 → 57¢ rounded.
    out = simulate_paper_fill(
        _book(no={45: 4, 40: 10}),
        action="buy", side="yes", count=6, limit_price_cents=70,
    )
    assert out.status == "filled"
    assert out.filled_qty == 6
    assert out.residual_qty == 0
    assert out.levels_walked == [(55, 4), (60, 2)]
    # 4*55 + 2*60 = 340 ; (340 + 6//2) // 6 = 343//6 = 57
    assert out.vwap_price_cents == 57


def test_buy_no_uses_yes_book_with_binary_transform():
    """BUY NO walks the yes-book; per-contract no-cost = 100 - yes_bid_price."""
    # yes-bids: 30 (size 4) → taker no-cost 70¢. Limit 75 allows it.
    out = simulate_paper_fill(
        _book(yes={30: 4, 25: 100}),
        action="buy", side="no", count=4, limit_price_cents=75,
    )
    assert out.status == "filled"
    assert out.filled_qty == 4
    assert out.vwap_price_cents == 70
    assert out.levels_walked == [(70, 4)]


def test_sell_yes_hits_yes_book_directly():
    """SELL YES does NOT cross-flip — it eats the yes-book bids and
    receives the bid price as proceeds. Best (highest) bid first."""
    out = simulate_paper_fill(
        _book(yes={75: 5, 70: 10}),
        action="sell", side="yes", count=5, limit_price_cents=70,
    )
    assert out.status == "filled"
    assert out.filled_qty == 5
    assert out.vwap_price_cents == 75
    assert out.levels_walked == [(75, 5)]


def test_sell_yes_partial_when_floor_blocks_deeper_levels():
    """SELL with a floor: stops at the first bid below the floor."""
    # Bids 75 (size 2), 60 (size 100). Floor 70¢ → only first level used.
    out = simulate_paper_fill(
        _book(yes={75: 2, 60: 100}),
        action="sell", side="yes", count=10, limit_price_cents=70,
    )
    assert out.status == "partial"
    assert out.filled_qty == 2
    assert out.residual_qty == 8
    assert out.vwap_price_cents == 75


def test_sell_no_hits_no_book_directly():
    out = simulate_paper_fill(
        _book(no={45: 3}),
        action="sell", side="no", count=3, limit_price_cents=40,
    )
    assert out.status == "filled"
    assert out.filled_qty == 3
    assert out.vwap_price_cents == 45
    assert out.levels_walked == [(45, 3)]


# ---------------------------------------------------------------------------
# Market orders (limit_price_cents=None) walk all available depth
# ---------------------------------------------------------------------------

def test_market_buy_walks_all_depth_with_no_limit_check():
    """Without a limit, the simulator consumes whatever depth it needs
    even if the deeper levels would have failed a limit-price check."""
    # no-bids: 60 (size 2) → 40¢, 10 (size 100) → 90¢. No limit, so
    # buy 5 contracts: 2 @ 40 + 3 @ 90 = 350¢ / 5 = 70¢.
    out = simulate_paper_fill(
        _book(no={60: 2, 10: 100}),
        action="buy", side="yes", count=5, limit_price_cents=None,
    )
    assert out.status == "filled"
    assert out.filled_qty == 5
    assert out.vwap_price_cents == 70
    assert out.levels_walked == [(40, 2), (90, 3)]


# ---------------------------------------------------------------------------
# Edge: deep-but-bounded books — partial fill from outright depth shortage
# ---------------------------------------------------------------------------

def test_partial_fill_from_pure_depth_exhaustion():
    """Even with no limit and no slippage band, a thin book caps the fill
    quantity. Result is a partial fill, not a reject."""
    out = simulate_paper_fill(
        _book(no={50: 3}),
        action="buy", side="yes", count=10, limit_price_cents=None,
    )
    assert out.status == "partial"
    assert out.filled_qty == 3
    assert out.residual_qty == 7
    assert out.vwap_price_cents == 50
