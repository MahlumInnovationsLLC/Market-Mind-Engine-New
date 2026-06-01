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
