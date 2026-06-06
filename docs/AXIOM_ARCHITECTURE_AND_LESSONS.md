# Axiom — Architecture & Lessons Learned

A complete reference for the Axiom Kalshi auto-trader: the execution engine, the
Kalshi market mapping/resolver, the Edge Terminal ingestion pipeline, the
orchestration/settlement layers, and — most importantly — the hard-won lessons
from running this on **live money** in production.

> **Live money.** This system places real orders on Kalshi from a deployed
> Reserved VM (`market-mind-engine.replit.app`). Most of the "lessons" below are
> scars: each one cost real trades, real downtime, or real misfires before the
> invariant that prevents it was put in place. Treat the **Why** lines as
> load-bearing.

---

## 1. System overview

Axiom is a Python FastAPI service (`artifacts/api-server`) that turns betting
signals into risk-gated Kalshi orders. It runs in two **input modes**:

- **`pipeline`** — the full in-house engine: discover markets, run a swarm of
  LLM "debates", predict with calibrated models, gate, and place.
- **`edge_terminal`** — ingest pre-made picks scraped from an external product
  (Edge Terminal), resolve each to a Kalshi market, size, and fire.

In production the global mode is usually `edge_terminal`, with **specific
verticals exempted** (e.g. `weather`) so they run their full pipeline + place
trades while everything else only trades Edge Terminal picks.

### High-level data flow

```
                      ┌────────────────────────── pipeline mode ──────────────────────────┐
                      │  discover → swarm debate → predict (calibrated) → risk gates →      │
                      │                                                       place_order   │
  control plane       │                                                          │         │
  (input_mode) ───────┤                                                          ▼         │
                      │                                              ┌────────────────────┐ │
                      └─ edge_terminal mode ─┐                       │ paper.place_order  │ │
                                             │                       │  (the chokepoint)  │ │
  Edge Terminal ──scrape──> edge_terminal_   │   resolve_match       │         │          │ │
  (Playwright)             signals (DB) ─────┴─> (ticker + side) ───>│         ▼          │ │
                                                  + 5% flat size      │ kalshi/lifecycle   │ │
                                                  + 60% conf floor    │  submit_order      │ │
                                                                      └────────┬───────────┘ │
                                                                               ▼             │
                                                                       Kalshi REST + WS ─────┘
                                                                               │
                                                  settlement scheduler (~180s) ┘ → PnL, retrain, alerts
```

### Key directories

| Path | Responsibility |
|---|---|
| `app/edge_terminal/` | Scrape, store, resolve, and fire Edge Terminal picks |
| `app/orchestrator/` | Per-vertical trading cycle, the `place_order` chokepoint, venue routing |
| `app/kalshi/` | Kalshi order lifecycle (submit / rest / cancel / WS fills) |
| `app/risk/` | Risk gates, Kelly sizing, RiskContext |
| `app/settlement/` | Outcome settlement, model retrain/refit, drift monitoring |
| `app/notifications/` | Daily summary email, alerts fan-out, staleness paging |
| `app/verticals/` | Vertical registry + builtins (sports, weather, crypto, …) |

---

## 2. Edge Terminal ingestion pipeline (`app/edge_terminal/`)

### 2.1 The poller (`poller.py`)

A **single asyncio task** that loops while the input mode is `edge_terminal`,
with a jittered 40–60s interval. Each tick (`_one_tick`):

1. `scraper.scrape_signals()` — Playwright fetches current picks.
2. `_insert_signal(sig)` — UPSERT each signal into `edge_terminal_signals`.
3. `executor.drain_pending()` — resolve + size + fire any `pending` rows.

Status is tracked in `edge_terminal_poller_status` (id=1): `last_poll_at`,
`last_ok_at`, `last_error_category` (`ok`, `login_failed`, `browser_missing`,
`no_rows`, …).

> ⚠️ **Lesson — the poller can stall silently and permanently.**
> Because it is a single task, **any unbounded `await` in a tick wedges the
> entire loop forever**. The prime offender is Playwright (`launch()` /
> `close()` / driver IPC have no asyncio timeout). The rest of the process keeps
> running, the healthz sidecar keeps answering, so the prod watchdog sees a
> healthy app and **never restarts it**. Symptom: "tons of fresh signals but
> almost nothing trading," `last_poll_at` frozen hours back, `last_error_category
> = ok`, no exception.
> **Invariant:** every `await` on the tick must be wall-clock bounded with
> `asyncio.wait_for(...)`. Bounded today: the scrape (`SCRAPE_TICK_TIMEOUT_SECONDS`),
> the drain (`DRAIN_TICK_TIMEOUT_SECONDS`), and `browser.close()`. A
> per-navigation Playwright timeout is **not** enough. Trust `last_poll_at`
> recency, not process liveness. Recovery requires a **redeploy** — a wedged
> build won't self-heal.

### 2.2 The scraper (`scraper.py`)

Async Playwright/Chromium logs into Edge Terminal and walks the DOM
(`_SIGNAL_EXTRACT_JS`) for `div.game-row` elements with a `.signal-tag`. A
scraped signal carries:

- `vertical` (NHL, NBA, MLB, soccer, …)
- `matchup` (game description)
- `selection` (the pick, e.g. "Canadiens +1.5", "Under 2.5")
- `side` (YES/NO, inferred from selection text like "Under"/"No")
- `model_prob` (win probability %)
- `edge_pct` / `ev_pct` (value)
- `avg_odds` (American odds)

### 2.3 Signal lifecycle table (`edge_terminal_signals`)

Columns: `id`, `seen_at`, `vertical`, `matchup`, `selection`, `signal_date`,
`edge_pct`, `model_prob`, `market_price`, `status`, `status_reason`, `trades_id`,
`side`.

**Dedupe key:** `UNIQUE (vertical, matchup, selection, signal_date)` — one
pick per UTC day.

**Status values:**

| Status | Meaning |
|---|---|
| `pending` | Ready to process |
| `placed` | Order placed on the exchange (holds a real position) |
| `skipped_unresolved` | No matching Kalshi market found (often: market not listed yet) |
| `skipped_slippage` | Requested side empty/too thin to fill in budget at decision time |
| `skipped_gate` | Settled/closed game or risk-gate stand-down |
| `skipped_already_placed` | Deduped against an existing position |
| `skipped_low_confidence` | Below the 60% model-confidence floor |

> ⚠️ **Lesson — the re-arm boundary is a double-bet guard.**
> `_insert_signal` re-arms a conflicting row (sets `status='pending'`) **only**
> for the *transient* dispositions `skipped_unresolved` and `skipped_slippage`.
> - `skipped_unresolved`: Kalshi sports markets list only a few hours before
>   game time, but the board badges picks earlier — the first drain finds "no
>   live market." Without re-arm, the pick froze for the whole day and never
>   retried once its market opened (this was the root cause of "only BTC trades,
>   no sports").
> - `skipped_slippage`: the early book thickens as the session opens; retry
>   later.
> - **Never re-arm** `placed` (real position → double-bet on live money),
>   `skipped_already_placed`, or `skipped_gate` (settled game). Thrash is
>   naturally bounded because the board only shows current/upcoming games.
> Keep the WHERE clause in lockstep with `_REARM_STATUSES` in
> `tests/test_poller_rearm.py`.

> ⚠️ **Lesson — a vertical with no discovery feed silently never trades.**
> Even with models fully wired, a vertical that never upserts rows into the
> predictions/signal feed produces zero trades — and it fails *silently*. When a
> whole vertical is dark, suspect the feed before the model.

### 2.4 The executor drain (`executor.py`)

`drain_pending` fetches up to ~50 `pending` rows and calls `_fire_one`:

1. **Resolve** — `market_resolver.resolve_match(sig)` → ticker + side.
2. **Size** — `_flat_fraction_quantity`, targeting a **flat 5% of bankroll**
   (`FLAT_BANKROLL_FRACTION`); skip if quantity < 1 (no sub-$1 trades).
3. **Fire** — `paper.place_order(..., placed_by='edge_terminal')`.

**Confidence floor:** hardcoded `_MIN_MODEL_CONFIDENCE = 0.60`. Below 60% →
`skipped_low_confidence`.

`_classify_exception` maps errors to statuses: `market_not_found` / "Market
closed" → `skipped_gate` (routine expiry, not a system failure).

> ⚠️ **Lesson — "trades not firing" is usually the confidence floor, not a bug.**
> Signals live in **Supabase** (not the Replit DB — query via the `app.db` pool).
> The most common cause of "nothing's trading" is the 60% floor, not a defect.
> Real `skipped_unresolved` is concentrated in tennis/soccer (sparse Kalshi
> coverage), not MLB.

> ⚠️ **Lesson — `market_not_found` at order time is a clean skip, not a failure.**
> A settled/closed/wrong-ticker 404 should skip cleanly and **not** be counted
> as "failed." Keep the reason non-asserting so a genuine resolver bug isn't
> masked by a routine expiry.

> ⚠️ **Lesson — the executor resolves via `resolve_match`, not `resolve`.**
> Unit tests that stub only the legacy bare `resolve()` leave the real
> `resolve_match` running. With a scraper-supplied `kalshi_ticker` it returns
> early (passes); with `kalshi_ticker=None` it does DB/series lookups, finds
> nothing, and the signal is `skipped_unresolved` so the executor never reaches
> `place_order` (0 fires). Stub **both** `resolve` and `resolve_match`.

---

## 3. Kalshi market mapping / resolver (`app/edge_terminal/market_resolver.py`)

Translates a scraped signal into a concrete Kalshi ticker + side using a
three-tier strategy: **direct ticker → heuristic scoring → LLM fallback**.

### 3.1 `resolve_match(signal) -> ResolveResult`

`ResolveResult` fields:

- `.ticker` — resolved Kalshi ticker (e.g. `KXNHLSPREAD-26MAY24…`).
- `.side` — `YES`, `NO`, or `None` (honor the scraper). The resolver **owns**
  the side for spreads/handicaps.
- `.reason` — human-readable resolution path.
- `.kind` — `matched` / `no_series` / `no_match`.
- `.candidates` — scored candidate tickers (debugging).

### 3.2 Series mapping (`_SERIES_BY_VERTICAL_PICK`)

Maps `(vertical, pick_type)` → Kalshi series prefixes. Pick-type suffixes:
`moneyline → GAME`, `spread → SPREAD`, `total → TOTAL`.

- Static map for the major leagues (e.g. `('nhl','spread') → ['KXNHLSPREAD']`).
- **Dynamic discovery** (`_discover_series`) for soccer: there is **no generic
  `KXSOCCER*` series** — soccer is listed per competition (`KXEPL*`, `KXLALIGA*`,
  `KXSERIEA*`, `KXLIGUE1*`, `KXMLS*`, `KXUCL*`, `KXUEL*`, `KXBUNDES*`).
  Cross-league fixtures (e.g. PSG vs Arsenal) live under the cup series
  (KXUCL/KXUEL). Cricket: only club IPL (`KXIPL*`) exists — internationals have
  no Kalshi market.

### 3.3 Ticker anatomy & team matching

```
[SERIES]-[YYMONDD][TIME][TEAM_CODES]-[OUTCOME]
e.g.  KXMLBSPREAD-26MAY30NYYBOS-NYY2
                   └date┘└codes┘ └tail┘
```

- **Team-code segment** extracted via `_ticker_team_code` (the chunk after the
  date, e.g. `NYYBOS`) — match codes **only** here, never the whole ticker
  (series prefix/outcome suffix cause trigram collisions).
- **Evidence** (`_side_evidence`): curated aliases (`_TEAM_ALIASES`), FIFA nation
  codes (`_SOCCER_NATIONS`), trigrams, multi-word acronyms (PSG).
- **Gating:** `_date_ok` (ticker within ±1 day of signal) **and** `_both_teams`
  (BOTH matchup sides must appear) — applied identically at heuristic ranking
  **and** LLM acceptance.
- **Scoring** (`_score`): rewards team-token hits, team-code hits in the segment,
  and numeric line matches (especially the ticker "tail").

> ⚠️ **Lesson — a wrong-game bet is strictly worse than no bet.**
> A resolver (heuristic *or* LLM) will happily latch a line onto the wrong
> fixture (a totals number onto an unrelated game, a right-team/wrong-opponent
> market, or a stale market days off). **One-team overlap is not enough.** The
> accepted market must satisfy *all* of: date within ±1 day, **both** matchup
> sides present. Over-rejection (no trade) is the acceptable failure direction.
> The hallucination guard (`ticker ∈ candidates`) is insufficient because the
> wrong market is often a real candidate.

> ⚠️ **Lesson — side identity is subtle (shared cities/words).**
> Match short codes only in the team-code segment. City/place words shared by
> co-located rivals (new, york, los, angeles, san, chicago) must **not** prove a
> side — the ordered acronym (NYY vs NYM) does the work. A single shared
> non-place token must not prove a side either (Man City vs Man United): the
> name-token path requires ALL distinctive tokens; abbreviated names (PSG) are
> rescued by the team-code-segment match.

> ⚠️ **Lesson — `model_prob` is pick-relative; never flip it for NO.**
> The scraped `model_prob` is the win-probability of the *pick the tile shows*
> (an "Under"/"No" tile's % is already the NO side's confidence). It is **not**
> P(YES). For NO picks use `model_prob` **directly** for the confidence floor,
> the Kelly win-prob, and `edge_signed = model_prob - pick_price`.
> `pick_price = price` for YES, `1 - price` for NO. The Kalshi lifecycle stamps
> `limit_price_cents` directly as `no_price` for NO orders, so `RiskContext.
> current_price`, `intended_notional_usd`, and `cand["kalshi_price"]` must all
> carry `pick_price` — a NO order built with the YES quote bids 62¢ for a
> contract that should cost 38¢. The old `1 - model_prob` flip both rejected
> genuine +EV NO/Under picks and placed EV-negative ones; live prod showed **zero
> trades in 24h**.

> ⚠️ **Lesson — signed spreads: the resolver owns the target market AND side.**
> For "+/-" spreads, ignore the scraper's ticker; the resolver picks the target
> market and the side from the sign.

> ⚠️ **Lesson — a soccer half-goal handicap is a moneyline bet.**
> A 0.5 Asian handicap can never push, so it is a 3-way winner bet, not a spread.
> `<team> -0.5` == team must win → YES on the team's winner market.
> `<team> +0.5` == team wins or draws → NO on the **opponent's** winner market.
> Kalshi lists no 0.5 spread (lines start at 1.5), so a `+0.5` underdog had no
> spread candidate and died `skipped_unresolved`. Reroute `vertical=='soccer'` +
> spread + `|line|==0.5` to the GAME series and derive the side from the sign.
> 3-way winner markets (e.g. GEO/ROU/TIE) share one title — only the ticker tail
> code disambiguates; never let TIE win by accident.

> ⚠️ **Lesson — Kalshi national-team codes are FIFA, not first-three-letters.**
> Romania→ROU, Netherlands→NED, Spain→ESP. A curated nation→FIFA map is
> required; it also resolves collisions (Chile=CHI vs China=CHN, India=IND vs
> Indonesia=IDN). And the **scorer must use the same code evidence as the
> both-teams gate** — otherwise a market that genuinely named both teams passes
> the gate, then scores 0 on alias-only scoring (empty for soccer) and is
> discarded. Non-aliased sports silently never trade.

### 3.4 LLM resolver fallback (`llm_resolver.py`)

When heuristic score < `_MIN_SCORE` (5), `llm_pick` passes the signal + top ~15
filtered candidates to `gpt-4o-mini` with strict JSON, restricted to the
candidate list (no hallucination). The chosen ticker must still pass
`_both_teams`. Results cache in `edge_terminal_llm_resolver_cache` (keyed by a
hash of the signal).

---

## 4. Execution engine

### 4.1 The chokepoint — `paper.place_order` (`app/orchestrator/paper.py`)

The single gateway for **all** trades (paper + live, Kalshi + Polymarket, both
pipeline and Edge Terminal).

```python
async def place_order(ctx: RiskContext, *, debate_id, consensus, edge, kelly,
                      exchange="kalshi", cand=None,
                      placed_by="orchestrator", signal_id=None)
```

Writes one row to `trades`:
`trade_id, market_id, market_title, vertical, side, action, quantity, price,
fees(0), pnl(NULL), trading_mode, exchange, executed_at, model_prob, model_edge,
kelly_fraction, metadata(JSONB), placed_by`.

- **Mode** comes from `RiskContext.operator_overrides` or system settings.
- **Live:** applies **canary** (force qty=1) or **ramp** (gradual sizing) if
  enabled, then `_dispatch_via_lifecycle` → venue `lifecycle.submit_order`.
- For Kalshi it builds an `OrderRequest` and calls
  `kalshi_lifecycle.submit_order(req, force_paper=paper)`.

> **Note — `trades.price` is stored in DOLLARS** (e.g. 0.62), converted to cents
> at submit (`cents = round(price * 100)`). Anything rendering price (e.g. the
> daily email) must convert to cents itself.

### 4.2 Kalshi order lifecycle (`app/kalshi/lifecycle.py`)

- **`OrderRequest` / `to_kalshi_payload`** — maps `side` to `yes_price` /
  `no_price` (cents) and `time_in_force`. GTC is the default (omitted); FOK maps
  to `fill_or_kill`. `marketable_buffer_cents` is added to the FOK limit so it
  crosses the spread.
- **Slippage guard (`_check_slippage_guard`)** — projects the fill against the WS
  `orderbook_cache`; rejects or slices if slippage erodes too much edge.
- **Resting orders** — held in the in-memory `_resting` dict; aged out by the
  cycle stale-sweep.
- **`cancel_order`** — cancels via REST, then performs **authoritative fill
  reconciliation** (parse cancel response → `filled_count`, else a same-session
  `get_order` fallback) before popping `_resting`.
- **`handle_ws_fill`** — processes real-time fills, computes weighted-average
  prices, updates `order_fills` and `trades`.
- **`_attempt_submit_recovery`** — on 5xx/timeout, polls by `client_order_id` to
  determine whether an ambiguous order actually placed.

> ⚠️ **Lesson — Kalshi has no IOC; "marketable FOK" emulates it by sizing down.**
> There is no take-what's-there order type. A marketable FOK is **sized down to
> the fillable depth** (IOC emulation): it takes partial fills and never rests.
> Canary is off by default. Setting `AXIOM_MARKETABLE_FOK` off re-enables resting
> GTC.

> ⚠️ **Lesson — the slippage guard's "rest the limit" paths are GTC-only.**
> When the orderbook exists but our side has zero resting liquidity
> (`book_known=False`), the guard **allows** by default (returns `None`) so the
> order rests as a limit at the edge-capped price — worst case is an unfilled
> resting order, never a bad fill. Gated by
> `AXIOM_SLIPPAGE_GUARD_ALLOW_EMPTY_SIDE` (default True; set false to restore the
> hard-reject kill-switch).
> A second flag `AXIOM_SLIPPAGE_GUARD_REST_FULL_LIMIT` (default True) rests the
> **full** Kelly-sized limit on a thin/partial book instead of trimming to the
> immediately-fillable slice (the "why are my bets sub-$1?" fix). It **must** be
> gated on `book_known` so it can't override the empty-side kill-switch.
> Both "rest the limit" early-returns are **FOK-gated** (`and req.time_in_force
> != "FOK"`): a marketable FOK's remainder is killed by Kalshi, and a full-size
> FOK on a thin book 409s (`fill_or_kill_insufficient_resting_volume`). FOK falls
> through to slice-to-depth instead.
> **Hybrid:** an empty-side FOK converts to a resting GTC (strips the marketable
> buffer, since there's nothing to take), gated by
> `AXIOM_SLIPPAGE_GUARD_REST_EMPTY_SIDE_FOK` (default True). The liquid-book path
> is untouched (locked by a regression test).

> ⚠️ **Lesson — backstop re-arm must reconcile authoritative venue truth first.**
> When the stale-sweep cancels an UNFILLED empty-side GTC backstop it flips the
> signal `placed → skipped_slippage` to retry — but **only** when it is a
> backstop, `filled_count == 0`, AND `fill_count_authoritative` is True. The
> in-memory `filled_count` is updated only by WS events; a venue-side partial
> fill whose WS event is delayed would leave it a stale 0, and re-arming would
> re-buy exposure we already hold (double-bet on live money). `cancel_order`
> reconciles the cancel/`get_order` response (the final, authoritative fill
> state) before deciding. **Unknown fill state must never be treated as a clean
> zero.**

### 4.3 Risk & sizing (`app/risk/`)

- **`RiskContext`** — carries candidate info (`market_id`, `side`), pricing
  (`edge`, `model_probability`, `current_price`), bankroll snapshot, and open
  exposure.
- **Kelly** — the orchestrator computes intended size from Kelly;
  `gate_kelly_fraction` caps it against the `kelly_limit` setting; per-vertical
  `kelly_multiplier` tunes risk (e.g. 0.15 for commodities).
- **`run_all_gates(ctx)`** — ~12 gates including vertical-halt, stale-live-state,
  live-Kelly-multiplier (halves in-game size), concentration/exposure caps, and
  best-line. Gates can mutate `intended_quantity` before placement.
- Edge Terminal uses a simpler **flat 5%-of-bankroll** sizing instead of Kelly.

---

## 5. Orchestration & settlement

### 5.1 Trading cycle (`app/orchestrator/cycle.py`)

`run_cycle(vertical)` per vertical per tick:

1. **Stale-sweep** — `_cancel_stale_resting_orders` cancels resting orders older
   than `resting_order_max_age_secs` (default ~30s) so we never fill against a
   stale thesis. (Re-arms eligible backstops per §4.2.)
2. **Input-mode resolution** — `_resolve_input_mode()` reads the control plane
   **once** and **fails CLOSED** to `edge_terminal` on error.
3. **Pipeline exemption** — verticals in `settings.pipeline_verticals`
   (`AXIOM_PIPELINE_VERTICALS`) bypass the edge_terminal skip and place trades
   even when the global mode is `edge_terminal`.
4. **Skips** — if mode is `edge_terminal` and the vertical is **not** exempt:
   skip fresh debates (`_skip_fresh_debates`) and skip placement
   (`decision: skipped_input_mode_edge_terminal`).
5. Candidate processing (cached + fresh swarm debates) → predictors → gates →
   `place_order` across routed venues.

> ⚠️ **Lesson — `input_mode` must be read once per cycle and fail CLOSED.**
> The cycle previously read `get_input_mode()` twice, both **fail-OPEN** to
> `pipeline`. A transient control-plane blip on the second read could flip a
> non-exempt vertical's placement open and place live trades while the operator
> was in edge_terminal mode. The single fail-closed `_resolve_input_mode()` only
> ever holds a non-exempt vertical back, never the reverse. Any new placement
> decision keyed on input_mode must **reuse** the single resolved value.

> ⚠️ **Lesson — pipeline-exempt verticals need vertical-scoped readiness.**
> The boot-once global READY gate parks **all** verticals until global readiness;
> a pipeline-exempt vertical (e.g. weather) must unpark on its **own** discovery
> feed + core checks, not the global gate, or it never trades.

### 5.2 Settlement scheduler (`app/settlement/scheduler.py`)

Settles trades every ~180s. Heavy maintenance is **staggered** off the per-tick
path:

- Online retrain + isotonic refit — `AXIOM_RETRAIN_INTERVAL_SECONDS` (default 900s).
- Per-league margin refit — `AXIOM_MARGIN_REFIT_INTERVAL_SECONDS` (default 900s).
- Shadow loop — hourly, predicts every active market for drift monitoring.

> ⚠️ **Lesson — concentration of heavy refits caused recurring VM recreation.**
> Running both heavy sklearn steps on every settlement tick, on top of ~1.2 GiB
> resident models in a single-process Reserved VM, periodically starved the main
> event loop. The platform health check hits the **main app port**; when it timed
> out the platform recreated the VM (~40s outage, no traceback) ~every 15 min.
> Staggering fixed the *frequency*, not un-offloaded work. Core settlement still
> runs every tick — only the heavy maintenance is gated, and `_last_*_at` updates
> even on failed attempts to avoid a retry storm.

> ⚠️ **Lesson — monotonic cold-start gate gotcha.**
> Interval gates use `time.monotonic()` (host uptime on Linux). A freshly
> recreated VM can have uptime < interval, so a naive `now - last >= interval`
> with `last == 0.0` would **skip** the intended warm-once on cold start. Treat
> `last == 0.0` as explicitly due.

> ⚠️ **Lesson — `app.settlement` shadows its own `scheduler` submodule.**
> The package re-exports the `scheduler` *instance*, so `import
> app.settlement.scheduler` and `from app.settlement import scheduler` both bind
> the instance, not the module. Use `importlib.import_module(...)` to get the
> real module (e.g. in tests needing module-level helpers).

### 5.3 Notifications (`app/notifications/`)

- **`daily_summary.py`** — fires once per UTC day (default 13:00 UTC); collects
  PnL, fills, top movers, persona performance, and (now) **trades placed today**.
- **`alerts.py`** — Telegram/email fan-out with `dedupe_or_skip` via the
  `notification_dedupe` table.
- **`live_state_staleness.py`** — alerts if `sportradar_live_state` staleness
  exceeds 2× budget for `DEFAULT_CONSECUTIVE_CHECKS` (2) ticks; **suppressed in
  edge_terminal mode** (see below).

> ⚠️ **Lesson — the daily email counted only SETTLED trades.**
> It derived its trade view from realized PnL (`trades WHERE settled_at IS NOT
> NULL`). Same-day live Edge Terminal placements have no `settled_at` yet, so the
> email read "no trades" even when the bot traded all day. Fix: a separate
> "Trades placed today" section sourced from `trades.executed_at` (all
> sources/modes). **Placed ≠ settled** — an order-flow view and a realized-PnL
> view answer different questions and must be computed from different columns.
> Renderer params (`daily_pnl_summary`, `_render_daily_pnl_summary_html`) are
> invoked via `**summary` splat, so new params must be **keyword-only with
> defaults** or the whole digest crashes.

> ⚠️ **Lesson — live-state staleness paging is input-mode-gated.**
> The `[axiom][LIVE-STATE STALE] <sport> feed …` pager only fires when the mode
> is **not** `edge_terminal`. The sportradar live-state feed only drives the
> in-game prediction loop, which ships no sports trades in edge_terminal mode —
> so a stale feed is expected and paging on it is pure noise. The gate reuses the
> single fail-closed `_input_mode` and auto-restores in pipeline mode.
> Pipeline-exempt verticals (weather) carry no sportradar live-state, so the
> global-mode gate covers them too. Gating lives at the **call site** so the
> checker's unit tests stay valid.

### 5.4 Verticals (`app/verticals/registry.py`, `builtins.py`)

Registered via `VerticalSpec`: `sports`, `weather`, `crypto`, `crypto-binary`,
`commodities`, `tech-ai`, `soccer`, `combat-individual`, `elections`,
`events-econ`, `entertainment` (some, e.g. `politics`, `wnba_player_props`, start
disabled). Each spec defines `predictor_hook`, `kelly_multiplier`, `enabled`.

Key env vars:
- `AXIOM_ORCHESTRATOR_VERTICALS` — active verticals for the scheduler.
- `AXIOM_PIPELINE_VERTICALS` — verticals exempt from the edge_terminal placement skip.

---

## 6. Operational notes & gotchas

> ⚠️ **The api-server dev workflow has NO hot reload.**
> It runs uvicorn under a watchdog supervisor **without `--reload`**. Editing a
> `.py` file does **not** change the running server. This silently burns
> debugging time — a fix can be correct in the file (and verified by a standalone
> `python -c` import) while the live poller/executor keeps the OLD behavior.
> After **any** edit you intend to verify through the running server, call
> `restart_workflow("artifacts/api-server: API Server")`. A standalone import
> test proves the file, not the running server.

> ⚠️ **Config/code changes are INERT until deployed.**
> The trader runs on a Reserved VM. Setting env vars (e.g.
> `AXIOM_PIPELINE_VERTICALS`) in dev/shared changes nothing live until a
> **publish/deploy**. A wedged production build won't self-heal until the fixed
> code is live.

> **Where the data lives.** Signals and trades live in **Supabase Postgres**
> (not the Replit DB); query via the `app.db` pool.

### Environment-variable kill-switches (quick reference)

| Env var | Default | Effect |
|---|---|---|
| `AXIOM_ORCHESTRATOR_VERTICALS` | — | Active verticals for the scheduler |
| `AXIOM_PIPELINE_VERTICALS` | (empty) | Verticals that run full pipeline + place even in edge_terminal mode |
| `AXIOM_MARKETABLE_FOK` | on | Marketable FOK (IOC emulation); off → resting GTC |
| `AXIOM_SLIPPAGE_GUARD_ALLOW_EMPTY_SIDE` | True | Empty side rests as a limit; false → hard-reject |
| `AXIOM_SLIPPAGE_GUARD_REST_FULL_LIMIT` | True | Rest full size on thin book vs. trim to fillable slice |
| `AXIOM_SLIPPAGE_GUARD_REST_EMPTY_SIDE_FOK` | True | Empty-side FOK → resting GTC (strips buffer) |
| `AXIOM_RETRAIN_INTERVAL_SECONDS` | 900 | Online retrain + isotonic refit cadence (0 = every tick) |
| `AXIOM_MARGIN_REFIT_INTERVAL_SECONDS` | 900 | Per-league margin refit cadence (0 = every tick) |
| `AXIOM_LIVE_STATE_STALE_DEDUPE_TTL_SECONDS` | — | Staleness-alert dedupe TTL |

---

## 7. The lessons, in one place

A condensed index of the scars — read the section above each for the full
**Why/How**.

1. **The Edge Terminal poller can stall silently and permanently** — bound every
   per-tick `await` with `asyncio.wait_for`; trust `last_poll_at`, not liveness.
2. **The signal re-arm boundary is a double-bet guard** — re-arm only
   `skipped_unresolved` / `skipped_slippage`; never `placed` / `skipped_gate`.
3. **A vertical with no discovery feed silently never trades.**
4. **"Trades not firing" is usually the 60% confidence floor, not a bug.**
5. **`market_not_found` at order time is a clean skip, not a failure.**
6. **The executor resolves via `resolve_match`, not `resolve`** (stale-stub trap).
7. **A wrong-game bet is strictly worse than no bet** — require both teams + date.
8. **Side identity is subtle** — shared cities/tokens must not prove a side.
9. **`model_prob` is pick-relative; never flip it for NO.**
10. **Signed spreads: the resolver owns the target market AND the side.**
11. **A soccer half-goal handicap is a moneyline bet** (route to GAME series).
12. **Kalshi national-team codes are FIFA** — and the scorer must use the gate's
    code evidence.
13. **Kalshi has no IOC** — marketable FOK is sized down to fillable depth.
14. **The slippage guard's "rest the limit" paths are GTC-only** — FOK-gated.
15. **Backstop re-arm must reconcile authoritative venue truth first** (double-bet).
16. **`input_mode` must be read once per cycle and fail CLOSED.**
17. **Pipeline-exempt verticals need vertical-scoped readiness.**
18. **Don't concentrate heavy refits on every tick** — it recreates the VM.
19. **Monotonic cold-start gate gotcha** — treat `last == 0.0` as due.
20. **`app.settlement` shadows its own `scheduler` submodule** — use importlib.
21. **The daily email counted only settled trades** — placed ≠ settled.
22. **Live-state staleness paging is input-mode-gated** (noise in edge_terminal).
23. **The api-server dev workflow has no hot reload** — restart to verify.
24. **Config/code is inert until deployed** to the Reserved VM.
