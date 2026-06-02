---
name: Marketable FOK + canary bypass (Kalshi live)
description: Operator directives that no Kalshi order may rest and every live trade is full flat-fraction; how they're enforced and where they can still be bypassed.
---

# Marketable FOK + canary bypass (Kalshi live)

Two standing operator directives for the LIVE-MONEY Kalshi auto-trader:
1. No resting orders — Kalshi orders are marketable fill-or-kill (FOK), price-protected with a cents buffer; fill in full immediately or kill. Never rest a partial.
2. No sub-target trades — every trade is the full flat fraction of bankroll (FLAT_BANKROLL_FRACTION, 5%); canary/ramp is bypassed by default so the full intended quantity is sent.

**Why:** operator does not want capital tied up in unfilled resting limits, and does not want tiny canary/min-stake probe trades — every live fire should be the full sized bet or nothing.

**How to apply (enforcement points, by behavior not line number):**
- The marketable behavior is gated on a Settings flag (env `AXIOM_MARKETABLE_FOK`, default ON). When ON, the Kalshi dispatch sets `time_in_force="FOK"` and limit cents = `clamp(mid + buffer, 1, 99)`. When OFF it reverts to resting GTC at the unmodified mid — so toggling the env var silently re-enables resting orders.
- FOK must never be sliced: the slippage-guard slice path is gated to skip FOK, so an FOK order rejects/kills instead of resting a trimmed remainder. Any future slicing/sizing change must preserve "FOK ⇒ no slice".
- Canary/ramp is gated on `canary_ramp_enabled` (env `AXIOM_CANARY_RAMP_ENABLED`, default OFF). Default OFF means no qty=1 canary coercion; turning it ON reintroduces ramping and breaks directive 2.
- `to_kalshi_payload` maps FOK → Kalshi `fill_or_kill`; GTC omits the field; IOC raises.

**Residual bypass to remember:** `paper.place_order` still floors `quantity = max(1, int(intended_quantity))`. The Edge Terminal autotrader path already skips when flat-fraction qty < 1, so the floor is unreachable from the autotrader — but a non-Edge-Terminal caller passing 0 could still be coerced to a 1-contract sub-target trade. Left as-is intentionally (out of scope of the two directives' live path); revisit if another caller starts originating live Kalshi orders.

**Settings fields are additive** (declared mid-dataclass): safe because nothing constructs the real `Settings` positionally — only `get_settings()` does, with keywords; the one test "Settings" is a separate `_Settings` stub class.
