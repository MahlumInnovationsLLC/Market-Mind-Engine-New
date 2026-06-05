---
name: Daily email trade visibility & staleness paging gate
description: Why the day-end email reads "no trades" and how live-state staleness paging is suppressed in edge_terminal mode.
---

## Daily summary counted only SETTLED trades
The day-end email (`daily_summary.collect_summary` + `alerts.daily_pnl_summary`)
historically derived its trade view from realized/settled PnL only
(`trades WHERE settled_at IS NOT NULL`). Same-day **live Edge Terminal**
placements have no `settled_at` yet, so the email read "no trades" even when
the bot traded all day.

**Fix pattern:** a separate "Trades placed today" section sourced from
`trades` filtered on `executed_at` (all sources/modes), exposed as
`trades_placed_today` (count) + `placed_trades` (list).
**Why:** placed ≠ settled; an order-flow view and a realized-PnL view answer
different questions and must be computed from different columns.
**How to apply:** when adding renderer params to `daily_pnl_summary` /
`_render_daily_pnl_summary_html`, make them keyword-only WITH DEFAULTS — both
are invoked via `**summary` splat (fire_now + preview endpoint), so a missing
kwarg crashes the whole digest. `trades.price` is stored in DOLLARS; render as
cents via `round(price*100)`.

## Live-state staleness paging is input-mode-gated
`check_live_state_staleness()` (the "[axiom][LIVE-STATE STALE] <sport> feed …"
pager) is scheduled in `cycle.run_cycle` ONLY when the resolved input mode is
not `edge_terminal`.
**Why:** the sportradar live-state feed only drives the in-game prediction
loop, which ships no sports trades in edge_terminal mode — a stale sports feed
is then expected, so paging on it is pure noise.
**How to apply:** the gate reuses the single fail-closed `_input_mode =
_resolve_input_mode()` already resolved in the cycle (fails CLOSED to
edge_terminal). It auto-restores if the operator returns to pipeline mode.
Pipeline-exempt verticals (e.g. weather) carry no sportradar live-state, so the
global-mode gate covers them too. The `check_live_state_staleness` function
itself is unchanged — gating lives at the call site so its unit tests stay
valid.
