# Workspace — Axiom Predict (Replit port)

## Overview

pnpm workspace monorepo hosting the Axiom-Predict prediction-market trading platform.
This is the foundation slice: a Python FastAPI backend reading the existing Supabase
production database (149 tables) + Storage bucket, plus a React operator console
showing real production telemetry.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: FastAPI (Python 3.11) — `artifacts/api-server`
- **Database**: Supabase Postgres (project `frwhxftngijwcujqhltf`, accessed via asyncpg)
- **Storage**: Supabase Storage bucket `axiom-predict-artifacts`
- **Frontend**: React + Vite + TanStack Query + Tailwind shadcn/ui — `artifacts/web`
- **API codegen**: Orval (`lib/api-spec/openapi.yaml` → `lib/api-client-react`)

## Required env / secrets

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

## Operator allow-list

Signing in with Clerk is **necessary but not sufficient** to see the operator
console. Telemetry, P&L, fills, and swarm debates are gated by an explicit
allow-list enforced both server-side (returns `403`) and client-side (the web
app shows the *Awaiting approval* screen instead of the dashboard).

A signed-in user is approved if **either**:

1. Their primary Clerk email (case-insensitive) appears in the comma-separated
   `OPERATOR_ALLOWED_EMAILS` env var on the API server. Example:
   `OPERATOR_ALLOWED_EMAILS="alice@example.com,bob@example.com"`. Restart the
   API workflow after changing this var.
2. Their Clerk user has `public_metadata.approved = true` (or the same flag in
   `private_metadata`). Set this from the Clerk dashboard under Users →
   *user* → Metadata. No deploy/restart needed; the backend caches Clerk user
   lookups for 60 seconds.

The backend resolves the user's email/metadata from JWT claims first and falls
back to the Clerk Backend API (`api.clerk.com/v1/users/{id}`) using
`CLERK_SECRET_KEY`. With no allow-list configured and no metadata flag set,
**every signed-in account is treated as unapproved**.

The `GET /api/me/access` endpoint is the only authenticated endpoint
unapproved users can reach; it returns `{approved, email, user_id}` and is
what drives the front-end gate. Every call also upserts the caller into
`app_users` so admins can see them in the access-requests panel.

### Approving from inside the console

Admins (anyone with `is_admin = true` on their `app_users` row, or a
bootstrap `OPERATOR_ADMIN_EMAILS` email on a blank database) see an
**ACCESS_REQUESTS** panel on the operator dashboard listing every
signed-in user who is still unapproved and has not been denied. One
click approves (writes `approved = true` and mirrors
`public_metadata.approved = true` to Clerk) or denies (stamps
`denied_at` so they drop out of the panel — re-approving clears it).
Both actions are written to `admin_audit_log` with action
`set_approved` / `set_denied`. Backed by:

- `GET  /api/admin/access-requests`
- `POST /api/admin/access-requests/{clerk_user_id}` body `{decision: "approve"|"deny"}`

This is the recommended path; editing `OPERATOR_ALLOWED_EMAILS` or
toggling `public_metadata.approved` in the Clerk dashboard still works
as a fallback.

## Key endpoints (`/api`)

- `GET /healthz` — liveness, public
- `GET /me/access` — approval status (auth required, but unapproved users get 200)
- `GET /me/subscription` — caller's effective tier/status (auth required)
- `GET /billing/pricing` — public tier catalogue (used by the `/pricing` page)
- `POST /billing/checkout-session` — start a Stripe Checkout (auth required)
- `POST /billing/portal-session` — open the Stripe Customer Portal (auth required)
- `POST /billing/webhook` — Stripe webhook receiver (signature-verified)
- `GET /health` — full readiness (DB + Storage + secrets) — approved-only
- `GET /dashboard/{overview,recent-trades,recent-cycles,swarm-personas}` — approved-only
- `GET /dashboard/recent-debates` — full LLM transcripts — Pro+ only
- `POST /swarm/debate`, `POST /orchestrator/run-cycle` — Pro+ only
- `GET /models/inventory` — multi-part archive listing under storage `exports/` — approved-only
- `GET /admin/access-requests` — pending sign-ups awaiting operator approval — admin-only
- `POST /admin/access-requests/{clerk_user_id}` — approve or deny in one click — admin-only
- `GET /calibration/snapshots?vertical=` — calibration history rows for the
  `/calibration` operator page — approved-only
- `GET /research/directives` — open auto-research directives feeding the
  `/research` operator page — approved-only
- `POST /research/directives/{id}/decision` — approve / reject directive — admin-only
- `POST /webhooks/clerk` — Clerk → app_users sync, svix-signature verified
  (manual HMAC-SHA256, 5-minute replay window). Subscribes to
  `user.created`/`user.updated`/`user.deleted` and busts the in-process user
  cache in `app/auth.py`. Configure with `CLERK_WEBHOOK_SECRET` (whsec_…).
- WebSockets — per-channel split (Task #245): `/ws/positions`, `/ws/trades`,
  `/ws/alerts`, `/ws/orderbook/{ticker}`, `/ws/temperature/{city}`. Each
  filters the shared pipeline/settlement/Kalshi event hubs by event type so
  the web shell can subscribe surgically instead of consuming the firehose.

## Drift auto-rollback (Task #245)

`app/settlement/retrain.py` enforces a hard guard around every retrain:
if the post-train Brier exceeds the pre-train Brier by more than
`ROLLBACK_TOLERANCE` (1e-4), the run inserts a `model_rollback_log` row,
raises a `model_health_alerts` entry of type `retrain_rollback`, tags the
training snapshot's `drift_status.status` as `rolled_back`, and leaves the
buffer rows un-acked so the next retrain batch retries instead of
silently shipping a worse model. The rollback log + alert give operators
the audit trail required by `docs/runbook.md` §6.

## Operator runbook

`docs/runbook.md` is the on-call reference for Axiom-Predict — paper↔live
toggling, Kalshi key rotation, emergency-stop, pipeline-log reading,
manual model promotion, and drift-rollback recovery. Task #272 added
four cutover-focused sections: Postgres outage (§7), Clerk / auth
provider outage (§8), per-vertical halt (§9 — uses the Task #271
toggle), and the paper → tiny-live → full-live go/no-go checklist
(§10) plus a "first 24 hours live" pager rider (§11).

## Subscription tiers

Four tiers are resolved per request via `app/billing.py`:

- `free` — default for any signed-in user with no active Stripe subscription.
- `pro` — paid (`active`/`trialing`/`past_due`); unlocks live signals, full
  swarm transcripts, and manual cycle execution.
- `elite` — superset of pro; reserved for future entitlements.
- `founder` — operator allow-list emails (`OPERATOR_ALLOWED_EMAILS` ∪
  `STRIPE_FOUNDER_USERS`); billing bypassed entirely.

Tier resolution upserts/reads from the `subscriptions` table (created
on first use). Stripe webhook events are idempotent through `stripe_events`.

After deploy you must, in the Stripe dashboard: (1) create your products +
recurring prices, (2) copy the `price_…` IDs into the four
`STRIPE_PRICE_*` env vars, and (3) add a webhook endpoint targeting
`https://<host>/api/billing/webhook` with the events
`checkout.session.completed`, `customer.subscription.{created,updated,deleted}`,
`invoice.payment_{succeeded,failed}`.

## Frontend information architecture (post Tasks #228–#237)

The web operator console (`artifacts/web`) and Expo mobile app
(`artifacts/mobile`) share the cyberpunk-finance design system from
`FRONTEND_UI_REPORT_*.md`. The web shell mounts a single
`AppShell` (sidebar + header + degraded/trading banners + global
toaster + WebSocket provider) around every authenticated page.

Authenticated routes are **lazy-loaded** via `React.lazy()` and
wrapped in a `<Suspense fallback={<PageFallback />}>` boundary so each
top-level page (`/app`, `/trading`, `/positions`, `/pipeline`,
`/intelligence`, `/system`, `/profile`, `/admin`, `/trade/:ref`) ships
its own JS chunk. Marketing, sign-in, pending-approval, and
not-found stay eager.

Top-level signed-in IA:

- `/app` — operator dashboard (KPIs, swarm, recent trades, pipeline)
- `/trading` — live trade ticket + feed
- `/positions` — open + recently-closed positions
- `/pipeline` — per-trade ticker (`<TradeTrainTicker />`) renders one
  horizontal "train" of 15 stage chips per in-flight candidate
  (predict → swarm → gatekeeper → 12 risk gates → place); chips pulse
  amber while pending, flip green ✓ on pass, red ✗ on fail, and click
  to open a side panel with the gate's full decision payload + a link
  to `/trade/:ref`. Aggregate six-stage flowchart + console tail still
  render below it. All consumes the shared `usePipelineSocket()` from
  `WebSocketProvider` — no extra socket connections.
- `/intelligence` — verticals, models, swarm transcripts (Pro+)
- `/system` — tabs `health` / `pipeline` / `controls` / `audit` /
  `runtime-settings`; admin-only tabs render forbidden state for
  non-admins instead of hiding them
- `/profile` — account, plan, notifications, billing portal entry
- `/admin` — user / access-request / vertical-toggle console
  (admin-only; non-admins get a 403 panel)
- `/trade/:ref` — single-trade deep-dive

Realtime fan-out (Task #236) flows through one shared WebSocket per
surface:

- Web: `WebSocketProvider` mounts inside `AppShell`; pages call
  `usePipelineSocket()` / `usePipelineSubscription()` /
  `usePipelineEventBuffer()` instead of opening their own sockets.
  `RealtimeNotifications` translates trade/alert/health events into
  click-through toasts (capped at 5, 6 s, deduped via a 200-entry LRU
  keyed only on server-issued ids — `trade_id`, `order_id`,
  `_seq`).
- Mobile: `lib/use-websocket.ts` uses a generation-guarded mount with
  atomic detach/close so React-strict-mode double-mounts cannot leak
  sockets; `buildWsUrl` defaults to `wss://` for bare hosts.

Accessibility primitives shipped in #237:

- AppShell renders an sr-only **Skip to main content** link as its
  first focusable element (`data-testid="link-skip-to-content"`),
  jumping to `<main id="main-content">`.
- All page chunks render a `PageFallback` skeleton during lazy load
  so first-paint never blanks the shell.

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/web run test:e2e` — Playwright operator + admin
  e2e suite (`artifacts/web/tests/`). The public-surface tests run
  unconditionally; auth-gated tests skip themselves unless
  `E2E_CLERK_USER_EMAIL` / `_PASSWORD` (and optionally
  `E2E_CLERK_ADMIN_EMAIL` / `_PASSWORD`) are set. Override the
  target with `E2E_BASE_URL=https://…` to run against a deploy.

  CI runs this suite automatically on every push + PR to `main` via
  `.github/workflows/web-e2e.yml` (Task #243). The job builds the web
  bundle, optionally boots the FastAPI server in the background (when
  `SUPABASE_DB_PASSWORD` is set as a CI secret), starts `vite preview`,
  installs Chromium with system libs, and runs the suite. On failure it
  uploads `playwright-report/`, `test-results/` (traces + screenshots +
  video), and the API/web background-process logs as job artifacts.

  CI secrets to configure in GitHub for the full 16-test matrix:
  `E2E_CLERK_PUBLISHABLE_KEY`, `E2E_CLERK_SECRET_KEY` (Clerk dev
  instance), `E2E_CLERK_USER_EMAIL`, `E2E_CLERK_USER_PASSWORD`,
  `E2E_CLERK_ADMIN_EMAIL`, `E2E_CLERK_ADMIN_PASSWORD`, plus the backend
  trio `SUPABASE_DB_PASSWORD`, `SUPABASE_SERVICE_ROLE_KEY`,
  `OPERATOR_ALLOWED_EMAILS`, `OPERATOR_ADMIN_EMAILS`. Without them only
  the public-surface tests execute; auth-gated tests skip themselves
  rather than failing.

- `pnpm --filter @workspace/mobile run test:e2e` — Playwright spec
  (`artifacts/mobile/tests/operator-journey.spec.ts`) that drives the
  Expo react-native-web export through sign-in → dashboard →
  sign-out. Asserts the same `card-recent-trades`,
  `card-trading-cycles`, and `card-health-snapshot` KPIs the web
  operator-journey spec checks (react-native-web maps `testID` →
  `data-testid`). Start the Expo server first
  (`pnpm --filter @workspace/mobile run dev`, or `build` + `serve`)
  and point the suite at it with
  `E2E_MOBILE_BASE_URL=http://localhost:<port>`. Auth-gated tests
  skip without `CLERK_PUBLISHABLE_KEY` / `CLERK_SECRET_KEY` /
  `E2E_CLERK_USER_EMAIL`.
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- API server: managed by the `artifacts/api-server: API Server` workflow (uvicorn on `$PORT`)
- Web: managed by the `artifacts/web: web` workflow

## Outbound HTTP reliability

All outbound REST calls from the API server route through
`app/http_retry.py`, a shared retry primitive that:

- retries on `httpx.TransportError`, 408/425/429/5xx with exponential
  backoff + full jitter (default `max_attempts=4`)
- honours upstream `Retry-After` headers when present
- records per-client attempt / retry / failure counters surfaced under
  `summary.retry_counters` in `/api/health/snapshot`

Wired into the GET / read paths of: **kalshi**, **polymarket**,
**sportradar:<sport>:<feed>**, **postmark**, **clerk** (backend
`/v1/users/{id}` lookup), **expo_push**.  Mutating verbs (Kalshi /
Polymarket order POSTs) are intentionally NOT retried so the system
cannot duplicate fills.  Polymarket re-signs every retry attempt
inside `_do()` because the L2 HMAC bakes in a per-attempt timestamp
that goes stale across the retry-after window — same pattern as
Kalshi's per-attempt RSA-PSS signing.

The LLM gatekeeper persona is **fail-closed** with strict schema
validation: a verdict is only honoured when the response is a dict
containing a real `bool` for `approved` AND a `recommended_action` in
the closed enum (`BUY_YES` | `BUY_NO` | `PASS`).  Anything else (call
exception, malformed JSON, wrong-typed `approved`, missing /
unknown action) completes the debate with `approved=false`,
`recommended_action="PASS"`, and bumps
`summary.gatekeeper_failures.{call_error|malformed_json}` on the
health snapshot.  Tests in `tests/test_swarm_gatekeeper_failclosed.py`
pin every branch of this contract.

## Accessibility + bundle-size audit (Task #239 — 2026-04-23)

Run with axe-core 4.11.3 (via `@axe-core/playwright`) against the local
dev build (1280×720, headless Chromium). Reports per route are written
to `artifacts/web/a11y-reports/<route>.json`; the spec lives at
`artifacts/web/tests/a11y.spec.ts`. Re-run with:

```
PORT=… BASE_PATH=/ pnpm --filter @workspace/web dev   # serve
E2E_BASE_URL=http://localhost:<port> \
  pnpm --filter @workspace/web exec playwright test tests/a11y.spec.ts
```

### Axe findings — public routes (scanned)

| Route          | total | serious/critical | notes                                             |
|----------------|-------|------------------|---------------------------------------------------|
| `/` marketing  | 3     | 0                | landmarks + viewport (see below)                  |
| `/pricing`     | 4     | 1 serious        | `color-contrast` on `.opacity-60` "(save ~17%)"   |
| `/about`       | 3     | 0                | landmarks + viewport                              |
| `/leaderboard` | 3     | 0                | landmarks + viewport                              |
| `/health`      | 4     | 1 serious        | `color-contrast` on the embedded NotFound h1/p    |
| `/sign-in`     | 4     | 1 serious        | same NotFound color-contrast leak (see follow-up) |

Recurring moderate findings on every public route:

- `landmark-one-main` / `region` — public pages render directly under
  `<body>` with no `<main>` landmark (only the authed `AppShell` wraps
  content in `<main id="main-content">`). Wrapping the public pages
  (marketing, pricing, about, leaderboard, health, sign-in) in a
  `<main>` clears both rules in one move.
- `meta-viewport` — `index.html` sets
  `content="width=device-width, initial-scale=1.0, maximum-scale=1"`,
  which axe flags as preventing user zoom (WCAG 1.4.4). Drop
  `maximum-scale=1`.

Serious findings to file/fix:

- `color-contrast` on `/pricing` — the "(save ~17%)" annotation in
  `src/pages/pricing.tsx` uses `opacity-60` against `#0a0e16`, giving a
  3.8 : 1 ratio (needs 4.5 : 1). Either drop the opacity or pre-compute
  a darker `text-muted-foreground` shade for that span.
- `color-contrast` on `/health` and `/sign-in` — both routes are
  rendering `src/pages/not-found.tsx` (which uses Tailwind's stock
  light-theme `text-gray-900` / `text-gray-600` on the dark background,
  ratios 1.03 : 1 and 2.43 : 1). Two bugs to file: (a) `not-found.tsx`
  must use the dark-theme tokens (`text-foreground` /
  `text-muted-foreground`), and (b) investigate why `/health` and
  `/sign-in` fall through to NotFound at all in the audited build —
  likely a Wouter base-path / Clerk-publishable-key edge case in the
  dev environment that should not occur in production.

### Axe findings — authed routes (scope limit)

`/app`, `/trading`, `/positions`, `/system`, `/admin` were **not**
scanned in this run because the audit harness has no Clerk test-user
credentials in the workspace env (`E2E_CLERK_USER_EMAIL` /
`_PASSWORD` are unset). Filed as a follow-up — once a Clerk testing
user is provisioned, the same `tests/a11y.spec.ts` can be extended to
sign in and rerun axe against each authed route. The shell-level
primitives that all authed routes share (skip-to-content link,
`<main id="main-content">`, sidebar landmarks) are exercised by the
existing operator-journey e2e suite.

### Bundle-size audit (`pnpm --filter @workspace/web run build`)

Route-level code-splitting is healthy — every lazy route chunk is well
under the ~250 KB gz budget:

| Chunk                          | raw      | gzip      | budget |
|--------------------------------|----------|-----------|--------|
| `dashboard-*.js`               | 31.67 KB | 8.59 KB   | ✅     |
| `pipeline-*.js`                | 30.69 KB | 8.73 KB   | ✅     |
| `admin-*.js`                   | 34.06 KB | 7.60 KB   | ✅     |
| `intelligence-*.js`            | 23.58 KB | 5.95 KB   | ✅     |
| `system-*.js`                  | 7.66 KB  | 2.45 KB   | ✅     |
| `trading-*.js`                 | 4.87 KB  | 2.07 KB   | ✅     |
| `positions-*.js`               | 8.56 KB  | 3.19 KB   | ✅     |
| `pricing-*.js`                 | 6.67 KB  | 2.40 KB   | ✅     |
| `profile-*.js`                 | 11.66 KB | 3.30 KB   | ✅     |
| `health-*.js`                  | 12.95 KB | 3.40 KB   | ✅     |
| (other lazy chunks)            | < 12 KB  | < 5 KB ea | ✅     |
| **shared entry `index-*.js`**  | 978.25 KB| **283.99 KB** | ⚠️ over |
| `index-*.css`                  | 133.33 KB| 20.94 KB  | ✅     |

The lazy split is doing its job — no individual route chunk exceeds
~9 KB gz. The over-budget item is the shared entry bundle (React +
React-DOM + Clerk + TanStack Query + Wouter + shared UI primitives)
at 284 KB gz. That's outside the per-route budget the spec called
out, but it's the dominant contributor to first-paint payload and
worth a follow-up: split Clerk (`@clerk/react` + `@clerk/themes`) out
of the entry chunk via `manualChunks`, or defer it behind the
sign-in/up routes that actually need it.

## Follow-up scope (NOT in this slice)

Live Kalshi trading loop, 6-persona LLM swarm debate engine, 12-gate risk system,
Stripe billing, Clerk auth, Expo mobile app, RevenueCat, source-archive bootstrap from
Storage. See `.local/tasks/axiom-predict-mvp.md` for the full multi-week plan.

## RevenueCat (mobile in-app subscriptions)

The webhook entitlement-plumbing path is locked down by
`artifacts/api-server/tests/test_rc_billing.py` — INITIAL_PURCHASE,
RENEWAL, PRODUCT_CHANGE upgrade, CANCELLATION (with grace), EXPIRATION,
BILLING_ISSUE, TRANSFER (sign-out/sign-in restore surrogate), idempotent
replay, auth-header gating, and an end-to-end check through
`/api/me/subscription`. Genuine on-device sandbox purchase / restore
flows can only be exercised on TestFlight + Google Play Internal
Testing — see `artifacts/mobile/docs/REVENUECAT_SANDBOX_QA.md` for the
manual checklist that must run on every release touching billing or the
mobile entitlement code.

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
