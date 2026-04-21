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
- `AXIOM_API_TOKEN` (optional in dev, **required in prod**) — shared bearer token. When set,
  every `/api/dashboard/*`, `/api/models/*`, `/api/storage/*`, and `/api/health` request must
  present it via `X-API-Token: <token>` or `Authorization: Bearer <token>`. `/api/healthz`
  stays public. There is no referer/host bypass — those are forgeable.
- `AXIOM_CORS_ORIGINS` (optional) — comma-separated allowed origins (defaults to `*`)
- `VITE_AXIOM_API_TOKEN` (frontend build-time) — when set, the operator console injects it
  as `X-API-Token` on every request via `setAuthTokenGetter`. Must match `AXIOM_API_TOKEN`.

## Key endpoints (`/api`)

- `GET /healthz` — liveness, public
- `GET /health` — full readiness (DB + Storage + secrets)
- `GET /dashboard/{overview,recent-trades,recent-cycles,recent-debates,swarm-personas}`
- `GET /models/inventory` — multi-part archive listing under storage `exports/`

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
