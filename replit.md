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