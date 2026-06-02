---
name: Marketable FOK + canary bypass (Kalshi live)
description: Operator directives that no Kalshi order may rest and every live trade is full flat-fraction; how they're enforced and where they can still be bypassed.
---

# Marketable FOK + canary bypass (Kalshi live)

Two standing operator directives for the LIVE-MONEY Kalshi auto-trader:
1. No resting orders — Kalshi orders are marketable fill-or-kill (FOK), price-protected with a cents buffer.
2. Take what fills now, don't lose thin-book trades (IOC-style) — a FOK is SIZED DOWN to the immediately-fillable depth and fired; whatever fills, fills; the venue kills the rest. Nothing ever rests. This SUPERSEDES the old "full size or nothing" rule for FOK.

**Why:** operator does not want capital tied up in unfilled resting limits. The old all-or-nothing FOK lost trades on thin sports/tennis books (Kalshi 409 `fill_or_kill_insufficient_resting_volume`); operator explicitly chose partial fills over missed trades. Kalshi has NO native IOC, so IOC semantics are emulated via book-aware FOK sizing.

**How to apply (enforcement points, by behavior not line number):**
- The marketable behavior is gated on a Settings flag (env `AXIOM_MARKETABLE_FOK`, default ON). When ON, the Kalshi dispatch sets `time_in_force="FOK"` and limit cents = `clamp(mid + buffer, 1, 99)`. When OFF it reverts to resting GTC at the unmodified mid — so toggling the env var silently re-enables resting orders.
- FOK IS sliced to fillable depth (IOC emulation): the slippage-guard slice path now applies to FOK, sizing the order to the resting depth within the edge budget. The two guard early-returns that "allow a resting limit" (empty-side allow, rest_full_limit) are gated `req.time_in_force != "FOK"` — their rationale ("the unfilled remainder simply rests") is only true for GTC; for FOK the remainder is KILLED. FOK on an empty side falls through to a clean slice-0 reject (no resting order). GTC behavior is unchanged. Residual: in degraded mode (stale/missing orderbook) the guard returns None and a full-size FOK can still 409 — a missed trade, but never resting exposure.
- Canary/ramp is gated on `canary_ramp_enabled` (env `AXIOM_CANARY_RAMP_ENABLED`, default OFF). Default OFF means no qty=1 canary coercion; turning it ON reintroduces ramping and breaks directive 2.
- `to_kalshi_payload` maps FOK → Kalshi `fill_or_kill`; GTC omits the field; IOC raises.

**Residual bypass to remember:** `paper.place_order` still floors `quantity = max(1, int(intended_quantity))`. The Edge Terminal autotrader path already skips when flat-fraction qty < 1, so the floor is unreachable from the autotrader — but a non-Edge-Terminal caller passing 0 could still be coerced to a 1-contract sub-target trade. Left as-is intentionally (out of scope of the two directives' live path); revisit if another caller starts originating live Kalshi orders.

**Settings fields are additive** (declared mid-dataclass): safe because nothing constructs the real `Settings` positionally — only `get_settings()` does, with keywords; the one test "Settings" is a separate `_Settings` stub class.
