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
what drives the front-end gate.

## Key endpoints (`/api`)

- `GET /healthz` — liveness, public
- `GET /me/access` — approval status (auth required, but unapproved users get 200)
- `GET /health` — full readiness (DB + Storage + secrets) — approved-only
- `GET /dashboard/{overview,recent-trades,recent-cycles,recent-debates,swarm-personas}` — approved-only
- `GET /models/inventory` — multi-part archive listing under storage `exports/` — approved-only

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
