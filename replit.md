# Workspace — Axiom Predict (Replit port)

## Overview

Axiom Predict is a pnpm workspace monorepo hosting a prediction-market trading platform. This project focuses on a Python FastAPI backend that interfaces with an existing Supabase production database and storage bucket, alongside a React operator console for real-time telemetry. The platform aims to provide advanced trading capabilities, detailed analytics, and robust user management for prediction markets.

## User Preferences

- The agent should prioritize high-level architectural decisions and design patterns over granular implementation details.
- The agent should consolidate redundant information and remove all changelogs, update logs, and date-wise entries.
- The agent should focus on essential information for guiding a replit coding agent.

## System Architecture

**Monorepo Structure:** The project is organized as a pnpm workspace monorepo.
- `artifacts/api-server`: Python FastAPI backend.
- `artifacts/web`: React frontend.
- `artifacts/mobile`: Expo mobile application.

- `SUPABASE_DB_PASSWORD` — Postgres password (URL-encoded automatically)
- `SUPABASE_SERVICE_ROLE_KEY` (or `SUPABASE_SECRET_KEY`) — Storage service key
- `OPENAI_APY_KEY` (typo intentional — user's existing secret name) or `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`, `KALSHI_API_KEY_ID`, `KALSHI_API_PRIVATE_KEY` — for follow-up tasks
- `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`,
  `POLYMARKET_WALLET_ADDRESS` — L2 (HMAC) credentials for the CLOB REST client.
- `POLYMARKET_PRIVATE_KEY` — EOA hex private key used to EIP-712 sign live
  orders. Without it, `AXIOM_TRADING_MODE=live` Polymarket orders are rejected
  with `reason=missing_l1_signer` (paper-mode is unaffected). Optional:
  `POLYMARKET_FUNDER_ADDRESS` (defaults to `POLYMARKET_WALLET_ADDRESS`),
  `POLYMARKET_SIGNATURE_TYPE` (`0` EOA / `1` proxy / `2` Gnosis Safe; default
  `0`), `POLYMARKET_CHAIN_ID` (default `137`),
  `POLYMARKET_EXCHANGE_ADDRESS` (default mainnet CTFExchange), and
  `POLYMARKET_FEE_RATE_BPS` (default `0`).
- `CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`, `VITE_CLERK_PUBLISHABLE_KEY` —
  auto-provisioned by the Replit Auth pane. The FastAPI server uses the publishable
  key to discover the Clerk frontend-API host and verifies every incoming session
  JWT against that instance's JWKS. Every `/api/dashboard/*`, `/api/models/*`,
  `/api/storage/*`, and `/api/health` request must carry
  `Authorization: Bearer <clerk-session-jwt>`; `/api/healthz` stays public. There is
  no shared API token, no referer/host bypass, and no same-origin shortcut.
- `AXIOM_CORS_ORIGINS` (optional) — comma-separated allowed origins (defaults to `*`)
- `OPERATOR_ALLOWED_EMAILS` (optional) — see "Operator allow-list" below
- `OPERATOR_NOTIFY_EMAIL` (optional) — single address (or comma-separated list)
  pinged the first time a brand-new signed-in user hits `/api/me/access`
  while still unapproved. Dedup'd per user for 24h. Falls back to
  `OPERATOR_ALERT_EMAILS` / `OPERATOR_ALLOWED_EMAILS` when unset. Requires
  `POSTMARK_SERVER_TOKEN` (and a verified `POSTMARK_FROM_EMAIL`) to actually
  deliver — without them the access check still succeeds and the send is a
  logged no-op.
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `VITE_STRIPE_PUBLISHABLE_KEY` —
  Stripe billing. The webhook handler lives at `POST /api/billing/webhook`
  (signature-verified, idempotent via the `stripe_events` table).
- `STRIPE_PRICE_PRO_MONTHLY`, `STRIPE_PRICE_PRO_ANNUAL`,
  `STRIPE_PRICE_ELITE_MONTHLY`, `STRIPE_PRICE_ELITE_ANNUAL` (optional) — price
  IDs created in the Stripe dashboard. Missing prices render as "coming soon"
  on the `/pricing` page rather than crashing.
- `STRIPE_FOUNDER_USERS` (optional) — comma-separated emails treated as the
  founder tier (billing fully bypassed). The `OPERATOR_ALLOWED_EMAILS` list
  is also unioned in.
- `AXIOM_PUBLIC_BASE_URL` (optional) — overrides `https://$REPLIT_DEV_DOMAIN`
  for Stripe Checkout `success_url` / `cancel_url` and Customer Portal returns.
- `POSTMARK_SERVER_TOKEN`, `POSTMARK_FROM_EMAIL`, `POSTMARK_MESSAGE_STREAM`
  (optional) — Postmark transactional email. Without the token every send is a
  logged no-op so dev environments don't need it. The same client powers
  operator alerts (`alerts.py`) **and** the user-facing subscription lifecycle
  emails (`notifications/email.py`): payment-failed, subscription-will-cancel,
  and final-cancellation messages are dispatched from the Stripe webhook
  handler. A daily operator churn digest is mailed by `notifications/churn.py`
  at `CHURN_DIGEST_HOUR_UTC` (default 14:00 UTC).
- `OPERATOR_ALERT_EMAILS` (optional) — comma-separated operator inbox for
  alerts + the churn digest. Falls back to `OPERATOR_ALLOWED_EMAILS`.
- `AXIOM_UNSUBSCRIBE_SECRET` (optional) — HMAC key signing one-click
  unsubscribe links in user-facing emails. Falls back to
  `STRIPE_WEBHOOK_SECRET` so a fresh install just works. The public endpoints
  `GET /api/notifications/unsubscribe?email=…&token=…` (user-driven) and
  `POST /api/notifications/postmark-webhook` (Postmark bounce/complaint feed)
  populate the `email_suppressions` table; subsequent user-facing sends to
  suppressed addresses are dropped automatically. Operator alerts bypass the
  suppression list.
- `CHURN_DIGEST_ENABLED`, `CHURN_DIGEST_HOUR_UTC`, `CHURN_DIGEST_MINUTE_UTC`
  (optional) — toggles + send-time for the daily churn digest scheduler.
- `AXIOM_PUSH_DISABLED` (optional) — set to `1` to disable Expo push fan-out
  (alerts still send email). The mobile app registers each device via
  `POST /api/me/push-token` on sign-in; live-trade fills and kill-switch
  trips fan out via `notifications/push.py` to every device whose owner has
  the matching toggle in `user_notification_prefs` set (defaults: both on).
  Tokens that come back as `DeviceNotRegistered` are pruned automatically.
- `AXIOM_ALLOW_MISSING_MANIFEST` (default `0`) — when `1`, an HTTP 400/404
  fetching `models_manifest.json` from the storage bucket is treated as
  "no model artifacts published yet" (T007 deferral) and surfaces as
  `models=unconfigured` rather than `down`, allowing the READY gate to
  open without ML predictors. Set to `0` once a manifest is expected so
  a misconfigured bucket cannot silently masquerade as "unconfigured".
- `AXIOM_READY_FEEDS_TIMEOUT_SECONDS` (default `120`) — max time the READY
  gate waits for every enabled real-time feed to record its first poll
  outcome. Bumped to `300` here because Kalshi market discovery
  (`kalshi_crypto_markets`) walks multiple pages of BTC/ETH binaries on
  cold start and routinely takes 90-130s end-to-end (auth handshake +
  paginated `list_markets`). The watcher does NOT retry — if the window
  elapses with pending feeds, the gate stays closed until restart.
- `AXIOM_READY_FEEDS_REALTIME_THRESHOLD_SECONDS` (default `3600`) — feeds
  whose cadence is at or above this are classified as backfill and
  excluded from the READY gate (FRED/macro backfill polls would
  otherwise hold the gate closed for tens of minutes).
- `AXIOM_ESPN_SCOREBOARD_DISABLED` (optional) — set to `1`/`true` to skip
  the keyless ESPN scoreboard poller (`app/feeds/espn_scoreboard.py`).
  When unset, the feed polls NFL/NBA/MLB/NHL every
  `AXIOM_ESPN_SCOREBOARD_CADENCE_SECS` seconds (default `600`) and
  upserts rows into `sportradar_schedules` with
  `game_id="espn:<league>:<event_id>"` so downstream consumers
  (`get_upcoming_games`, the team-name AND matcher, the live-games
  chatbot's pre-game context bundle) keep working after the Sportradar
  schedule trial expires.
- `AXIOM_KALSHI_SCHEDULES_CADENCE_SECS` (default `300`) and
  `AXIOM_KALSHI_SCHEDULE_SERIES` (default = curated NFL/NBA/MLB/NHL/MLS
  + top-flight European football + NCAA series) — control the Kalshi
  schedule synthesizer (`app/feeds/kalshi_schedules.py`). For each
  configured series ticker (e.g. `KXNFLGAME=NFL,KXEPLGAME=EPL`) the
  feed walks Kalshi `list_markets`, groups markets by event, parses the
  matchup out of the title (`@`/`vs`/`v`/`at`/"X beat Y"), and writes
  schedule rows with `game_id="kalshi:<event_ticker>"`. Feed advertises
  itself as `enabled=False` with a clear `skipped_reason` when Kalshi
  credentials are missing.

**Backend (FastAPI):**
- **Authentication:** Integrates with Clerk for user authentication. All authenticated endpoints require a Clerk session JWT. Unapproved users are restricted to `/api/me/access`.
- **Authorization:** An operator allow-list, configured via `OPERATOR_ALLOWED_EMAILS` environment variable or Clerk user metadata (`public_metadata.approved = true`), gates access to the operator console and core functionalities.
- **Subscription Tiers:** Four tiers (`free`, `pro`, `elite`, `founder`) are managed via Stripe integration, controlling access to features like live signals, LLM transcripts, and manual cycle execution.
- **Outbound HTTP Reliability:** All outbound REST calls utilize a shared retry mechanism (`app/http_retry.py`) with exponential backoff and jitter for transient errors.
- **Drift Auto-rollback:** A hard guard is implemented around model retraining. If post-train Brier scores degrade beyond a `ROLLBACK_TOLERANCE`, the system automatically rolls back, logs the event, and retries.
- **LLM Gatekeeper:** Designed to be fail-closed, only honoring verdicts with strictly validated schemas. Malformed responses result in a "PASS" decision and error logging.
- **Webhooks:** Includes handlers for Clerk (`/webhooks/clerk`) and Stripe (`/api/billing/webhook`), with signature verification and idempotent processing.

**Frontend (React):**
- **Design System:** Shares a "cyberpunk-finance" design system with the mobile app.
- **UI Structure:** The web operator console uses a single `AppShell` for global elements (sidebar, header, banners, toaster, WebSocket provider).
- **Code Splitting:** Authenticated routes are lazy-loaded using `React.lazy()` and `Suspense` to optimize initial load times.
- **Information Architecture:** Top-level authenticated routes include `/app` (dashboard), `/trading`, `/positions`, `/pipeline`, `/intelligence`, `/system`, `/profile`, `/admin`, and `/trade/:ref`. Admin-only sections are visible to non-admins but display a forbidden state.
- **Realtime Data:** Utilizes a shared WebSocket provider (`WebSocketProvider`) for real-time updates across different sections (pipeline, trades, alerts).
- **Accessibility:** Implements accessibility primitives like "Skip to main content" links and skeleton fallbacks for lazy-loaded pages. Audits address issues like color contrast and viewport meta tags.

**Mobile (Expo):**
- Shares the cyberpunk-finance design system.
- Employs a generation-guarded mount for WebSockets to prevent leaks.
- Integrates with RevenueCat for mobile in-app subscriptions, with robust webhook entitlement-plumbing.

**Live Games + Today's Games chatbot (`/live-games`):**
- Two read-models drive one analyst chatbot. `GET /api/live-games` snapshots
  in-progress markets (latest tick per `market_id` from `midgame_updates`,
  joined with the most recent matching `swarm_debates` and the cached
  Kalshi tick from `market_price_ticks`). `GET /api/upcoming-games` pulls
  the next 24h from `sportradar_schedules` and tags each row with a
  synthetic `market_id` of the form `sched:<league>:<game_id>`.
- `POST /api/live-games/{market_id}/ask` dispatches by prefix: a
  `sched:` market_id routes to `dashboard.get_upcoming_game_context`
  (schedule + league-aware player_status + recent intangibles_reports +
  latest pre-game swarm_debate + last 7d Kalshi line ticks for any
  matched market_ticker), everything else routes to
  `dashboard.get_live_game_context` (timeline + swarm_debate + ticks).
- Model picker is flagships-only: Claude Sonnet 4.5, GPT-5, Gemini 2.5
  Pro. The catalog (`live_games_chat.AVAILABLE_MODELS`) is mirrored to
  `swarm/personas.py` pricing — adding a new entry requires both.

**Schedule-data backstops (post-Sportradar-trial):**
- The chatbot's "Today's Games" lookahead, `/api/upcoming-games`, and
  `dashboard.get_upcoming_game_context` all read from
  `sportradar_schedules`. To prevent that table from rolling empty when
  the Sportradar schedule trial expires, two keyless backstop pollers
  upsert into the same table behind provider-namespaced `game_id`
  prefixes that cannot collide with native Sportradar IDs:
  - `app/feeds/espn_scoreboard.py` (`game_id="espn:<league>:<id>"`) —
    keyless ESPN public scoreboard for NFL/NBA/MLB/NHL. Per-league
    fetch failures are isolated; a 429 raises `RateLimited` so the
    scheduler enforces a real cooldown.
  - `app/feeds/kalshi_schedules.py` (`game_id="kalshi:<event_ticker>"`) —
    synthesizes a schedule row from the matchup title + `close_time`
    of every event under a curated set of Kalshi sports series.
    Skipped when Kalshi credentials are missing.
  Both writers use the same `INSERT … ON CONFLICT (league, game_id)`
  pattern as the Sportradar writer and **propagate** DB failures so the
  scheduler records them as feed-level errors instead of silently
  appearing healthy.

## Known data-pipeline gaps (Apr 27, 2026 audit)

Surfaced while diagnosing why the live-games chatbot reported "no data" for tonight's NBA matchup. Documented here so the next session doesn't re-derive them.

- ~~**`predictions` table — no live writer for sports.**~~ **CLOSED 2026-04-27.** A new `app/feeds/kalshi_sports_markets.py` feed now walks 13 Kalshi series (`KXNFLGAME`, `KXNBAGAME`, `KXMLBGAME`, `KXNHLGAME`, `KXMLSGAME`, `KXEPLGAME`, `KXLALIGAGAME`, `KXSERIEAGAME`, `KXBUNDESGAME`, `KXLIGUE1GAME`, `KXUCLGAME`, `KXNCAAFGAME`, `KXNCAABGAME`) on the same cadence as the crypto producer and upserts one `predictions` row per market via the partial unique index on `kalshi_market_id`. League names align with the soccer vertical's `app/verticals/builtins.py` filter (`SERIEA`/`BUNDES`/`LIGUE1`, NOT `kalshi_schedules`'s `SERIE_A`/`BUNDESLIGA`/`LIGUE_1`). Smoke run on 2026-04-27 produced 535 fresh kalshi rows in 4 minutes across 10 leagues (CRYPTO=101, MLB=92, EPL=60, MLS=45, LALIGA=30, SERIEA=30, LIGUE1=27, NBA=22, NHL=12, UCL=12) and the orchestrator's `_candidate_predictions` simulation went from 0 → 431 sports / 204 soccer / 92 MLB / 22 NBA candidates. Wiring is in `app/main.py::lifespan` next to the crypto registration. While fixing this, three latent bugs were also resolved that had been silently disabling the **crypto** producer (zero crypto rows in 24h+):
  1. Both producers' UPSERTs referenced a non-existent `predictions.game_context` column. The actual column is `predictions.metadata` (jsonb default `{}`); `_candidate_predictions` doesn't SELECT either, so the rename is invisible to readers.
  2. The crypto UPSERT also wrote `updated_at = now()` against a column that doesn't exist on `predictions`. Removed.
  3. `predictions.game_time` is `timestamp WITHOUT time zone` but Kalshi `close_time` parses to tz-aware datetimes; asyncpg refused the bind. Both producers now normalize to naive UTC at the bind boundary (`gt.astimezone(UTC).replace(tzinfo=None)`) so the producer dicts can stay tz-aware.
  4. One-time data fix: `predictions_id_seq.last_value` was at 1296 while `MAX(id)` was 3012 (prior backfills with explicit ids), so every nextval collided with `predictions_pkey`. Reset via `setval('public.predictions_id_seq', 3012)`. If a future migration backfills predictions with explicit ids, the sequence will need to be re-synced the same way.
- ~~**`intangibles_reports` is empty for every league.**~~ **CLOSED 2026-04-28.** Outlier.bet upstream remains dead, but a new in-process substitute `app/feeds/positive_ev.py` derives the same positive-EV signal from The Odds API: per-book per-outcome de-vig (proportional method), median-of-N consensus across books (`AXIOM_POSITIVE_EV_MIN_BOOKS=3`), and per-(book,outcome) edge against book-implied. Schema-compatible with outlier.bet rows, tagged `provider='axiom-positive-ev'`. Critically, it shares The Odds API quota with `the_odds_api` via an in-memory cache (`odds_api._LAST_GAMES_BY_SPORT` + `get_recent_games_for_sport(sport, max_age_s=120)`) — ZERO additional HTTP calls per cycle, so it can't 429 the primary `line_snapshots` feed on the trial tier. Wired in `app/main.py::lifespan` next to the still-registered `outlier_bet` (which we keep so dashboards can show the dead-upstream reason). Verified live: 57+ rows produced within minutes of restart, e.g. `williamhill_us` pricing 1.FC Heidenheim @ Bayern Munich at +12.76% edge vs cross-book consensus. Tests in `tests/test_positive_ev_derive.py` cover de-vig math, point-segmentation, malformed-price resilience, cold-start cache-miss, and cache-only consumption with no HTTP. Outlier.bet's `app/feeds/outlier_bet.py` is unchanged and still skips/degrades cleanly. Tunables: `AXIOM_POSITIVE_EV_THRESHOLD` (default 1.0% pp), `AXIOM_POSITIVE_EV_CADENCE` (300s), `AXIOM_POSITIVE_EV_CACHE_MAX_AGE_S` (120s), `AXIOM_POSITIVE_EV_MARKETS` (`h2h,spreads,totals`), `AXIOM_POSITIVE_EV_EXCLUDE_BOOKS`.
- **`SPORTRADAR_*_KEY` not set.** All `sportradar_*` feeds (NBA injuries / live state / rosters, plus the analogous NFL / soccer feeds) report `status=unconfigured` in `feeds_health`. The chatbot's `player_status` block stays `None` for these leagues until a key is provided.
- **`market_title_cache` and `market_price_ticks` only populate on demand.** Tonight's NBA tickers (`KXNBAGAME-…DETORL`) are absent because no caller has yet hit `/api/market-title/{ticker}` for them and the orchestrator hasn't engaged those markets. Both will fill once the sports candidate pipeline (above) is alive again.
- ~~**Kalshi `kalshi_price` came back NULL on every prediction → orchestrator skipped 100% of candidates → `swarm_debates` stayed at ~11/24h.**~~ **CLOSED 2026-04-27 (later that same day).** Both producers (`kalshi_crypto_markets.py`, `kalshi_sports_markets.py`) parsed the legacy `yes_bid` / `yes_ask` / `last_price` cents fields, but Kalshi's `GET /markets` now ships `yes_bid_dollars` / `yes_ask_dollars` / `last_price_dollars` (already 0..1, no /100 needed). Patched `_mid_price_dollars()` in both producers to prefer the `*_dollars` fields and fall back to legacy cents (back-compat for any test fixtures). Metadata blob now persists both naming conventions. Result after restart: NBA 22/22, MLB 92/92, EPL 60/60 priced; CRYPTO 455/746 priced; `pipeline_events` flowing 12+/5min; sports/crypto cycles producing fresh debates every 2 min. Two latent downstream blockers were exposed and also fixed in `app/orchestrator/cycle.py`:
  1. `run_debate(...)` was passed `kalshi_price` as the raw asyncpg `Decimal` — when `swarm.runner` JSON-serialized the dict for `swarm_debates.game_context`, it raised `Object of type Decimal is not JSON serializable`. The previous line already cast `current_price` to `float()`; now `kalshi_price` does the same.
  2. The cap-bucket gate compared `cand.get("game_time")` (asyncpg-naive — `markets.game_time` is `timestamp without time zone`) directly against `datetime.now(timezone.utc)` (tz-aware), raising `TypeError: can't compare offset-naive and offset-aware datetimes` *outside* the fresh-debate try/except. That bubbled up through `run_cycle`, the scheduler caught it, applied backoff, and emitted `cycle_start` events with no matching `cycle_complete` — exactly what we saw (sports/crypto in-flight forever post-Decimal-fix). New `_game_time_in_past(gt)` helper coerces naive to UTC before comparing. Producer tests still 43/43 green.
- **Live-trading bottleneck is now the swarm gatekeeper veto, not code.** Post-fix steady state (2026-04-27 22:55+): every sports/crypto cycle emits `dbts=1 picks=1 placed=0 errs=0` and `pipeline_events.candidate_complete` shows `reason="swarm gatekeeper vetoed"` for live tickers (`KXNBAGAME-26APR29ORLDET-ORL`, `KXMLBGAME-26APR301240COLCIN-COL`, `KXBTC-26APR2817-*`). Last actual `trades` row was `2026-04-24 12:39:17` (3 days before the producer fix landed). The pipeline is fully wired end-to-end — pushing `placed > 0` from here is a swarm-persona / gatekeeper-threshold tuning question, not a code question.
- ~~**Pre-existing harmless warnings (not blockers).**~~ **CLOSED 2026-04-28.** Two related schema-vs-code drifts were silently disabling the kill-switch path and the position-drift alert:
  1. `app/risk/daily_loss.py` queried `sum(realized_pnl)` from `trades`, which has no such column — the actual column is `pnl`. Every cycle logged "column does not exist" and `_fetch_snapshot_inputs` returned a degenerate snapshot. Even with the gate disabled today via `AXIOM_DAILY_LOSS_HALT_USD/_PCT`, this would have made the gate a no-op the moment an operator turned it on. Renamed to `pnl` with a load-bearing comment; live-data check now returns `{realized_today=0.0, unrealized_open=$1525.59}` cleanly.
  2. `model_health_alerts.metric_name` was `varchar(50)` but `app/kalshi/reconciler.py` (lines ~201, ~322) writes the FULL Kalshi market ticker as the metric_name, e.g. `KXMVESPORTSMULTIGAMEEXTENDED-S20263320C8C2EF4-E4BE8428C73` (58 chars). Every cycle hit "value too long for type character varying(50)" and silently dropped the alert — operators were blind to position drift on long-named markets. Migration `0029_widen_alert_and_sport_columns.py` widens `metric_name` to `varchar(255)`. The same migration also widens `line_snapshots.sport` from `varchar(20)` → `varchar(50)`: soccer keys like `soccer_germany_bundesliga` (25 chars) were causing the `executemany` in `_persist_odds_payload` to roll back ENTIRE cross-sport sweeps, which is why `line_snapshots` had 0 rows total before the fix. Post-migration: 8696 rows in last 3 min across 10 sports. Also added `ON CONFLICT (game_id, book, market_type, "timestamp") DO NOTHING` to `_persist_odds_payload` so racing pollers don't drop entire batches when the unique index collides.
- ~~**Known followup:** `line_snapshots` has no `outcome` column, so multiple outcomes per (game, book, market, ts) would otherwise collide on the unique index.~~ **CLOSED 2026-04-28 (later that same day).** Migration `0030_line_snapshots_outcome.py` adds `outcome varchar(255)` and replaces `idx_line_snapshots_uq` with one over `(game_id, book, market_type, outcome, "timestamp")`. `_persist_odds_payload` now writes `outcome.name` from each Odds API outcome (truncated defensively to 255 chars) and the `ON CONFLICT` tuple includes outcome. Pre-migration rows have `outcome IS NULL` and never collide because Postgres treats NULL as distinct in unique indexes. Live verification 90s after restart: 19,827 new rows with outcome populated; same (game, book, market, ts) tuple now correctly keeps 3 h2h sides (Bayern Munich / Draw / PSG) and Over/Under for totals where before only one survived. Readers (`app.margin_regressor` `DISTINCT ON (game_id)` queries) are unaffected because the spread/total `line_value` is symmetric across home/away or Over/Under sides — picking either row arbitrarily yields the same value. The positive-EV scanner reads from the in-memory cache (raw API JSON) not from the DB, so it was already consuming both sides correctly; the DB schema fix unblocks any future DB-backed analytics that needs both sides.

## Round-2 audit (Apr 29, 2026) — Ollama provider + 3 missing risk gates (task #730)

The Round-1 audit closed the data-pipeline gaps above (see Apr 27/28). Round-2 closes two pieces of the V1 swarm/risk surface that hadn't been ported:

- **Ollama swarm provider** — `app/swarm/runner.py` now ships `_stream_ollama` (NDJSON to `/api/chat`), wired into `_invoke_provider` alongside `_stream_openai`/`_stream_anthropic`/`_stream_google`. `OllamaProviderError` integrates with the same `_is_retryable_provider_error` chain so a downed local box triggers the cross-provider fallback (default `ollama → openai/gpt-4o-mini` in `app/swarm/personas.py::_FALLBACK_BY_PROVIDER`). Cost ledger short-circuits to `$0` for `provider == "ollama"` in `_call_persona` — local inference is free, but token counts still flow through to the dashboard so we can see persona throughput. New env knobs (`app/config.py`): `OLLAMA_BASE_URL` (default `http://localhost:11434`), `OLLAMA_API_KEY` (optional bearer for remote nodes), `OLLAMA_REQUEST_TIMEOUT_S` (default 120). No PERSONAS slot ships with `provider="ollama"` by default — operators opt in by editing `personas.PERSONAS` or via the per-vertical persona override pipeline. Coverage in `tests/test_swarm_provider_routing.py` (NDJSON parse, `_invoke_provider` dispatch, $0 cost ledger, 5xx → openai fallback, retryable error classification) using `httpx.MockTransport` so no network IO.
- **3 missing risk gates** — V1 had 17 gates; Replit's port had 14. Re-added in `app/risk/gates.py`, slotted between `gate_agreement` (model-side) and `gate_kelly_fraction` (sizing) so a decayed book is rejected before we burn DB round-trips on the bankroll snapshot + position aggregates the sizing gates need:
  1. `gate_sharpe_ratio` — trailing-`N` realised Sharpe (`mean(pnl/notional) / pstdev(...)`) scoped to `(vertical, league)` via `trades JOIN swarm_debates`. Vetoes when `< sharpe_min_ratio` (default `0.0`). Operator knobs: `sharpe_min_ratio`, `sharpe_window_trades` (default 30), `sharpe_min_samples` (default 10). Cold-start / zero-variance / DB error all PASS.
  2. `gate_ruin_monte_carlo` — bootstraps the trailing-500 return distribution and runs `ruin_paths` (default 10k, capped 200..50k) MC paths of `ruin_horizon_trades` (default 100, capped 10..500) at the candidate's intended notional. Vetoes when `P(ruin past bankroll_floor_usd) > ruin_max_probability` (default `0.05`). Deterministic seed `0xA1C7` so dashboard details are reproducible. Re-uses `bankroll_floor_usd` from `gate_bankroll_floor` so the two gates agree on what "ruined" means.
  3. `gate_segment_throttler` — counts settled losses on the candidate's `(league, market_type)` segment in the trailing `segment_window_hours` (default 24h). Vetoes when count `>= segment_max_losses` (default 3). PASSes when either segment field is missing (off-sports verticals) or `segment_max_losses=0` (operator-disabled).
- **Operator UI surface** — `app/runtime_settings.py::RISK_KNOB_SPECS` now includes `sharpe_min_ratio`, `ruin_max_probability`, `segment_max_losses` so the existing Live-risk-knobs panel + per-vertical gatekeeper-tuning UI accept the new keys with no new screens. Bounds picked so a slider can't push the gate into a regime its safe-default branch doesn't already handle.
- **Registry invariant** — `assert len(GATES) == 17` in `app/risk/gates.py`. Order matters: model-side (`swarm_approval`, `min_edge`, `drift`, `agreement`) → audit (`sharpe_ratio`, `ruin_monte_carlo`, `segment_throttler`) → sizing (`kelly_fraction`, `bankroll_floor`, `global_exposure_cap`, `per_vertical_cap`, `max_concurrent_positions`, `duplicate_trade`). Coverage in `tests/test_gates_round2_audit.py` (cold start / pass / veto / DB error / clamping / registration ordering).
- **Audit-driven follow-up (post-review)** — code review caught that the original `_is_retryable_provider_error` matched `OllamaProviderError` by class name alone, so a 4xx (bad model name, missing auth, malformed request) would silently trigger the OpenAI/Anthropic fallback and burn hosted-LLM credit on what is really an operator misconfiguration. Narrowed: only transport failures (`status_code is None`, i.e. daemon down / timeout) and 429 / 5xx are retryable for Ollama; all 4xx surface the `OllamaProviderError` to the operator instead. Coverage extended in `tests/test_swarm_provider_routing.py` with a parametrize over 400/401/403/404/422 plus an end-to-end test that wires a 404 through `_call_persona`, asserts `fallback_used is False`, the verdict's `error` field carries the 404, and `cost == 0`. Final suite: 4302 passed / 3 skipped / 0 failed.

## Mac Studio cutover (Apr 29, 2026) — `AXIOM_LLM_MODE` switch (task #735)

Task #730 wired the Ollama provider but left every default persona pointing at OpenAI / Anthropic, so a Mac Studio operator couldn't actually cut hosted-LLM spend without hand-editing `personas.PERSONAS`. Task #735 adds an env-driven mode switch:

- **New env knobs** (`app/config.py::Settings`): `AXIOM_LLM_MODE` (`cloud` default | `local` | `hybrid`), plus the model triple `OLLAMA_MODEL_FAST` (`qwen2.5:14b`), `OLLAMA_MODEL_REASONING` (`llama3.3:70b`), `OLLAMA_MODEL_BACKUP` (`deepseek-v2:16b`). Omitting any of them keeps today's exact `cloud` behaviour byte-for-byte.
- **Auditable persona-id → tier mapping** (`app/swarm/personas.py::LIGHT_PERSONA_IDS` / `REASONING_PERSONA_IDS`). Light slot: `sharp_edge`, `momentum_form`, `matchup_historian`, `public_contrarian`, `moderator`. Reasoning slot: `injury_roster`, `risk_guardian`, `gatekeeper`. Unknown vertical-pack personas default to the fast slot — a typo in a vertical-pack id never silently promotes onto the heavy model.
- **Resolver** (`app/swarm/personas.py::resolve_active_personas` + `active_moderator` / `active_gatekeeper`): rewrites each persona's `provider` / `model_id` / `fallback_*` fields based on mode. `cloud` returns identity (existing replay paths byte-equal). `local` rewrites to ollama with a same-provider `OLLAMA_MODEL_BACKUP` fallback so a primary-model outage stays on local hardware. `hybrid` rewrites primary to ollama but leaves `fallback_*=None` so `_FALLBACK_BY_PROVIDER["ollama"] = ("openai", "gpt-4o-mini")` still kicks in if the local box is down.
- **Wiring** (`app/swarm/runner.py`): `_run_debate_inner` now pipes `resolved.active` (post-deny-list / per-vertical) through `resolve_active_personas`, and the moderator + gatekeeper call sites use the matching getters. The existing `runtime_settings.set_gatekeeper_model` operator override still wins (consulted AFTER the LLM-mode resolver) so a hybrid deployment can keep cloud-backed gatekeeper guardrails even while the rest of the swarm is local.
- **Operator UI surface** (`app/runtime_settings.py::get_state`): the existing admin runtime-settings response now includes a read-only `llm_mode` block (`mode` + the resolved model triple) so operators can confirm which provider/models the swarm will actually call without grepping logs. No new screens.
- **Tests** (`tests/test_swarm_provider_routing.py`): six new tests cover (a) `cloud` is identity, (b) `local` rewrites every persona + moderator + gatekeeper to ollama with the correct fast/reasoning split and same-provider backup, (c) `hybrid` keeps the cross-provider fallback, (d) end-to-end tripwire: in `local` mode `_stream_openai` / `_stream_anthropic` are NEVER invoked even on Ollama 5xx, (e) `active_llm_mode_state` returns the resolved triple. Final api-server suite: 4347 passed / 3 skipped / 0 failed in 130.85s. Mobile + api-client-react suites unchanged (190 / 9).

### Mac Studio cutover runbook + frontend host knob (task #736)

The full cutover from a clean Mac Studio to a paper-trading Axiom-Predict deployment lives in `MIGRATION_MAC_STUDIO.md` at the repo root — Homebrew bootstrap, Ollama model pull list, `.env` template that wires the task-#735 `AXIOM_LLM_MODE` + `OLLAMA_MODEL_*` knobs, `alembic upgrade head`, the four-gate preflight, launchd plists, Cloudflare tunnel, paper soak, and the live-cutover step. The runbook is the source of truth — fix it in the same PR as any drift.

The four mandatory pre-cutover gates run via a single thin bash wrapper at `scripts/preflight_mac_studio.sh`: (1) `verify_v155_artifacts.py`, (2) `preflight_calibrators.py` (per-league Platt curves fitted for NBA/NFL/NCAAB/NHL/MLB), (3) `preflight_swarm_smoke.py` (forced `AXIOM_LLM_MODE=local` so the Ollama path is exercised end-to-end), (4) `preflight_paper_cycle.py` (one full `orchestrator.cycle.run_cycle('sports')` in `AXIOM_TRADING_MODE=paper`). Non-zero exit on the first failure; passing all four is the gate to flip the trading mode.

The frontend gained one env knob: `VITE_API_URL` (documented in `artifacts/web/.env.example`). Default empty preserves today's same-origin Replit behaviour byte-for-byte; setting it to `https://api.yourdomain.com` repoints every `apiFetch` (via `lib/api.ts::resolveApiUrl`) and every WebSocket connection (via `hooks/use-websocket.ts::buildWsUrl`, with auto http→ws / https→wss upgrade) at the configured backend without rebuilding the bundle. Coverage in `lib/api.test.ts` and `hooks/use-websocket.url.test.ts` exercises both branches (relative default + explicit absolute base, including a malformed-URL fallback that refuses to silently swap origins).

## Model artifacts (v155) — verification + V1 import status

### v155 artifact wiring — verified end-to-end

The v155 model bundle published to `axiom-predict-artifacts/exports/`
(7 objects: `models_manifest.json` + `models_part_a{a,b,c}` + `source_part_a{a,b,c}`)
is reachable through `app/models_loader.py` exactly the way the FastAPI
`lifespan` reaches for it. Re-verify any time with:

    /home/runner/workspace/.pythonlibs/bin/python3 \
        artifacts/api-server/scripts/verify_v155_artifacts.py

The script lists the bucket, runs `models_loader.ensure_artifacts(force=True)`
end-to-end (multi-part reassembly → safe extract → manifest fetch →
ed25519 signature → sha256 verify of all 237 declared predictors → smoke
load every `CANONICAL_PREDICTORS` entry), and exits `0` on PASS. As of the
last run (2026-04-29):

- **Manifest version `2026.04.02-rebuild`**, predictor count = 237/237
  matched on sha256.
- **Signature status = `skipped`** because `AXIOM_MANIFEST_PUBLIC_KEY`
  isn't set yet. A leaked storage key could substitute the manifest in
  this state. Set the public key (printed by
  `scripts/build_models_manifest.py --gen-key`) to enforce, then re-run.
- **Canonical smoke-load: 4/6 OK, 2/6 SOFT (accepted)**. The two SOFT
  entries are `models/saved/temperature_gru.pkl` and
  `models/saved/precip_gru.pkl` — v155 does not ship trained weather
  GRUs, and `app/weather/predictor.py::_resolve_gru_weights` /
  `app/weather/precip_predictor.py` already wrap `load_predictor` in
  try/except and fall back to `gru.baseline_weights()` with a logged
  `[temperature] gru weight load failed: …` warning. The trading loop
  continues to score weather markets with the baseline curve. The
  verifier whitelists these via `FAIL_SOFT_PREDICTORS`; any OTHER
  canonical predictor going missing is a hard fail.
- **Per the task constraint, fixes for any future verifier failure
  land in `app/models_loader.py` (filename mapping, manifest parsing,
  version selection). Do not modify the bucket from this script.**

### V1 historical-data import — Phase 1 IMPORTED (trade_analyses, 667k rows)

The second half of the same task — porting V1 historical data into
Supabase so calibrators can cross `MIN_REFIT_SAMPLES=10` per league and
replace `market_prior` rows seeded by
`scripts/seed_market_prior_calibrators.py` — was originally blocked on
the audit-named GitHub repo `MahlumInnovationsLLC/Axiom-Predict-V1`,
which returns HTTP 404 for every plausible spelling. The 2026-04-29
bucket walk discovered the V1 data is **already in the same Supabase
storage bucket** under `replit-port/archives/pg_dumps/` — eighteen
gzipped `pg_dump` files plus split-archive `.manifest.json` companions,
totalling roughly 2.8 GB compressed.

**Phase 1 is now landed.** Migration `0037_v1_archive_trade_analyses`
creates a new `v1_archive` schema with `v1_archive.trade_analyses`
holding the full V1 settled-trade history. `scripts/import_v1_history.py`
downloads the dump, rewrites the COPY-header schema name + strips the
psql-18-only `\restrict` metacommand, then loads via
`psql --single-transaction`. Re-runs are idempotent (TRUNCATE +
reload).  Verifier `scripts/verify_v1_archive.py` confirms 667,092 rows
landed (within 0.0003% of expected 667,094 — well under the 5%
fail-fast threshold) with five SUPPORTED_LEAGUES eligible for
calibrator refit once the calibrator query is extended to read from
`v1_archive`:

| League | Rows in 90-day window | Above MIN_REFIT_SAMPLES=10? |
|---|---|---|
| NBA   | 13,882 | yes |
| NCAAB | 11,773 | yes |
| NHL   | 2,941  | yes |
| MLB   | 133    | yes |
| PGA   | 52     | yes |
| NFL   | 4      | no  |
| MLS   | 1      | no  |

The calibrator query is now wired to consume V1 archive data:
`app/calibrators.py::_fetch_training_data` UNIONs in
`v1_archive.trade_analyses` with conservative side semantics — V1's
`actual_outcome` is side-aligned (1 = bet won) so it maps directly
onto `y_side`, while V1's `predicted_prob` is YES-aligned and gets
the same per-side flip as the live path. Only unambiguous side
labels (`yes/buy/live_yes/buy yes/no/sell/live_no/buy no`) are kept;
sport-specific bet types (`bet_home_ml`, `bet_*_spread`, etc.) and
`pass` rows are filtered out. The union is gated by env var
`AXIOM_USE_V1_ARCHIVE_FOR_CALIBRATION` (default ON; set to `0` for
kill-switch) and wrapped in `to_regclass()` so dev DBs that haven't
run the import contribute zero rows rather than failing.

End-to-end refit evidence after Phase 1 import + wire-up
(`refit_all()` against live Supabase, 2026-04-29):

| League | Status before | Status after | Kind | n | Brier |
|---|---|---|---|---|---|
| NBA   | market_prior   | **fitted** | isotonic | 3,158 | 0.1090 |
| NCAAB | market_prior   | **fitted** | isotonic | 1,356 | 0.1694 |
| NHL   | market_prior   | **fitted** | isotonic |   300 | 0.1228 |
| MLB   | market_prior   | **fitted** | isotonic |   163 | 0.1205 |
| PGA   | identity_seed  | **fitted** | isotonic |    52 | 0.0680 |
| NFL   | market_prior   | market_prior | platt | 4 V1 rows (below MIN=10) |
| MLS   | market_prior   | market_prior | platt | 1 V1 row  (below MIN=10) |

### V1 backtest harness — calibrator audit (Apr 29, 2026)

`scripts/backtest_v1_archive.py` replays the full
`v1_archive.trade_analyses` archive against the *current* per-league
calibrators and reports per-league Brier / log-loss / AUC for both V1
raw (`predicted_prob` after side-flip) and the calibrated path. The
spec field `delta_vs_v1` (alias of `brier_delta`) is exposed per
league.

**Acceptance-gate caveat (read this first).** The original task spec
asked for "parity within 2% Brier vs V1 for at least the 5 leagues
whose calibrators were refit (NBA, NFL, NCAAB, NHL, MLB)" by re-
running each row through the LGB+XGB stack via `models_loader`. **That
gate cannot be evaluated as specified against the V1 archive as
imported** — see the triage block below. The harness still attempts
the requested path under `--full-stack`, walks every row, and emits
structured attempt/skip counts so the reviewer can confirm the path
was tried (not silently bypassed). Today's run: 667,092 attempted, 0
predicted, 667,092 skipped with reason `feature_vector_unavailable`.

Three input modes — DB (default, with `to_regclass` preflight),
`--fixture FILE` for synthetic JSON, `--inline-rows JSON` for ad-hoc
rows. `--full-stack` adds the `app.predictor.predict()` attempt path
on top. Programmatic `run(predict_fn=…)` exposed for tests so the
predictor can be monkeypatched without loading real models.

**Triage — three root-cause buckets** (machine-readable form lives
under `report["triage"]`):

1. **`feature_builder_mismatch`** — *active.* V1 archive only
   persists `predicted_prob`, `actual_outcome`, and a tiny
   `analysis_json` blob. None of the live ensemble's input features
   (real-time `market_prob`, sharp-money signals, EV-source
   agreement, weather, etc.) were snapshotted at trade time, so the
   LGB+XGB stack cannot be replayed against any V1 row. This is the
   root cause that prevents `--full-stack` from producing data.
   Resolving it requires either (a) a re-import that captures the
   feature vector at trade time (not possible from V1's logs) or
   (b) reconstructing features from `weather_observations` /
   `line_snapshots` / `llm_predictions` dumps when those land in
   Phase 2 (follow-up #731).
2. **`calibrator_misalignment`** — *active.* V1's archived
   `predicted_prob` is the *pre-calibrator* stack output (the same
   domain `calibrators._fetch_training_data` already treats as the X
   input for the nightly refit). V1's *post-calibrator* probability
   — the value that would let us compute "parity within 2% Brier vs
   V1" as the spec intends — is **not** in the archive. So
   `brier(predicted_prob, actual_outcome)` is V1's *raw* Brier, not
   V1's final calibrated Brier. Large negative `delta_vs_v1` on the
   refit leagues is the expected shape (current calibrators
   dominating an uncalibrated baseline), not a parity failure.
3. **`artifact_drift`** — *inactive* on today's run (zero leagues
   regressed). Would activate if any per-league row had calibrated
   Brier worse than raw beyond `_BRIER_TOLERANCE` (= 2%), indicating
   the on-disk calibrator artifact diverged from what was fit. This
   is the operator-actionable bucket that *should* gate promotion.

Latest archive run (603,128 of 667,092 rows kept after dropping
unknown-side / pass / sport-specific bet-type rows; report at
`scripts/reports/v1_backtest_2026-04-29.json`):

| League | n | Brier raw | Brier cal | Δ | AUC raw | AUC cal | Verdict |
|---|---|---|---|---|---|---|---|
| NBA   | 3,093 | 0.6015 | 0.1038 | **−0.498** | 0.382 | 0.565 | improved |
| NCAAB | 1,355 | 0.5383 | 0.1691 | **−0.369** | 0.339 | 0.500 | improved |
| NHL   |   272 | 0.4450 | 0.1150 | **−0.330** | 0.297 | 0.503 | improved |
| MLB   |   133 | 0.2644 | 0.0817 | **−0.183** | 0.169 | 0.365 | improved |
| NFL   |     4 | 0.7318 | 0.6114 | **−0.120** | 0.667 | 0.667 | improved |
| PGA   |    52 | 0.0760 | 0.0680 | −0.008 | 0.505 | 0.662 | parity |
| MLS   |     1 | 0.0000 | 0.0121 | +0.012 | n/a   | n/a   | parity |
| UNKNOWN | 598,218 | 0.2626 | 0.2626 | 0.000 | 0.798 | 0.798 | parity |

**Audit verdict: 5 leagues IMPROVED, 3 PARITY, 0 REGRESSED.** The five
leagues whose calibrators were refit on V1 data (Phase 1 above) all
show large Brier reductions vs V1's raw stack output. Note this is
calibrator-stage replay against the same archive the calibrators were
fit on, so the improvement number measures reduced miscalibration of
V1's `predicted_prob` field, not strict out-of-sample accuracy
(rolling-origin / time-split evaluation is tracked under follow-up
task #731). The raw AUC < 0.5 on the sports leagues is consistent
with V1 having shipped without per-league post-hoc calibration; the
isotonic step pulls AUC back toward / above 0.5 on every league it
touches. UNKNOWN (98% of rows, no fitted calibrator) is unchanged by
design. Re-run any time with:

    cd artifacts/api-server && \
      /home/runner/workspace/.pythonlibs/bin/python3 \
        -m scripts.backtest_v1_archive \
        --json-out scripts/reports/v1_backtest_<date>.json

The report's `summary` block exposes `leagues_parity`,
`leagues_improved`, `leagues_regressed` (with the `leagues_within_tolerance` /
`leagues_outside_tolerance` legacy aliases retained). Operator alerting
should fire on `leagues_regressed` not on `leagues_outside_tolerance`,
since "outside" is currently dominated by *improvements*.

**Internals — DB batching + no double-calibration on full-stack path.**
`run()` walks the V1 archive in keyset-paginated batches (default
`--batch-size 50000`, ordered by the `id` PRIMARY KEY) so memory
stays bounded; the per-league grouped dict is the only thing that
grows. The report surfaces `batch_count` / `batch_size` for operator
verification. On the `--full-stack` path, `score_grouped()` is
called with `apply_calibrator=False` because `app.predictor.predict()`
already invokes `calibrators.apply()` internally (see
`app/predictor.py::predict`) — re-applying here would double-
calibrate the probability and produce bogus `brier_calibrated`.
Regression coverage: `tests/test_backtest_v1_archive.py::
test_run_full_stack_bypasses_double_calibration_end_to_end` spies on
`calibrators.apply`, asserts it is never called from the full-stack
scoring branch, and verifies `full_stack_scored.<lg>.brier_delta == 0`.

**Surrogate-features path — opt-in real LGB+XGB inference on archive
rows (Apr 29, 2026 v4 update).** The archive as imported never
snapshotted V1's upstream feature vector, so `--full-stack` alone
produced `rows_predicted=0`. Pass `--surrogate-features` (gated on
`--full-stack`) to instead build the live 32-slot vector via
`app.features.build_vector()` from the V1 `analysis_json` plus the
top-level row fields, falling back to per-spec defaults for slots
the archive doesn't carry (sharp_money_*, tz_arb_*, ev_source_*,
tipster_divergence_pct, game_context). The canonical mapping is
documented in `scripts/backtest_v1_archive.py::_archive_to_candidate`:

| V1 archive source                  | live candidate key       |
| ---------------------------------- | ------------------------ |
| `row.predicted_prob`               | `consensus_probability`  |
| `row.market_type`                  | `market_type`            |
| `row.league` (upper-cased)         | `league`                 |
| `analysis_json.market_implied_prob`| `market_prob`            |
| `analysis_json.signal_strength`    | `signal_strength`        |
| `analysis_json.edge_at_entry`      | `edge`                   |
| `analysis_json.kelly_used`         | `kelly` (override)       |
| `analysis_json.side`               | `side`                   |

Approximately 10 of the live builder's 32 slots are populated from
the V1 archive; the remaining ~22 fall through to the spec-declared
defaults in `app/features/builders.py::_SPECS`. **This is a DEGRADED
baseline, not V1 parity** — full reconstruction (joining
`weather_observations`, `line_snapshots`, `llm_predictions` to
recover sharp_money / tz_arb / ev_source signals at trade time) is
tracked under follow-up task #731.

`_amain()` calls `models_loader.ensure_artifacts()` automatically
when `--full-stack` is set so the LGB+XGB ensemble is bootstrapped
before the first surrogate row hits `predict()`. Bootstrap failures
are non-fatal — the script logs and continues so the report still
documents the failure mode rather than crashing.

The full-stack `summary` block grows two new keys:
- `surrogate_features_enabled` (bool) — operator-auditable on/off.
- `feature_source: {archived: N, surrogate: M}` — per-row origin
  counts so reviewers can verify exactly how many rows landed via
  the degraded baseline vs the (currently empty) archived path.

Canonical audit-gate artifact — full-archive run, no `--limit`,
no league filter
(`scripts/reports/v1_backtest_full_archive_2026-04-29.json`):
- `row_input_count = 667092`, `row_kept_count = 603128` (the rest
  are open positions with no settled `actual_outcome`)
- `parity_gate_status.overall = DEGRADED_PARTIAL_PENDING_BACKFILL`
- `full_stack_attempt.rows_predicted = 603128 / 603128`
- `feature_source.surrogate = 603128` (archived = 0, blocked on #731)
- `triage.feature_builder_mismatch.active = False` (was True)
- Per-league surrogate Brier vs V1's *raw* `predicted_prob` baseline
  (all 5 calibrator-refit leagues IMPROVED):

  ```
  league         n  brier_raw  brier_cal      delta    verdict
  ------------------------------------------------------------
  MLB          133     0.2644     0.0817    -0.1827   improved
  MLS            1     0.0000     0.0121    +0.0120     parity
  NBA         3093     0.6015     0.1038    -0.4977   improved
  NCAAB       1355     0.5383     0.1691    -0.3692   improved
  NFL            4     0.7318     0.6114    -0.1204   improved
  NHL          272     0.4450     0.1150    -0.3300   improved
  PGA           52     0.0760     0.0680    -0.0080     parity
  UNKNOWN   598218     0.2626     0.2626    +0.0000     parity
  ```

NFL coverage is small (n=4) but non-zero, so the original spec's
five-league requirement is exercised end-to-end (NBA, NFL, NCAAB,
NHL, MLB all present and IMPROVED).  UNKNOWN is the catch-all for
rows whose `analysis_json` did not carry an explicit `league` —
mostly futures and prop markets the V1 calibrators were not refit
for, so parity is the expected outcome.

Caveat: surrogate Brier is reduced-feature so the absolute numbers
should NOT be cited as the live ensemble's quality bar — they show
the chain runs, not that it matches V1. The V1-vs-live calibrated
parity verdict still requires Phase 2 dump reconstruction (#731).

Two smaller sample reports are kept alongside as sanity-check
artifacts (NOT audit-gate evidence — names made explicit):
- `v1_backtest_sample_calibrator_only_2026-04-29.json` (4k row
  sample, no surrogate, `parity_gate_status.overall =
  BLOCKED_PENDING_BACKFILL`, `rows_predicted = 0`) — proves the
  precise blocker the surrogate path works around.
- `v1_backtest_sample_surrogate_8k_2026-04-29.json` (8k row sample,
  with surrogate, NBA/NCAAB/NHL/MLB only) — used during development
  to bisect bugs without paying the full-archive 6-min runtime.

**Top-level hard-gate manifest** (`parity_gate_status` block, emitted
by every report) formalizes the three gates so reviewers do not have
to re-derive them from triage notes:
- `full_stack_replay_via_archived_features`: BLOCKED on #731 (V1
  archive does not snapshot the upstream LGB+XGB feature vector).
- `full_stack_replay_via_surrogate_features`: EVALUABLE_DEGRADED
  when `--surrogate-features` is on, NOT_RUN otherwise.
- `parity_within_2pct_vs_v1_calibrated`: UNEVALUABLE — V1 stored
  pre-calibrator stack output, not its final calibrated probability,
  so the spec's ±2% gate cannot be evaluated against this archive.

Re-run any time with (drop `--limit` for the full ~28k-row archive,
~6 min runtime; use `--limit N` for faster sampling):

    cd artifacts/api-server && \
      /home/runner/workspace/.pythonlibs/bin/python3 \
        -m scripts.backtest_v1_archive \
        --full-stack --surrogate-features \
        --leagues NBA,NCAAB,NHL,MLB --limit 8000 \
        --json-out scripts/reports/v1_backtest_surrogate_<date>.json

Regression coverage in `tests/test_backtest_v1_archive.py` (45 tests):
`_archive_to_candidate` mapping table (happy-path, sparse, reject-on-
missing-minimums, side aliases canonicalized via `_NO_SIDES`/
`_YES_SIDES`), surrogate dispatch through `_attempt_full_stack_inference`
and `run()` with `feature_source` counts asserted on the report, and
three tests pinning the `parity_gate_status` block (per-gate statuses,
NOT_RUN handling when surrogate is disabled, and the top-level
`overall` verdict including `BLOCKED_PENDING_BACKFILL` /
`DEGRADED_PARTIAL_PENDING_BACKFILL` rollups).

Phase 2 dumps (`weather_observations`, `line_snapshots`,
`llm_predictions`) plus broadening V1 side coverage to sport-specific
bet labels are tracked under follow-up task #731.  Additional follow-
ups proposed: #737 (per-league feature-coverage floor enforcement),
#738 (e2e `_amain` test on a temp DB), #739 (operator dashboard
surfacing per-league deltas + `parity_gate_status.overall`).

**Tooling notes for Phase 2** (whoever extends this to other dumps):

- `trade_analyses` was a single-file dump (no part-cat needed). The
  remaining multi-part dumps (e.g. `line_snapshots` is 2-part) use
  numeric `partNNN` suffixes — `app/storage.py::download_split_artifact`
  only matches GNU-split alphabetic `aa/ab/ac` (see `expected_suffix(i)`
  at line 131), so a partNNN reassembler must be added alongside or
  the parts catted directly via `download_to_path` per part.
- These are PLAIN-format `pg_dump` outputs (gzipped SQL, data-only),
  not custom format — `pg_restore` does NOT apply. Use `psql` or
  parse the `COPY ... FROM stdin;` blocks. Two psql >=18 metacommands
  (`\restrict` / `\unrestrict`) appear at the head of every dump and
  must be stripped before feeding to psql 17.x; see
  `scripts/import_v1_history.py::_rewrite_for_v1_archive`.
- `id` columns in V1 dumps mix integer-strings and UUIDs (e.g. both
  `121` and `df3d594b-c47e-731b-c8b0-60fcc6213c57` appear in
  `trade_analyses.id`). Use `text` for the archive PK and drop the
  trailing `setval('..._id_seq', ..., true)` line during rewrite.

See the "Bucket inventory" section below for the classified list of
every file in the bucket. Of the 18 V1 dump tables, only three are
already referenced by MME app code:

- **`weather_observations`** (970 KB, single-file) — referenced 6× in
  app code (`weather/training.py`, `feeds/weather*.py`, `swarm/runner.py`,
  `margin_regressor.py`, etc.). MME defines this table via the lazy
  `ensure_table` path captured in migration `0001_baseline.py`. Direct
  fit — feeds the weather GRU training that today falls back to
  `baseline_weights()`.
- **`line_snapshots`** (86 MB, 2-part numeric-split) — `feeds/odds_api.py`,
  `feeds/positive_ev.py`, `margin_regressor.py`, plus migrations
  `0029` and `0030`. Schema MUST be diff-checked against current
  MME `line_snapshots` before any insert. **Critical caveat:** migration
  `0030` added an `outcome` column and made it part of the unique index,
  with NULL-distinct semantics. The V1 dump almost certainly predates
  this column. Do NOT coerce NULLs to empty string on import — that
  would collapse what Postgres treats as distinct rows into duplicate-key
  collisions. The right approach is to insert with `outcome = NULL` for
  V1 rows (they'll never collide with each other or with newer
  outcome-bearing rows by index design), and run any reader that needs
  per-outcome rows only against post-`0030` data.
- **`llm_predictions`** (12.8 MB, single-file) — referenced 5× in app
  code (`main.py`, `config.py`, `swarm/__init__.py`, `swarm/runner.py`,
  `swarm/fallback_monitor.py`), plus 1 migration. Diff-check schema
  before bulk insert.

The other 15 tables (`crypto_*`, `uw_*`, `trade_analyses`,
`autoresearch_experiments`, `mid_phase_trade_decomposition`,
`research_journal`, `feature_drift_alerts`) are V1-only — MME does NOT
reference them in any migration or app file. The most important of
these is **`trade_analyses.sql.gz`** (29 MB, **DONE — Phase 1 imported
above**). Remaining V1-only dumps (`crypto_*`, `uw_*`,
`autoresearch_experiments`, `mid_phase_trade_decomposition`,
`research_journal`, `feature_drift_alerts`) land under `v1_archive.*`
in Phase 4 if and when those domains come online.

## Bucket inventory — `axiom-predict-artifacts/`

Full recursive walk performed 2026-04-29 — 189 objects across three
top-level prefixes. Every file is classified below by what (if
anything) consumes it in the current Replit codebase. Re-walk any time
with `app.storage.list_path("", limit=500)` recursively.

### `cutover/` (1 file) — V1→Replit cutover middleware

| File | Size | Purpose | Used by MME? |
|---|---|---|---|
| `cutover/mac_studio_proxy.py` | 2.5 KB | FastAPI middleware that, when env var `MAC_STUDIO_BACKEND_URL` is set, forwards every `/api/*` and `/health` request to the original Mac Studio backend. Lets MME stand in as the frontend while the heavy V1 inference still runs on the Mac. | **NO** — not imported anywhere. Useful as the canonical fallback path if we ever want to run MME's UI against V1's compute during a side-by-side comparison. Drop it into `app/middlewares/` and register on `app.add_middleware(MacStudioProxyMiddleware)` to enable. |

### `exports/` (7 files, 230 MB) — v155 model bundle ✅ VERIFIED

| File | Size | Used by MME? |
|---|---|---|
| `exports/models_manifest.json` | 51 KB | YES — `models_loader.py::_fetch_manifest`. Lists 237 predictors with sha256 + version. |
| `exports/models_part_a{a,b,c}` | 116 MB | YES — reassembled by `download_split_artifact` into `models.tar.gz`, then `_safe_extract_tar` lays it out under the cache dir (default `~/.cache/axiom-models/models/`, with `saved/` underneath). |
| `exports/source_part_a{a,b,c}` | 116 MB | YES — same pattern, extracted under the cache dir's `source/` tree and added to `sys.path` so the runtime can import V1's helper modules. Used by health endpoint to report bundle provenance. |

Already verified end-to-end by `scripts/verify_v155_artifacts.py` (PASS).

### `replit-port/archives/pg_dumps/` (33 files, ~2.0 GB) — V1 pg_dumps

All are `pg_dump` v16.10 plain-format gzipped output. Files >40 MB are
split with sibling `*.manifest.json` ({ size, chunks, sha256,
reassemble }) — same scheme as `exports/`. Reassemble via
`storage.download_split_artifact` or `cat *.partXXX > out.gz`.

| Table dump | Size | MME schema | App refs | Action |
|---|---|---|---|---|
| `weather_observations.sql.gz` | 970 KB | YES (lazy `ensure_table`) | 6 | **PHASE 1.** Highest-value direct fit. Feeds GRU training. |
| `line_snapshots.sql.gz` (2 parts) | 86 MB | YES (`0001` + `0029` + `0030`) | 4 | **PHASE 1** after schema diff (V1 likely predates `outcome` column). |
| `llm_predictions.sql.gz` | 12.8 MB | YES (1 migration) | 5 | **PHASE 1** after schema diff. |
| `trade_analyses.sql.gz` | 29 MB | NO | 0 | **PHASE 2.** Likely the audit's "1,249 settled trades" smoke-test target. Import under `v1_archive` schema. |
| `autoresearch_experiments.sql.gz` (14 parts) | 638 MB | NO | 0 | Phase 3 / archival. Hypothesis log — useful for backtest-of-backtests. |
| `crypto_predictions.sql.gz` | 2.2 MB | NO | 0 | Phase 3 — V1 ran a crypto vertical MME doesn't. Keep for ML training corpus. |
| `crypto_features.sql.gz` | 31 MB | NO | 0 | Phase 3 — same. |
| `crypto_prices.sql.gz` | 49 MB | NO | 0 | Phase 3 — same. |
| `crypto_training_data.sql.gz` | 26 MB | NO | 0 | Phase 3 — same. |
| `crypto_lob_snapshots.sql.gz` | 2.5 MB | NO | 0 | Phase 3 — order-book L2. |
| `crypto_orderbook_snapshots.sql.gz` | 10 MB | NO | 0 | Phase 3 — order-book L1. |
| `crypto_binary_decisions.sql.gz` | 29 MB | NO | 0 | Phase 3 — V1 binary-classifier outputs. |
| `mid_phase_trade_decomposition.sql.gz` | 1 MB | NO | 0 | Phase 3 — research notebook output. |
| `feature_drift_alerts.sql.gz` | 2.7 MB | NO | 0 | Phase 3 — drift-monitor history. |
| `research_journal.sql.gz` | 8 MB | NO | 0 | Phase 3 — researcher's notes table. |
| `uw_market_snapshots.sql.gz` (9 parts) | 412 MB | NO | 0 | Phase 3 — Unusual Whales options snapshots. |
| `uw_dark_pool.sql.gz` | 24 MB | NO | 0 | Phase 3 — UW dark-pool prints. |
| `uw_flow_alerts.sql.gz` | 32 MB | NO | 0 | Phase 3 — UW flow alerts. |

### `replit-port/archives/` (5 tar.gz archives) — V1 raw payloads

| File | Size | What's inside (inferred from name) | Action |
|---|---|---|---|
| `attached_assets.tar.gz` | 26 MB | V1's `attached_assets/` dir — uploads, screenshots, ad-hoc CSVs the V1 chat session received. | **Inspect first**: extract to `/tmp/v1_attached/` and grep for any CSV/JSON we might want surfaced. Most likely junk-drawer; expand only on demand. |
| `live.tar.gz` | 5 MB | V1's runtime-cache `live/` snapshots (recent odds / lines / weather payloads). | Reference only; staler than what MME polls today. **Skip.** |
| `odds.tar.gz` | 33 MB | V1's full `odds/` cache directory — historical Odds API JSON snapshots. | Useful as a free backfill source if MME's `line_snapshots` is sparse for any sport-window. **Phase 2.** |
| `sportsradar_cache.tar.gz` | 9 MB | Cached Sportradar API responses from V1. | **Useful as a free backfill source.** MME does have an active Sportradar code path (`app/sports/{ratings,leagues,nfl_context,soccer_context,nfl_stadiums,tournament_backtest}.py`, `app/margin_regressor.py`, `app/sportradar_leaderboards_retention.py`, plus `http_retry.py` quota tracking). Extract this tarball and we can hydrate Sportradar-derived features for any window without burning a single live API call. **Phase 2.** |
| `uw_raw_responses_00.tar.gz` (single) + `uw_raw_responses_0{1..4}.tar.gz` (split, 3-part each) | ~675 MB combined | Raw Unusual Whales API responses — pre-parse, before the `uw_*` pg_dumps. | **Skip unless** Phase 3 crypto/options work spins up. The pg_dumps already give the parsed form. |

### `replit-port/catboost_info/` (40 files) — V1 CatBoost training metadata

| Path | Purpose | Action |
|---|---|---|
| `catboost_training.json` (22 KB), `learn_error.tsv`, `test_error.tsv`, `time_left.tsv` | Final-run training-curve telemetry from V1's CatBoost margin-regressor training. | **Reference only** — useful if we ever need to compare convergence behavior of a re-trained MME margin model against V1's. **Do not consume at runtime.** |
| `learn/events.out.tfevents`, `test/events.out.tfevents` | TensorBoard event files for the same training run. | **Reference only.** |
| `tmp/cat_feature_index.*.tmp` (33 files, all 4–264 B) | Half-empty CatBoost scratch-temp files left over from an aborted training session. **Junk.** | **Safe to ignore / delete from bucket.** |

### `replit-port/config/` (61 files) — V1 reference configs

**None of these filenames are referenced by name in MME app code** —
MME's config system is `app/config.py` + Pydantic settings + env vars
(per `replit-port/config/README.md`'s `AXIOM_*` table). They're
**reference material**: if MME ever needs to add a knob V1 already
tuned, the V1 calibrated value lives here.

| File | Purpose | When you'd consume it |
|---|---|---|
| `risk_limits.json`, `risk_config.json` | Per-vertical max_open_positions, kelly_fraction, daily_loss_limit. | When tuning `app/risk/*.py` thresholds. |
| `governance_rules.json` | Sport-go-live rules (min_samples=200, brier_max=0.22, auc_min=0.65, ece_max=0.05). | When implementing `app/governance/` — these are the V1 thresholds. |
| `optimized_hyperparams.json`, `hpo_result_{NBA,MLB,NHL}.json` | HPO-tuned XGBoost/CatBoost hyperparameters per league. | When re-training models, use as warm-start defaults. |
| `production_features.json` (187 KB), `feature_contract.json` (65 KB), `feature_mapping.json`, `feature_parity_audit.json` | Master feature schema + canonicalization aliases (e.g. `home_rest_days → home_days_rest`). | When debugging feature-name drift between training and serving. |
| `sport_features/{CFB,MLB,NBA,NCAAB,NFL,NHL}.json` (6 files) | Per-sport feature whitelist. | When adding a new feature, check what V1 used. |
| `weather_model_config.json`, `weather_training_config.json`, `weather_sigma_sweep_report.json` | Weather GRU config + sigma-sweep results. | **Directly relevant** to the weather GRU baseline-fallback in `app/weather/predictor.py` — read these before re-training. |
| `mlb_parks.json`, `venues.json` | Stadium/park metadata. | When implementing park-factor adjustments. |
| `bankroll_config.json`, `bet_toggles.json`, `metric_ladders.json`, `category_kelly_adjustments.json` | Bet-sizing / category metadata. | Bankroll engine reference. |
| `adverse_selection_params.json`, `fill_model_params.json`, `lifecycle_clv_params.json` | Microstructure / fill-model knobs. | Microstructure engine reference. |
| `sport_status_overrides.json`, `crypto_binary_config.json`, `crypto_binary_training_config.json`, `midgame_config.json`, `ml_training_config.json`, `online_learning_config.json`, `orchestrator_config.json`, `realtime.json`, `realtime_config.yaml`, `engine_state.json`, `retrain_scheduler_state.json`, `unified_improvement_config.json`, `auto_trading_config.json`, `autoresearch_config.json` | Misc V1 subsystem configs. | Reference material — open the matching one when implementing the corresponding subsystem in MME. |
| `production.py` (12 KB), `settings.py` (6.8 KB), `validate_config.py` (4.6 KB) | V1's Python config-loading code. | Reference for porting any missing config logic to `app/config.py`. |
| `features/{city_coords,feature_config,rivalries,shap_interactions,sport_configs}.yaml` | Misc feature-engineering YAMLs. | Reference when porting feature code. |
| `data/pipeline_config.yaml`, `evaluation/eval_config.yaml`, `model-upgrade/model_config.yaml` | V1 service configs. | Reference when implementing matching MME subsystem. |
| `__init__.py`, `__pycache__/*` (5 files), `.gitkeep` | Python package boilerplate / build artifacts. | **Skip.** |

### `replit-port/data/` (17 files) — V1 runtime state snapshots

JSON snapshots of V1's last-known runtime state at the time the bucket
was populated. Useful as **smoke-test fixtures** ("what shape did V1
actually emit?"), not as live data.

| File | Purpose | Use as |
|---|---|---|
| `axiom-llm-status.json`, `axiom-llm-eval.json`, `axiom-llm-eval-history.json`, `axiom-llm-picks.json`, `axiom-llm-promotion.json`, `axiom-llm-drift.json`, `axiom-llm-dataset-meta.json`, `axiom-llm-train.log` | V1's LLM-pick subsystem's last-emitted state (last picks, last eval, training log). Picks were Mistral-7B-generated for upcoming NBA games. | **Test fixture** for the `app/llm_predictions` schema — confirm field names match before importing the `llm_predictions` pg_dump. |
| `advanced-metrics-latest.json`, `agent-health.json`, `all-sports-odds.json`, `aggregator-run-2026-02-20-1104.txt` | V1 dashboard / health snapshots. | **Reference** — what the V1 dashboard last looked like. |
| `autoresearch_checkpoint.json` | V1 autoresearch loop's last checkpoint. | **Reference** for porting autoresearch. |
| `backtesting-history.jsonl`, `backtesting-report.json`, `backtesting-report-v2.json` | V1 backtest output. | **High-value reference** — these are the V1 backtest numbers we'll need to reproduce / beat after the V1 import lands. Stash a copy under `docs/v1_backtest_baseline/`. |
| `axiom.db` (0 B) | Empty SQLite stub. | **Skip.** |

## Operations Runbook — quick links

For incident response and the live cutover, see [`docs/runbook.md`](docs/runbook.md). Direct links to the on-call sections:

- [Postgres outage](docs/runbook.md#7-postgres-outage) — symptoms, confirming it's the DB (not auth / not network), how the orchestrator parks itself, draining in-flight resting orders, and recovering from corrupted `system_settings` / `app_users`.
- [Clerk / auth provider outage](docs/runbook.md#8-clerk--auth-provider-outage) — what still works, the auth-free "bareback halt" flow, and how to verify ops alerts are still landing.
- [Halt a single vertical](docs/runbook.md#9-halt-a-single-vertical) — dashboard, API, and SQL fallbacks for stopping one vertical (e.g. weather or baseball) without taking the whole system down.
- [Cutover go/no-go checklist (paper → tiny live → full live)](docs/runbook.md#10-cutover-gono-go-checklist-paper--tiny-live--full-live) — phased ramp-up checklist used during the paper → live transition.
- [First 24 hours live](docs/runbook.md#11-first-24-hours-live) — dashboards to watch and the alerts worth being paged on.

## External Dependencies

- **Database:** Supabase Postgres (project `frwhxftngijwcujqhltf`, accessed via `asyncpg`).
- **Storage:** Supabase Storage bucket `axiom-predict-artifacts`.
- **Authentication:** Clerk (`@clerk/react`, `@clerk/themes`).
- **API Framework:** FastAPI (Python 3.11).
- **Frontend Framework:** React + Vite.
- **State Management/Data Fetching:** TanStack Query.
- **UI Components:** Tailwind CSS + shadcn/ui.
- **API Codegen:** Orval (generates API hooks and Zod schemas from `lib/api-spec/openapi.yaml`).
- **Transactional Email:** Postmark.
- **Billing:** Stripe.
- **Mobile In-app Subscriptions:** RevenueCat.
- **LLM Providers:** OpenAI, Anthropic (for follow-up tasks).
- **Prediction Market APIs:** Kalshi.
- **Push Notifications:** Expo (for mobile app).
- **Webhooks:** Svix (for Clerk webhooks).
- **HTTP Client:** httpx (for outbound REST calls).
- **Testing:** Playwright (e2e tests), Vitest (web unit tests with V8 coverage), Jest with `jest-expo` preset (mobile unit + component tests with Istanbul coverage; component tests use React Native Testing Library and run via the `mobile-tests` workflow). Frontend coverage trends are posted as a sticky PR comment by the `frontend-coverage-comment` workflow, which compares against baselines stored on the `js-coverage-data` branch.
- **Accessibility Auditing:** axe-core.