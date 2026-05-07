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
- **Tail-risk exposure tile (May 2026):** Read-only dashboard tile (web + mobile, slotted directly under `KpiSummaryBar`) backed by `GET /api/public/tail-risk-exposure`. See the dedicated subsection at the end of this file for bucket taxonomy and audit context.

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

**Supabase Growth Retention (task #757):**
- Per-table retention loops in `app/<table>_retention.py` keep high-growth Supabase tables bounded. Two tiers: archive-then-delete for ML/training tables (90-day default — predictions, market_price_ticks, autoresearch_features, tech_evidence, line_snapshots, sportradar_tournament_replays, precip_emos_observations) and delete-only for operational/audit logs (30-day default — pipeline_events, settlement_events, mobile_notification_log, bankroll_events, rc_events, stripe_events, rc_webhook_deliveries, billing_churn_events, admin_audit_log, position_drift_resolutions, order_recovery_attempts, stale_cancel_observations, vertical_halt_history, gatekeeper_setting_history, model_rollback_log).
- Loops factored on `app/retention_base.py` (`DeleteOnlyRetention`, `ArchiveThenDeleteRetention`) and `app/archive_helper.py` (gzip JSONL chunks under `archives/<table>/dt=YYYY-MM-DD/part-<n>.jsonl.gz` with sibling `manifest.json` sidecar; upload-then-delete per chunk for crash safety).
- All loops default DISABLED — operator opts in via `AXIOM_<PREFIX>_RETENTION_ENABLED=1` after reviewing the dry-run from `scripts/archive_and_prune_backlog.py`. Registry + lifecycle hook live in `app/retention_loops_v757.py`; dashboard at `/api/dashboard/supabase-growth-retention`. See `docs/retention_runbook.md`.

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
## Tail-risk exposure tile (May 2026)

**Why it exists.** Forensic audit of YTD 2026 SYSTEM trades found that
0.28% of trades (15 of 5,347) drove −$9,604 of cumulative losses on a
−$5,878 net book — i.e. without those 15 trades the bot is +$3,726 YTD.
Two patterns cause virtually all of it:

- **Deep-OTM tail bets** (entry price < $0.10) lost −$5,607 in the
  sports vertical alone, and one Feb-20 NBA bet (qty=4,394 NO at
  $0.04 → −$4,218) was bigger than the entire YTD loss.
- **Jumbo bets** (qty ≥ 1,000) were 15 trades for −$5,985 net.

The good buckets — $0.60–$0.90 favorites at +$11–$17 EV/trade for
+$6,326 cumulative — were masked by these blow-ups. The tile makes
the live concentration of those bets visible right under the KPI
strip so the operator sees today's tail-risk exposure before the next
blow-up.

**Bucket taxonomy (server-owned).** Boundaries are SQL `CASE`
expressions in `dashboard.py` — DO NOT re-derive them client-side, the
server owns the taxonomy so every surface (web tile, mobile tile,
future alerts) stays aligned:

| Bucket          | `lo` (≥) | `hi` (<) | Default `is_tail` |
| --------------- | -------- | -------- | ----------------- |
| `<$0.10`        | 0.00     | 0.10     | true              |
| `$0.10–$0.25`   | 0.10     | 0.25     | false (straddles) |
| `$0.25–$0.40`   | 0.25     | 0.40     | false             |
| `$0.40–$0.60`   | 0.40     | 0.60     | false             |
| `$0.60–$0.75`   | 0.60     | 0.75     | false             |
| `$0.75–$0.90`   | 0.75     | 0.90     | false             |
| `$0.90+`        | 0.90     | 1.00     | false             |

The tail definition (`_TAIL_PRICE_THRESHOLD = 0.20`) is its own
constant and the `is_tail` flag is true only when the whole bucket
sits below it.  The `$0.10–$0.25` bucket straddles the threshold, so
the tile uses a **dedicated row-level aggregate**
(`sql_tail_positions` / `sql_tail_trades`) for the headline number
instead of summing the straddling bucket — that would over-count any
trade priced $0.20–$0.25.

**Data path.** The aggregator runs `sql_with_positions` first; if
that throws OR returns rows but every `open_count == 0`, it
reconstructs open exposure from the `trades` table via
`sql_trades_only` (VWAP-netted by `(vertical, market_key, side_lc)`
with `HAVING sum(qty) > 0`).  The realized slice in BOTH paths
filters `pnl IS NOT NULL` so open trades never inflate realized
counts.  The response carries `source ∈ { "positions",
"trades_fallback", "empty" }` so the client can label provenance and
the realized slice always surfaces — even when both paths find zero
open exposure but the positions path returned realized rows, those
rows are kept (the dispatch never blanks the tile).

**Frontend wiring.** Web (`tail-risk-exposure-panel.tsx`) reads
`days` from `usePeriod()` and threads `placedBy`,
`effectiveVerticals`, and `days` into the React-Query key so the
PeriodStrip + provenance + vertical chip toggles all refetch in
lock-step with `KpiSummaryBar` / `EquityCharts`.  Mobile
(`TailRiskExposure.tsx`) accepts `days` / `period` props that the
dashboard screen wires from its local `period` state.  Default = 30
days for both (preserves historical behaviour and keeps the test
harness compatible).

**Tail-risk policy gates — counterfactual replay (May 2026, task
#745).**  `scripts/replay_tail_risk_2026.py` reconstructs a
`RiskContext` from each SYSTEM trade in the YTD 2026 cohort
(`placed_by IN ('orchestrator','autotrader','swarm','operator_recovery')`,
`executed_at >= 2026-01-01`) and runs the **full `app.risk.gates.GATES`
tuple** (default) with side effects suppressed (the script
monkey-patches `_emit_tail_risk_trip_alert` and
`_persist_risk_gate_trip` to no-ops via `_suppress_gate_side_effects()`
so a backtest never spams operator alerts or pollutes the daily
honesty report's `risk_gate_trips` panel).  Globally-stateful gates
(`kill_switch`, `daily_loss`, `vertical_halted`,
`uncalibrated_vertical`) are bypassed via replay-only operator
overrides because their verdict reflects *current* operator state,
not the state at trade time.  `--tail-only` runs just the two new
gates; `--from-json` swaps the DB cohort for a fixture.  Per-trade
veto reasons → `--out-csv`; aggregate → `--out-json`/stdout.

**Headline (live cohort, 2026-05-05; report at
`artifacts/api-server/reports/tail_risk_replay_2026-05-05.{csv,json}`):**
on **5,349 SYSTEM trades** (5,347 settled, total realised audit loss
$39,181.69) the two new gates would have **vetoed 21 trades and
prevented $6,952.41 of losses — 17.74 % of audit losses prevented**
while forfeiting $116.73 of incidental profit, flipping the cohort's
realised P&L from **−$5,663 to +$1,173 (+$6,836 net delta)**.
By gate: `tail_price_size_cap` 4 vetoes ($4,928 prevented / $113
forfeited), `jumbo_quantity_cap` 17 vetoes ($2,024 prevented / $4
forfeited).  Re-run with `python -m scripts.replay_tail_risk_2026
--from-db --out-csv reports/tail_risk_replay_<date>.csv --out-json
reports/tail_risk_replay_<date>.json` to refresh.

**Bucket drill-in (May 2026).**  Each bucket row in the tile is a
button: clicking on web (or tapping on mobile) navigates to
`/positions?bucket_lo=…&bucket_hi=…&days=…&placed_by=…&vertical=…&sort=max_loss`,
mirroring the same days/placed_by/vertical filters that drove the
tile.  The positions page parses those params, restricts rows to
trades with `entry/100 ∈ [lo, hi)`, surfaces a "Drilling in from
tail-risk tile" banner with a Clear button, and supports a
`max_loss` sort key (qty × entry cents).  Hovering a bucket on web
exposes a `title` peek listing the top 5 contributors by max-loss
without leaving the dashboard.

**Typed client.** The endpoint is published in
`lib/api-spec/openapi.yaml` as `getPublicTailRiskExposure` and the
Orval codegen produces typed React-Query hooks in
`lib/api-client-react/src/generated/` and Zod schemas in
`lib/api-zod/src/generated/`.  Future surfaces should consume those
types instead of re-defining the response shape locally.
