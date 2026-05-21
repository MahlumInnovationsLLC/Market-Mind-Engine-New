# Cross-book line-shopping router runbook (task #828)

This document covers the day-to-day operation of `gate_best_line`, the
cross-book line-shopping gate that runs after `gate_agreement` and
before the tail-risk / sizing gates. The gate compares our chosen
venue's implied price against the latest `line_snapshots` row for every
quoting book and either lets the trade through, reroutes it to
Polymarket, or blocks it with an operator alert.

## Scope

Only candidates with `vertical=sports` and `market_type ∈ {moneyline /
h2h, spread, total}` are evaluated. Everything else is scoped out and
passes immediately. Player props are handled by a separate gate.

## Outcomes

1. **PASS (no change)** — our chosen venue beats or matches every
   other book, or the delta is below threshold.
2. **PASS + reroute** — Polymarket is offering a meaningfully better
   implied price. The gate sets `ctx.venue_override=['polymarket']`
   and the orchestrator calls the Polymarket execution path instead
   of `route_for_candidate`. An info-severity `best_line_alert` with
   `kind=reroute_polymarket` is emailed + broadcast.
3. **FAIL + alert** — a traditional sportsbook (DraftKings, FanDuel,
   BetMGM, Caesars, …) is offering a meaningfully better implied
   price. The Kalshi candidate is blocked. An info-severity
   `best_line_alert` with `kind=block_traditional` is emailed +
   broadcast so the operator can place the bet manually on the
   named book.

## Tuning the delta threshold

Default is **200 bps (2.00% implied)**. Override globally or per
league via the `system_settings.risk_knob_overrides` JSON column
already used by the other risk gates.

```sql
-- Global override (applies to every league)
UPDATE system_settings
   SET risk_knob_overrides = risk_knob_overrides
                          || '{"best_line_threshold_bps": 300}'::jsonb
 WHERE id = 1;

-- Per-league override (key is "best_line_threshold_bps_<league_lower>")
UPDATE system_settings
   SET risk_knob_overrides = risk_knob_overrides
                          || '{"best_line_threshold_bps_nba": 100,
                               "best_line_threshold_bps_ncaab": 250}'::jsonb
 WHERE id = 1;
```

Per-league overrides take precedence over the global override, which
takes precedence over the 200 bps default. Lower the per-league number
when a market is consistently tight (e.g. NBA primetime) and raise it
when books quote wide (e.g. mid-week NCAAB).

## Adding or removing books

The gate reads from `line_snapshots`, which is populated by the
`odds_api` ingestion job. To add a book:

1. Confirm the book is in the [odds-api supported list](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html).
2. Add it to the comma-separated `ODDS_API_BOOKS` env var on the API
   server (e.g. `"draftkings,fanduel,betmgm,caesars,pointsbetus"`).
3. Redeploy. The ingester will pick it up on the next tick; rows
   appear in `line_snapshots` keyed by `(book, market_type, outcome)`.
4. Confirm the new book is in the alert envelope's `books_seen` list
   on the next `best_line_alert`.

To drop a book, remove it from `ODDS_API_BOOKS` and redeploy. Rows
older than 5 minutes are ignored by the gate, so the dropped book
will stop appearing in alerts within minutes.

## Failure modes (fail-open guarantees)

Every error path in `gate_best_line` returns `passed=True` with a
logged warning so a feed outage cannot silently halt sports trading:

| Condition                         | Result                                       |
|-----------------------------------|----------------------------------------------|
| `lookup_best_line` raises         | PASS, `fail_open=lookup_exception`           |
| Lookup takes > 50 ms              | PASS, `fail_open=lookup_timeout`             |
| Last snapshot > 5 min old         | PASS, `fail_open=no_recent_quotes`           |
| `current_price` missing           | PASS, `reason=no_current_price`              |
| Non-sports vertical               | PASS, `scoped_out=non-sports`                |
| Unsupported market type           | PASS, `scoped_out=unsupported_market_type`   |

If you see a sustained run of `fail_open=*` reasons in the
`risk_gate_trips` table, the `odds_api` ingester is likely behind —
check its workflow logs before assuming the gate is mis-tuned.

## Reconciling manually-placed bets

When the gate fires `kind=block_traditional`, the dashboard tile
"Best-line alerts (last 24h)" shows the row with a **Mark placed**
toggle. Click it after you place the bet on the named book. This
writes a row to `best_line_alert_actions` keyed by the alert's dedupe
key with your operator email, the book name, the stake size, and an
optional note.

The daily honesty report's `best_line_router` section reads the join
of `notification_dedupe` (the alert) and `best_line_alert_actions`
(the manual placement) over the last 24h, and a 7-day
implied-EV-preserved estimate derived from `risk_gate_trips`.

To clear a marker (you changed your mind before placing), call:

```bash
curl -X POST $API/api/admin/best-line-alerts/mark-placed \
  -H 'Content-Type: application/json' \
  -d '{"dedupe_key":"best_line_alert:block_traditional|NBA|moneyline|LAL_vs_BOS|...","placed":false}'
```

## Observability checklist

- `risk_gate_trips` rows with `gate_name='best_line'` capture every
  blocked candidate (with the full delta + best-book breakdown in
  `details`).
- `notification_dedupe` rows with `key LIKE 'best_line_alert:%'` are
  the source of truth for alerts that were dispatched.
- Daily honesty report `best_line_router` section surfaces 24h
  counters + 7d EV-preserved.
- Dashboard tile is fed by `GET /api/public/best-line-alerts`.
