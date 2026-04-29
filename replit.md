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

### V1 historical-data import — BLOCKED on V1 source access

The second half of the same task — porting V1's ~60,503 settled games
and ~1,249 settled trades into Supabase under new `sports_games` /
`trade_history` tables (so calibrators can cross `MIN_REFIT_SAMPLES=10`
per league and replace the `market_prior` rows seeded by
`scripts/seed_market_prior_calibrators.py`) — is currently blocked.
The audit referenced `https://github.com/MahlumInnovationsLLC/Axiom-Predict-V1.git`
as a public source, but the GitHub API returns HTTP 404 for that exact
repo slug, every plausible alternate spelling, and every plausible
alternate org. There is no `GITHUB_TOKEN` secret configured, no GitHub
connection set up, and no V1 SQL/CSV dump in `attached_assets/`.

To unblock, one of the following is needed:
1. Make the V1 repo public under its real slug, OR
2. Provide a Personal Access Token (`GITHUB_TOKEN`) with `repo:read`
   scope, OR
3. Drop a V1 SQL dump (`.sql` / `.sql.gz` / `pg_dump -Fc`) or
   per-table CSV exports into `attached_assets/`.

Until V1 is reachable, the migration `0037_v1_history_tables.py` and
the ETL script `scripts/import_v1_history.py` are intentionally
deferred — writing speculative table DDL against a schema we cannot
verify would just have to be rewritten once V1's real column shapes
land. The audit-reported V1 row counts (60,503 games / 1,249 trades)
remain the smoke-test target.

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