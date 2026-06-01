---
name: Edge Terminal re-arm boundary
description: Which disposed signal statuses may be re-armed on re-scrape, and why only skipped_unresolved.
---

# Edge Terminal signal re-arm (ON CONFLICT) boundary

The `edge_terminal_signals` INSERT in `poller._insert_signal` re-arms a
conflicting row (dedupe key `(vertical, matchup, selection, signal_date)`) via
`ON CONFLICT DO UPDATE SET status='pending' ... WHERE status = 'skipped_unresolved'`.

**Rule:** re-arm exactly ONE status — `skipped_unresolved` — and nothing else.

**Why:**
- Kalshi sports markets list only a few hours before game time, but the Edge
  Terminal board badges picks earlier. The first drain of the day resolves a
  pre-game pick to "no live market" → `skipped_unresolved`. The old
  `ON CONFLICT DO NOTHING` + the dedupe unique index then froze that
  disposition for the whole UTC day, so the pick never retried once its market
  opened. That was the root cause of "only BTC trades, no MLB/sports".
- `placed` / `skipped_already_placed` must NEVER be re-armed — they hold a real
  position and re-arming would double-bet live money. They fail the WHERE clause.
- `skipped_gate` is deliberately EXCLUDED even though it looks transient: an
  order-time `market_not_found` is "overwhelmingly a settled/closed game" (see
  `executor._classify_exception`), so re-arming it would re-retry finished
  fixtures, not fix anything. `order rejected` / `market closed` gates are also
  not transient market-availability.
- Thrash is naturally bounded: the board only shows current/upcoming games, so
  a settled pick stops being re-scraped and stops re-arming on its own.

**How to apply:** when adding a new transient skip reason that should retry,
add its status to that WHERE clause AND to `_REARM_STATUSES` in
`tests/test_poller_rearm.py` (kept in lockstep). Never add a status that can
represent a held position or a deterministic stand-down. `DO UPDATE ... WHERE
false` returns no row → `fetchval` None → treated as a conflict-skip, matching
the dedupe contract (`inserted` only increments on non-None).
