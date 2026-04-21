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

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- API server: managed by the `artifacts/api-server: API Server` workflow (uvicorn on `$PORT`)
- Web: managed by the `artifacts/web: web` workflow

## Follow-up scope (NOT in this slice)

Live Kalshi trading loop, 6-persona LLM swarm debate engine, 12-gate risk system,
Stripe billing, Clerk auth, Expo mobile app, RevenueCat, source-archive bootstrap from
Storage. See `.local/tasks/axiom-predict-mvp.md` for the full multi-week plan.

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
