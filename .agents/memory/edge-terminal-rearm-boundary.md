---
name: Edge Terminal re-arm boundary
description: Which disposed signal statuses may be re-armed on re-scrape, and why; plus the backstop re-arm double-bet guard.
---

# Edge Terminal signal re-arm (ON CONFLICT) boundary

The `edge_terminal_signals` INSERT in `poller._insert_signal` re-arms a
conflicting row (dedupe key `(vertical, matchup, selection, signal_date)`) via
`ON CONFLICT DO UPDATE SET status='pending' ... WHERE status IN (...)`.

**Rule:** re-arm ONLY the *transient market/liquidity* dispositions —
`skipped_unresolved` and `skipped_slippage` — and nothing else.

**Why:**
- `skipped_unresolved`: Kalshi sports markets list only a few hours before game
  time, but the board badges picks earlier. The first drain resolves a pre-game
  pick to "no live market". The old `ON CONFLICT DO NOTHING` + the dedupe unique
  index then froze that disposition for the whole UTC day, so the pick never
  retried once its market opened (root cause of "only BTC trades, no sports").
- `skipped_slippage`: the requested side was empty / too thin to fill within
  budget at decision time. The early book thickens as the session opens, so the
  edge should be retried later ("come back when it's deeper") rather than
  forgotten for the day.
- `placed` / `skipped_already_placed` / `skipped_gate` must NEVER be re-armed —
  `placed` holds a real position (double-bet), `skipped_gate` is a settled/closed
  game (order-time `market_not_found` is "overwhelmingly a settled game", see
  `executor._classify_exception`). They fail the WHERE clause.
- Thrash is naturally bounded: the board only shows current/upcoming games, so a
  settled pick stops being re-scraped and stops re-arming on its own.

**How to apply:** to add a new transient skip reason, add its status to that
WHERE clause AND to `_REARM_STATUSES` in `tests/test_poller_rearm.py` (kept in
lockstep). Never add a status that can represent a held position or a
deterministic stand-down. `DO UPDATE ... WHERE false` returns no row →
`fetchval` None → treated as a conflict-skip (matches the dedupe contract;
`inserted` only increments on non-None).

# Backstop re-arm (empty-side hybrid GTC) — double-bet guard

The empty-side hybrid FOK→GTC backstop rests deliberately (tagged
`rest_empty_side_backstop`, carries `edge_signal_id`) and earns a longer TTL
than ordinary resting orders (`_resolve_backstop_max_age`, clamped to never
shorter than the plain resting TTL). When the cycle stale-sweep cancels an
UNFILLED backstop it flips the signal `placed → skipped_slippage` so path (A)
retries it.

**Rule:** the sweep re-arms a backstop ONLY when all three hold: it is a
backstop, `filled_count == 0`, AND `fill_count_authoritative` is True. The
`_rearm_backstop_signal` UPDATE is additionally guarded `WHERE id=$1 AND
status='placed'`.

**Why:** the re-arm decision reads in-memory `rec.filled_count`, which is only
updated by WS fill events. A venue-side partial fill whose WS event is delayed
would leave `filled_count` at a stale 0 — re-arming then re-buys exposure we
already hold (double-bet on LIVE money). Unknown fill state must never be
treated as a clean zero.

**How to apply:** `cancel_order` (live path) reconciles authoritative venue
truth BEFORE popping the record from `_resting`: it parses the cancel response
(`_extract_venue_filled_count` → `filled_count`/`executed_count`, else
`maker_fill_count + taker_fill_count`); if that is silent it does a same-session
`get_order` fallback. On a value it sets `filled_count = max(in_memory, venue)`
and `fill_count_authoritative = True`; if still unknown it sets
`fill_count_authoritative = False`. The `get_order` fallback is wrapped in its
OWN inner try/except so a lookup failure can't mask a genuinely successful
cancel as a cancel failure. Once Kalshi confirms a cancel, no further fills can
land, so the cancel/get-order response is the final, authoritative fill state.
