---
name: Edge Terminal signal diagnostics
description: How to inspect live edge_terminal_signals data, and what "trades not firing" usually actually means.
---

# Where the signal data lives
- The Edge Terminal poller/executor write to **Supabase Postgres** (`settings.supabase_db_dsn`), NOT the Replit built-in database.
- Therefore `executeSql` (Replit DB / prod replica) CANNOT see `edge_terminal_signals` — it returns "relation does not exist" or empty.
- To inspect live signals, run a read-only Python script from `artifacts/api-server` using the app's own pool: `from app import db; await db.fetch(...)`. Same pattern as the Kalshi client diagnostics. Always `await db.close_pool()` at the end.

# "Sports/MLB trades not firing" is usually NOT a bug
**Why:** Most sports picks are skipped for legitimate, by-design reasons, not resolution failures. Classify by `status` + `status_reason` before assuming a bug:
- `skipped_low_confidence` — model prob below the 60% floor (a deliberate live-money risk gate). This is the dominant MLB skip reason; lots of picks sit just under 60%.
- `skipped_slippage` — "no liquidity on requested side" (correct guard).
- `placed` — it IS trading.
- `skipped_unresolved` — the only status that signals a real resolve/market-availability gap.

**How to apply:** Query `SELECT vertical,status,count(*) ... GROUP BY` first. As of mid-2026 the real `skipped_unresolved` backlog is in **tennis and soccer**, not MLB. MLB resolves fine — Kalshi lists open MLB markets ~3-6 days ahead (`KXMLBGAME/SPREAD/TOTAL`, status filter `open` returns them; `active`/`unopened` are invalid filter values → HTTP 400), and next-day games scraped today resolve to the correct future ticker. Lowering the 60% confidence floor is a risk change — never do it without explicit user consent.
