---
name: Slippage guard empty-side allow
description: The slippage guard's empty-requested-side branch allows resting limits by default; flag to restore hard-reject.
---

# Slippage guard: empty requested side → allow as resting limit

In `lifecycle._check_slippage_guard`, when the orderbook snapshot EXISTS
but the requested side has zero resting liquidity (`book_known=False`),
the guard returns `None` (allow) by default rather than hard-rejecting
with "no liquidity on requested side".

**Why:** thin sports / tennis / weather books often carry no standing
asks on our side at decision time. Returning `None` lets the order rest
as a limit at the already-edge-capped price — worst case is an unfilled
resting order, never a bad fill (no market execution; `to_kalshi_payload`
always emits limit `yes_price`/`no_price`).

**How to apply:** gated by `Settings.slippage_guard_allow_empty_side`
(env `AXIOM_SLIPPAGE_GUARD_ALLOW_EMPTY_SIDE`, default True). Set it false
to restore the legacy hard-reject — keep this as an operational
kill-switch. This loosening does NOT touch the full/partial-depth
slippage-erosion math, only the zero-depth-on-our-side case.

## Full-rest path must be gated on `book_known`

A second flag `slippage_guard_rest_full_limit` (env
`AXIOM_SLIPPAGE_GUARD_REST_FULL_LIMIT`, default True) rests the FULL
limit-order size on a thin/partial-depth book instead of trimming to the
immediately-fillable slice (the "why are my bets sub-$1?" fix — trimming
shrank a Kelly-sized order to a 1-contract sub-dollar position).

**Why:** a limit order's every fill is bounded by its own limit price
(already inside the edge budget), so a thin book can never produce an
out-of-budget fill — the remainder simply rests.

**How to apply:** the full-rest branch MUST be gated on `book_known`
(`if book_known and slippage_guard_rest_full_limit:`). An empty requested
side reaches that branch with `book_known=False` only when the operator
set `allow_empty_side=False` (their explicit hard-reject) — an ungated
full-rest would silently override that kill-switch and let an empty-side
order through. Keep the two flags independent: full-rest is for partial
depth, allow_empty_side is for zero depth on our side.

## Both "allow as resting limit" paths are gated OFF for FOK

Both the empty-side allow AND the rest_full_limit early-returns reason
"the unfilled remainder simply rests" — true ONLY for GTC. A marketable
FOK's remainder is KILLED by Kalshi, and a full-size FOK on a thin book
gets a 409 `fill_or_kill_insufficient_resting_volume` (lost trade). So
both early-returns carry `and req.time_in_force != "FOK"`: FOK falls
through to the slice-to-depth path (sized to fillable depth, IOC-style),
and FOK on an empty side falls through to a clean slice-0 reject. The
submit-time slice branch also applies to FOK now (no longer excluded).

**Why:** operator chose partial fills over missed trades on thin sports
books. **How to apply:** any future guard change must keep these two
"rest-the-limit" allows FOK-excluded, or a marketable FOK will silently
rest or 409.

## HYBRID: empty-side FOK converts to resting GTC (no-trades fix)

A pure marketable-FOK regime starves at thin early-morning books: when
the requested side is EMPTY there is nothing to take, every FOK is killed,
and a whole morning of signals dies with "no liquidity on requested side"
(`market_price=None`). Kalshi has NO IOC, so there is no take-what's-there
order type to fall back on. The operator-approved HYBRID keeps marketable
FOK on liquid books but, when the requested side is empty, converts that
ONE order to a resting GTC limit instead of killing it.

**Mechanics (behavior, not line numbers):**
- Guard: when `worst is None and filled==0` with a snapshot present (empty
  requested side) AND the order is FOK AND the hybrid flag is on, the guard
  returns a sentinel `{"rest_empty_side_as_gtc": True, ...}` instead of
  falling to the slice-0 "no liquidity" reject.
- `submit_order`: on that sentinel it rewrites the OrderRequest
  `time_in_force` FOK→GTC and STRIPS `marketable_buffer_cents` from
  `limit_price_cents` (only when `marketable_fok_enabled`), then sets
  `guard=None` so the order proceeds and rests. Stripping the buffer
  matters: the buffer is a cross-the-spread allowance that only makes sense
  when taking; a resting order should sit at the edge-capped price.

**Why strip the buffer:** the marketable buffer crosses the spread to take
liquidity. With nothing to take, the buffer would just overpay on a
resting order, eroding edge for no reason.

**How to apply:** gated by `Settings.slippage_guard_rest_empty_side_fok`
(env `AXIOM_SLIPPAGE_GUARD_REST_EMPTY_SIDE_FOK`, default True). When OFF,
empty-side FOK reverts to the clean slice-0 reject (legacy kill). The
liquid-book path (`book_known=True`) is untouched — FOK there still fills
or slices, never flips to GTC (locked by a regression test). In PAPER mode
the converted GTC is then rejected by the fill sim as `paper_no_liquidity`
(expected); in LIVE mode it rests on the real book.
