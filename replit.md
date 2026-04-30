# Workspace — Axiom Predict (Replit port)

## Overview

Axiom Predict is a prediction-market trading platform designed for advanced trading, detailed analytics, and robust user management. It features a Python FastAPI backend, a React operator console for real-time telemetry, and an Expo mobile application. The platform integrates with an existing Supabase production database and storage, aiming to provide comprehensive tools for prediction market participants.

## User Preferences

- The agent should prioritize high-level architectural decisions and design patterns over granular implementation details.
- The agent should consolidate redundant information and remove all changelogs, update logs, and date-wise entries.
- The agent should focus on essential information for guiding a replit coding agent.

## System Architecture

**Monorepo Structure:** The project is organized as a pnpm workspace monorepo, including:
- `artifacts/api-server`: Python FastAPI backend.
- `artifacts/web`: React frontend.
- `artifacts/mobile`: Expo mobile application.

**Backend (FastAPI):**
- **Authentication & Authorization:** Integrates with Clerk for user authentication and uses an operator allow-list (via `OPERATOR_ALLOWED_EMAILS` or Clerk user metadata) for core functionality access. All authenticated endpoints require a Clerk session JWT.
- **Subscription Tiers:** Manages four tiers (`free`, `pro`, `elite`, `founder`) through Stripe integration, controlling feature access.
- **Reliability:** Employs a shared retry mechanism with exponential backoff for outbound HTTP calls.
- **Drift Auto-rollback:** Automatically rolls back and retries model retraining if Brier scores degrade beyond a defined tolerance.
- **LLM Gatekeeper:** Designed to be fail-closed, honoring only strictly validated LLM verdicts and logging errors for malformed responses.
- **Webhooks:** Handles Clerk and Stripe webhooks with signature verification and idempotent processing.
- **Risk Gates:** Includes a comprehensive set of risk management gates (e.g., `sharpe_ratio`, `ruin_monte_carlo`, `tail_price_size_cap`, `jumbo_quantity_cap`) to manage trading exposure and prevent catastrophic losses.
- **LLM Mode Switching:** Supports `cloud`, `local`, and `hybrid` LLM inference modes via environment variables (`AXIOM_LLM_MODE`), allowing operators to control cost and latency.

**Frontend (React):**
- **Design System:** Shares a "cyberpunk-finance" design system with the mobile app.
- **UI Structure:** Uses a single `AppShell` for global UI elements and lazy-loads authenticated routes for performance.
- **Realtime Data:** Utilizes a shared WebSocket provider for real-time updates across various sections.
- **Accessibility:** Implements accessibility primitives and undergoes regular audits.
- **Trade Provenance:** Incorporates a `placed_by` filter for analytics, allowing users to view trades by `manual_ui`, `orchestrator`, or `all`. This filter is applied to dashboard metrics and equity curves.
- **Vertical Filtering:** Enables filtering of equity-derived tiles and charts by specific verticals (e.g., sports, crypto) to provide accurate P&L views.

**Mobile (Expo):**
- Shares the "cyberpunk-finance" design system.
- Integrates with RevenueCat for in-app subscriptions and robust webhook entitlement plumbing.
- Employs generation-guarded WebSocket mounts to prevent leaks.

**Live Games + Today's Games Chatbot:**
- Provides real-time and upcoming game market information.
- Utilizes multiple LLM models (Claude Sonnet, GPT-5, Gemini 2.5 Pro) for analysis.
- Integrates with ESPN and Kalshi for schedule data, acting as a backstop against external data source failures.

**Model Artifacts (v155):**
- Model bundle published to `axiom-predict-artifacts/exports/` includes `models_manifest.json` and multi-part archives for models and source code.
- Verification script (`verify_v155_artifacts.py`) confirms integrity, predictor count, and performs smoke tests.
- V1 historical data (trade_analyses) imported into a `v1_archive` schema for calibrator refitting.

## External Dependencies

- **Database:** Supabase Postgres (via `asyncpg`).
- **Storage:** Supabase Storage bucket `axiom-predict-artifacts`.
- **Authentication:** Clerk (`@clerk/react`, `@clerk/themes`).
- **API Framework:** FastAPI (Python 3.11).
- **Frontend Framework:** React + Vite.
- **State Management/Data Fetching:** TanStack Query.
- **UI Components:** Tailwind CSS + shadcn/ui.
- **API Codegen:** Orval.
- **Transactional Email:** Postmark.
- **Billing:** Stripe.
- **Mobile In-app Subscriptions:** RevenueCat.
- **LLM Providers:** OpenAI, Anthropic, Ollama (local inference).
- **Prediction Market APIs:** Kalshi.
- **Push Notifications:** Expo.
- **Webhooks:** Svix.
- **HTTP Client:** httpx.
- **Testing:** Playwright (e2e), Vitest (web unit), Jest with `jest-expo` (mobile unit/component).
- **Accessibility Auditing:** axe-core.