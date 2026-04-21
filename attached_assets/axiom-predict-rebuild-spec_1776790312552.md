# Axiom-Predict — REBUILD SPEC (Definitive)

> **This document supersedes SYSTEM.md as the single source of truth.**
> It merges SYSTEM.md (3,765 lines, 2026-04-21) with the rebuild-spec-audit.md findings.
> Replit Agent should follow this document step-by-step to rebuild the system from a clean repo.
> Every claim herein is traceable to source code or explicitly marked VERIFY.

**Version:** Rebuild v1.0 — 2026-04-21
**Production model:** v155, Brier 0.2094, AUC 0.7127, Core-4 Brier ~0.180
**Live bankroll:** ~$8.4k on Kalshi (~27 concurrent positions)

---

## Table of Contents

### PART I — EXECUTIVE SPEC
1. [What Axiom-Predict IS](#1-what-axiom-predict-is)
2. [Rebuild Goals & Non-Goals](#2-rebuild-goals--non-goals)
3. [Deployment Topology](#3-deployment-topology)

### PART II — COMPLETE ARCHITECTURE
4. [Repository Layout](#4-repository-layout-final-canonical)
5. [Backend — FastAPI](#5-backend--fastapi)
6. [The Trading Loop](#6-the-trading-loop)
7. [Verticals (Honest Status)](#7-verticals-honest-status)
8. [ML Pipeline (ACTUAL state)](#8-ml-pipeline-actual-state)
9. [Calibration System](#9-calibration-system)
10. [Swarm Intelligence + LLM Gatekeeper](#10-swarm-intelligence--llm-gatekeeper)
11. [Risk Management (12 Gates)](#11-risk-management-12-gates)
12. [Execution Layer](#12-execution-layer)
13. [Self-Healing & AutoResearch](#13-self-healing--autoresearch)
14. [Frontend — React + Vite](#14-frontend--react--vite)
15. [Mobile App Expo](#15-mobile-app-expo)
16. [UW MCP Service](#16-uw-mcp-service)

### PART III — BUILD MANIFEST
17. [Python Dependencies](#17-python-dependencies-exact-versions)
18. [Node Dependencies](#18-node-dependencies)
19. [Environment Variables](#19-environment-variables-envexample)
20. [Database Schema](#20-database-schema-single-source-of-truth)
21. [External APIs Required](#21-external-apis-required)
22. [Docker / Deployment](#22-docker--deployment)

### PART IV — REBUILD PLAN
23. [Phase 1: Foundation (Week 1)](#23-phase-1-foundation-week-1)
24. [Phase 2: Core Backend (Week 2-3)](#24-phase-2-core-backend-week-2-3)
25. [Phase 3: ML Pipeline (Week 3-4)](#25-phase-3-ml-pipeline-week-3-4)
26. [Phase 4: Trading Loop (Week 4-5)](#26-phase-4-trading-loop-week-4-5)
27. [Phase 5: Frontend (Week 5-6)](#27-phase-5-frontend-week-5-6)
28. [Phase 6: Verticals (Week 6-8)](#28-phase-6-verticals-week-6-8)
29. [Phase 7: Observability (Week 8-9)](#29-phase-7-observability-week-8-9)
30. [Phase 8: Self-Improvement (Week 9-10)](#30-phase-8-self-improvement-week-9-10)
31. [Phase 9: Polish + Investment-Ready (Week 10-12)](#31-phase-9-polish--investment-ready-week-10-12)

### PART V — APPENDICES
- [Appendix A: Complete Router Inventory (27 routers)](#appendix-a-complete-router-inventory-27-routers)
- [Appendix B: Complete Page Inventory (123 pages)](#appendix-b-complete-page-inventory-123-pages)
- [Appendix C: All 145 Selected Features (Model v155)](#appendix-c-all-145-selected-features-model-v155)
- [Appendix D: Complete Env Var List (135 vars)](#appendix-d-complete-env-var-list-135-vars)
- [Appendix E: Complete Table Schema (80 tables)](#appendix-e-complete-table-schema-80-tables)
- [Appendix F: Swarm Prompts (preserved)](#appendix-f-swarm-prompts-preserved)
- [Appendix G: What to DELETE from Current Repo](#appendix-g-what-to-delete-from-current-repo)
- [Appendix H: Known Bugs to Fix in Rebuild](#appendix-h-known-bugs-to-fix-in-rebuild)
- [Appendix I: Investment Readiness Checklist](#appendix-i-investment-readiness-checklist)

---

# PART I — EXECUTIVE SPEC

---

## 1. What Axiom-Predict IS

**Axiom-Predict** is a 24/7 autonomous multi-vertical prediction market trading platform. It generates probabilistic forecasts using ML ensembles + LLM-based swarm debate, gates every candidate trade through 12 independent risk checks, and submits live limit orders on Kalshi.

### Product Definition

| Dimension | Value |
|---|---|
| **Primary Exchange** | Kalshi (sports, weather, crypto binary, events, economics) |
| **Secondary Exchange** | Polymarket (surveillance + arb scanning — execution not active) |
| **Spot Crypto** | Robinhood (paper mode only — 0% WR historically) |
| **Model** | Stacking ensemble v155: LGB + XGB (active), CatBoost (wired, VERIFY), LSTM (deferred) |
| **Brier Score (all sports)** | 0.2094 |
| **AUC** | 0.7127 |
| **ECE** | 0.0192 |
| **Core-4 Brier** | ~0.180 (NBA, NHL, NCAAB, MLB) |
| **Bankroll** | ~$8.4k live Kalshi |
| **Cycle cadence** | ~2 min per multi-vertical pass |
| **Concurrent positions** | ~27 typical |
| **EV signals scanned/cycle** | ~1,000–1,200 (from OUTLIER) → 0–3 actual trades |

### Target Users

1. **Internal operator** (Colter Mahlum, solo) — primary user. All trade emails route to `coltermahlum@gmail.com`.
2. **Future external users** — subscription model via Stripe, multi-user auth, leaderboard.

### Competitive Positioning

The system differentiates from sharp sports bettors by:
- Operating on **prediction markets** (Kalshi binary contracts) not sportsbook moneylines
- **12-gate defense in depth** — most retail bots gate on EV alone
- **6-persona LLM swarm** with per-analyst historical performance weighting
- **Continuous self-improvement** (AutoResearch + online SGD learner)
- **Per-sport calibration** (6 sports, isotonic + Platt, with drift detection)
- **Weather vertical** with dedicated 10-source meteorological ensemble
- **Self-healing** 3-layer process supervision

### Current Operational State

```
Live verticals:     Sports (NBA, NHL, MLB, NCAAB, NFL + international)
                    Weather (KXHIGH*/KXTEMP*/KXLOW* on Kalshi)
                    Crypto Binary (KXBTC15M, 1H, daily variants)
                    Player Props
                    Events / Economics (EVENT_ECONOMICS only)
Paper verticals:    Geopolitics, Tech/AI, Stocks/Commodities, Entertainment, Elections
Paper exchanges:    Robinhood spot, Polymarket execution
```

---

## 2. Rebuild Goals & Non-Goals

### Keep (Working — Copy Cleanly)

| Component | Source | Status |
|---|---|---|
| `src/trading/kalshi_client.py` | RSA-PSS signed JWT, real order placement | ✅ Working |
| `src/trading/order_manager.py` | Order lifecycle, partial fills, GTC detection | ✅ Working |
| Trading loop logic | `server_core.py` → extract to `trading_loop.py` | ✅ Working (consolidate) |
| 6-persona swarm | `src/trading/swarm_debate.py` | ✅ Working |
| LLM gatekeeper | `src/trading/llm_gatekeeper.py` | ✅ Working |
| Calibration (11 files) | `src/calibration/` | ✅ Working |
| Paper/Live toggle | `AXIOM_PAPER_MODE` / `AXIOM_TRADING_MODE` env | ✅ Working |
| SportsRadar cache (49 JSON) | `data/sportradar/` | ✅ Populated |
| UW MCP service | `uw-mcp/` (Hono + MCP SDK) | ✅ Implemented |
| 27 FastAPI routers | `src/api/routers/` | ✅ Implemented |
| Frontend 123 pages | `frontend/src/pages/` | ✅ Implemented |
| Weather v6 model | `src/weather/` (10-source ensemble) | ✅ Working |
| Crypto binary engine | `src/crypto/binary_trading_engine.py` | ✅ Working |
| Settlement feedback | `src/trading/settlement_feedback.py` | ✅ Working (VERIFY per-fill bug) |
| Kelly + risk manager | `src/trading/kelly.py`, `risk_manager.py` | ✅ Working |
| AutoResearch engine | `src/ml_training/autoresearch_engine.py` | ✅ Working |

### Drop (Delete in Rebuild)

| Item | Reason |
|---|---|
| `src/api/server_core.py` | Duplicate of `unified_server.py` (17,205 lines). Delete. |
| `services/data-pipeline/` | Pick `services/data_pipeline/` (underscore), delete hyphen variant |
| `services/model-upgrade/` | Pick `services/model_upgrade/` (underscore), delete hyphen variant |
| `src/models/lstm_model.py` (as trainer) | Orphaned — not wired. Defer to v2. Keep file but do not call from trainer. |
| `src/models/transformer_model.py` | Unused. Delete. |
| `src/features/feature_store.py` (v1) | Replace with `src/feature_engine/feature_store_v2.py` |
| `src/features/store/feature_store.py` | Third duplicate. Delete. |
| `models/evaluation_report_v*.json` (300+) | Archive to cold storage. Keep last 10 only. |
| `models/current_weights_v*.tmp.json` (14) | Interrupted writes. Delete. |
| Root-level `.js`/`.mjs` scripts | Move to `scripts/` or delete |
| `debug-odds.js`, `fetch-polymarket-soccer.mjs`, `fetch-soccer-live.mjs`, `fetch-soccer-schedule.mjs`, `generate-live-update.mjs`, `market-inefficiency-scanner.js`, `hou-isu-monitor.sh`, `market-inefficiency-scanner-monitor.js`, `test-kalshi-api.mjs`, `update-soccer-scores.js`, `update-soccer-scores.mjs` | Root clutter — move to `scripts/` |
| `historical-odds-researcher-FINAL.json`, `historical-odds-researcher-update.json`, `historical-odds-researcher.json`, `ncaab-data-collector.json` | Move to `config/research/` or archive |
| `training_result.json`, `pipeline_state.json` at root | Move to `data/` or `artifacts/` |
| `catboost_info/` at root | CatBoost training info. Delete or move to `artifacts/`. |
| `src/api/server.py`, `src/api/app.py`, `src/api/gateway.py` | Legacy entry variants. Delete. |
| `src/api/kalshi_routes.py` | Legacy Kalshi routes (superseded by `routers/exchanges.py`). Delete after VERIFY. |
| Duplicate `Dockerfile.data-pipeline`, `Dockerfile.evaluation`, `Dockerfile.model-upgrade` | Superseded by multi-stage `Dockerfile`. Delete. |
| `ARCHITECTURE.md` | Legacy OpenClaw era docs. Archive. |

### Add (Missing — Must Build)

| Item | Priority |
|---|---|
| `migrations/0001_initial.sql` — canonical schema from 33 source files | CRITICAL |
| `alembic/` — Alembic setup for future schema changes | HIGH |
| `.env.example` — all 135 env vars with descriptions and defaults | HIGH |
| `src/api/trading_loop.py` — extract loop from `server_core.py`, single file | HIGH |
| Per-sport per-model features fully wired to training runner | HIGH |
| CatBoost wired into `model_trainer.py` (class exists, just add import + call) | HIGH |
| `pyproject.toml` — lock Python version + metadata | MEDIUM |
| Clean topology docs in `docs/TOPOLOGY.md` | MEDIUM |
| JSON schemas for key config files | MEDIUM |
| Fix `OPENAI_APY_KEY` typo → `OPENAI_API_KEY` everywhere | LOW (but required) |
| Consolidate dual frontend auth (Node `pg/bcryptjs` vs FastAPI JWT) — pick FastAPI | MEDIUM |

---

## 3. Deployment Topology

**Decision: Option A — Monolith on Mac Studio + Replit Frontend Proxy**

This is the current production topology as documented in `replit.md`. Option B (Docker multi-service cloud) is deferred to v2 when system is investment-ready and staffed.

### Process Map

```
┌─────────────────────── Replit Workspace (NixOS container) ──────────────────────┐
│                                                                                  │
│  frontend/server.js  (Node, port 3000 or Replit-exposed)                        │
│     └── serves built React SPA (vite build output)                              │
│     └── proxies /api/* → MAC_STUDIO_BACKEND_URL (env var)                       │
│     └── serves WebSocket upgrades transparently                                  │
│                                                                                  │
│  uw-mcp/ Node service  (port varies, internal)                                  │
│     └── Hono HTTP + MCP SDK                                                      │
│     └── Proxied from Python backend via /api/uw-mcp/*                           │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────── Mac Studio (backend, persistent) ────────────────────────┐
│                                                                                  │
│  start_backend.py  (supervisor, TCP proxy 8000 → 8001)                          │
│     └── uvicorn src.api.unified_server:app --port 8001                          │
│         ├── _trading_loop_inner() [async background task]                       │
│         ├── 27 REST routers                                                      │
│         ├── WebSocket hub (positions, pipeline, trades, temperature, llm-logs)  │
│         ├── Concierge AI (Telegram + UI)                                         │
│         └── PostgreSQL connection (Replit-managed Postgres)                     │
│                                                                                  │
│  scripts/backend_watchdog.py  (port 8099 — layer 2 watchdog)                   │
│     └── pings 127.0.0.1:8000 every 5s                                           │
│     └── respawns supervisor after 30s consecutive port-down                     │
│     └── 120s cooldown                                                            │
│                                                                                  │
│  scripts/train_ensemble_fast.py  (triggered manually or via workflow)           │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────── Replit-Managed PostgreSQL ───────────────────────────────┐
│  ~80 tables (see §20)                                                            │
│  DATABASE_URL env var                                                            │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### Port Map

| Port | Service | Notes |
|---|---|---|
| 8000 | TCP proxy (supervisor) | Public-facing, held by `start_backend.py` always |
| 8001 | uvicorn (FastAPI) | Internal only |
| 8099 | Backend watchdog HTTP | Internal health surface |
| 3000 | Vite dev / Node server | Frontend, Replit-exposed |
| 6800 | GitNexus | Code intelligence (currently failed — non-blocking) |

### Env Vars Per Service

See §19 for the full 135-var list. Critical per-service subset:

**Backend (Mac Studio):**
```bash
DATABASE_URL            # Replit Postgres URL
KALSHI_API_KEY_ID       # Kalshi trading
KALSHI_API_PRIVATE_KEY  # RSA private key (PEM string or path)
KALSHI_ENV              # demo | prod
AXIOM_PAPER_MODE        # true | false
ANTHROPIC_API_KEY       # Swarm + gatekeeper LLMs
OPENAI_API_KEY          # Swarm alternate + BTC model
SESSION_SECRET          # Auth sessions
```

**Frontend (Replit):**
```bash
VITE_API_URL            # Points to Mac Studio backend
MAC_STUDIO_BACKEND_URL  # Node proxy target
SESSION_SECRET          # Must match backend
```

**UW MCP:**
```bash
UNUSUAL_WHALES_API_KEY
UW_MCP_SERVICE_URL
DATABASE_URL            # Same Postgres
```

### Process Supervision

Three layers (must all be present):

| Layer | File | Trigger | Recovery |
|---|---|---|---|
| L1 | `start_backend.py` | uvicorn child exit → SIGCHLD | Respawn child within 5s; port 8000 stays held |
| L2 | `scripts/backend_watchdog.py` | 30s consecutive port-down on 8000 | Respawn supervisor as detached process group |
| L3 | Replit workflow runner | Watchdog process crash | `autoStart=true` in `.replit` |

Signal handling: SIGTERM → graceful `_halted=True` → trading loop exits cleanly within one cycle.

---

# PART II — COMPLETE ARCHITECTURE

---

## 4. Repository Layout (Final Canonical)

The clean rebuild target. Items marked `[DELETE]` should be removed. Items marked `[KEEP]` are working. Items marked `[NEW]` need to be added.

```
.
├── src/                                # Python backend (610 files, ~243k LOC)
│   ├── api/                            # FastAPI application
│   │   ├── unified_server.py           # [KEEP] Canonical entrypoint — 11,708 lines
│   │   ├── server_core.py              # [DELETE] Duplicate kernel — extract loop to trading_loop.py
│   │   ├── trading_loop.py             # [NEW] Extracted _trading_loop_inner() from server_core.py
│   │   ├── routers/                    # [KEEP] 27 router modules (see Appendix A)
│   │   │   ├── __init__.py
│   │   │   ├── admin.py
│   │   │   ├── ai_ml.py
│   │   │   ├── auth.py
│   │   │   ├── calibration.py
│   │   │   ├── concierge.py
│   │   │   ├── crypto.py
│   │   │   ├── events.py
│   │   │   ├── exchanges.py
│   │   │   ├── health.py
│   │   │   ├── live_scores.py
│   │   │   ├── notifications.py
│   │   │   ├── orchestrator.py
│   │   │   ├── pipeline_logs.py
│   │   │   ├── portfolio.py
│   │   │   ├── signals.py
│   │   │   ├── sportradar.py
│   │   │   ├── sports.py
│   │   │   ├── sports_core.py
│   │   │   ├── stripe_billing.py
│   │   │   ├── telegram.py
│   │   │   ├── trading.py
│   │   │   ├── uw_mcp.py
│   │   │   ├── uw_predictions.py
│   │   │   ├── watchdog.py
│   │   │   ├── weather.py
│   │   │   └── websockets.py
│   │   ├── shared/state.py             # [KEEP] Module-global state container
│   │   ├── concierge.py                # [KEEP] Concierge AI handler
│   │   ├── email_notifications.py      # [KEEP] Postmark sender
│   │   ├── notification_scheduler.py   # [KEEP] Scheduled reports
│   │   ├── telegram_bot.py             # [KEEP] Telegram webhook
│   │   ├── trading_bridge.py           # [KEEP] Cross-system state bridge
│   │   ├── _trading_loop_watchdog.py   # [KEEP] In-loop watchdog
│   │   ├── stripe_billing.py           # [KEEP] Top-level billing
│   │   ├── middleware.py               # [KEEP] Auth, CORS, logging, rate limit
│   │   ├── kalshi_routes.py            # [DELETE after VERIFY] Legacy — superseded by routers/exchanges.py
│   │   ├── mac_studio_proxy.py         # [KEEP] Optional ML offload proxy
│   │   ├── server.py                   # [DELETE] Legacy entry
│   │   ├── app.py                      # [DELETE] Legacy entry
│   │   ├── gateway.py                  # [DELETE] Legacy entry
│   │   ├── routes.py                   # [VERIFY then DELETE or fold]
│   │   ├── schemas.py                  # [KEEP if used, else DELETE]
│   │   ├── services.py                 # [KEEP] Service bootstrap
│   │   ├── dashboard_api.py            # [KEEP] Dashboard endpoints
│   │   ├── intangibles.py              # [KEEP] Intangibles endpoints
│   │   └── weather_model.py            # [KEEP] Weather model endpoints
│   ├── calibration/                    # [KEEP] 13 modules — see §9
│   ├── crypto/                         # [KEEP] 26 modules — see §7
│   ├── data/                           # [KEEP] 48 modules — sport adapters, ESPN, Outlier
│   ├── data_feeds/                     # [KEEP] 3 modules — Elo engine, stats fetcher
│   ├── data_pipeline/                  # [KEEP] 56 modules — ingestion, validation, streaming
│   ├── evaluation/                     # [KEEP] 83 modules — metrics, monitoring, CLV, post-mortems
│   ├── events/                         # [KEEP] 7 modules — current-events vertical
│   ├── feature_engine/                 # [KEEP] 26 modules — feature transforms; keep feature_store_v2.py
│   ├── features/                       # [KEEP minus duplicates] 90 modules
│   │   ├── feature_store.py            # [DELETE] Superseded by feature_store_v2.py
│   │   └── store/feature_store.py      # [DELETE] Third duplicate
│   ├── ml_training/                    # [KEEP] 15 modules — training + improvement
│   ├── models/                         # [KEEP minus orphans] 53 modules
│   │   ├── lstm_model.py               # [KEEP FILE, DO NOT CALL from trainer — deferred v2]
│   │   └── transformer_model.py        # [DELETE] Unused
│   ├── model_upgrade/                  # [KEEP] 26 modules — registry, rollout, serving
│   ├── real_time/                      # [KEEP] 2 modules — real-time aggregation
│   ├── reporting/                      # [KEEP] 2 modules
│   ├── reports/                        # [KEEP] 2 modules
│   ├── shared/                         # [KEEP] Cross-cutting state, contracts, config
│   ├── signals/                        # [KEEP] 4 modules
│   ├── trading/                        # [KEEP] 43 modules — see §6, §11, §12
│   ├── utils/                          # [KEEP] 5 modules
│   ├── verticals/                      # [KEEP] 16 modules — non-sports vertical engines
│   ├── watchdog/                       # [KEEP] 2 modules — daily audit watchdog
│   └── weather/                        # [KEEP] 12 modules — weather model v6
│
├── frontend/                           # [KEEP] React + Vite + Tailwind + TypeScript
│   ├── src/
│   │   ├── pages/                      # [KEEP] 123 pages (see Appendix B)
│   │   ├── components/                 # [KEEP] 28 component groups
│   │   ├── hooks/                      # [KEEP] 22 custom React hooks
│   │   ├── contexts/                   # [KEEP] AuthContext, WebSocketContext
│   │   ├── api/                        # [KEEP] apiClient, client, portfolio
│   │   ├── config/                     # [KEEP] Frontend config
│   │   ├── styles/                     # [KEEP] Tailwind + custom CSS
│   │   ├── types/                      # [KEEP] TypeScript types
│   │   └── utils/                      # [KEEP] Frontend helpers
│   ├── server.js                       # [KEEP] Node proxy (but consolidate auth to FastAPI)
│   ├── vite.config.ts                  # [KEEP] Vite build config
│   ├── tailwind.config.js              # [KEEP]
│   └── package.json                    # [KEEP] See §18
│
├── mobile/                             # [KEEP AS-IS] Internal dashboard (5 screens)
│   ├── app/(tabs)/                     # index, pipeline, positions, research, settings
│   └── package.json
│
├── uw-mcp/                             # [KEEP] TypeScript MCP service
│   ├── src/                            # index.ts, api/, bridge/, db/, mcp/, pipelines/
│   └── package.json
│
├── config/                             # [KEEP] 30+ JSON config files — see §19
├── scripts/                            # [KEEP + RECEIVE from root] 120+ scripts
│   └── migrations/                     # [ADD] Alembic + SQL migration files
│
├── migrations/                         # [NEW] Canonical DB migrations
│   └── 0001_initial.sql                # Generated from 33 CREATE TABLE sources
│
├── data/                               # [KEEP] Generated cache / feed snapshots
│   ├── sportradar/                     # 49 JSON schedule + injury files
│   ├── feature_store/                  # Parquet artifacts
│   └── cache/                          # SQLite caches
│
├── models/                             # [KEEP + CLEAN] Trained model artifacts
│   ├── champion_metadata.json          # Active champion
│   ├── ensemble_model.pkl              # LGB + XGB stacker
│   ├── catboost_model.pkl              # CatBoost (keep)
│   ├── crypto_lgb.txt                  # Crypto LGB
│   ├── crypto_xgb.json                 # Crypto XGB
│   └── [DELETE] evaluation_report_v1..v300.json  # Archive
│
├── docs/                               # [KEEP + EXPAND]
│   ├── TOPOLOGY.md                     # [NEW] Clean deployment topology
│   └── ...
│
├── tests/                              # [KEEP + EXPAND]
├── attached_assets/                    # [KEEP]
│
├── start_backend.py                    # [KEEP] In-process supervisor + TCP proxy
├── start_production.py                 # [KEEP] Production entry
├── startup.sh                          # [KEEP]
├── service_launcher.py                 # [KEEP] Generic launcher (AXIOM_SERVICE env)
├── pyproject.toml                      # [NEW] Python project metadata
├── requirements.txt                    # [KEEP] See §17
├── requirements-evaluation.txt         # [KEEP]
├── .env.example                        # [NEW] All 135 vars
├── replit.md                           # [KEEP] Replit-specific memory
├── replit.nix                          # [KEEP] Nix dependencies
│
# [DELETE these root files]:
# server_core.py (duplicate)
# debug-odds.js, fetch-polymarket-soccer.mjs, fetch-soccer-live.mjs
# fetch-soccer-schedule.mjs, generate-live-update.mjs, market-inefficiency-scanner.js
# hou-isu-monitor.sh, market-inefficiency-scanner-monitor.js, test-kalshi-api.mjs
# update-soccer-scores.js, update-soccer-scores.mjs
# historical-odds-researcher-FINAL.json, historical-odds-researcher-update.json
# historical-odds-researcher.json, ncaab-data-collector.json
# training_result.json, pipeline_state.json (move to data/)
# catboost_info/ (move to artifacts/ or delete)
# ARCHITECTURE.md (archive)
│
# services/ directory — clean up naming:
├── services/
│   ├── data_pipeline/                  # [KEEP] (underscore)
│   ├── data-pipeline/                  # [DELETE] (hyphen — duplicate)
│   ├── model_upgrade/                  # [KEEP] (underscore)
│   ├── model-upgrade/                  # [DELETE] (hyphen — duplicate)
│   ├── evaluation/                     # [KEEP]
│   ├── real-time/                      # [KEEP]
│   └── shared/                         # [KEEP]
│
└── artifacts/                          # [NEW] Training outputs, state snapshots
    ├── training_result.json            # Moved from root
    └── pipeline_state.json             # Moved from root
```

---

## 5. Backend — FastAPI

### Entrypoint & Lifecycle

**Canonical file:** `src/api/unified_server.py`

Launch sequence:
1. `start_production.py` imports `unified_server`, starts `start_backend.py`
2. `start_backend.py` binds TCP proxy on **port 8000** → forks uvicorn on **port 8001**
3. FastAPI app uses `lifespan` context with `asyncio.create_task(_deferred_startup_task())` — keeps `/api/health` responsive immediately while heavy init runs
4. Deferred init sequence:
   - ML ensemble loading
   - Calibrator warm-up
   - Feature store hydration
   - Performance monitor init
   - Kalshi RSA-PSS handshake
   - Standalone vertical engines (events, geopolitics, tech_ai, stocks_commodities, entertainment, elections)
5. `_deferred_init_complete` flag flips → trading loop READY-gate releases

### Registration Order (`_ROUTER_REGISTRY`)

```python
# Registered BEFORE main registry:
app.include_router(pipeline_logs.router)  # pipeline events

# _ROUTER_REGISTRY (in order):
admin, ai_ml, auth, calibration, concierge, crypto, events, exchanges,
health, live_scores, notifications, orchestrator, portfolio, signals,
sportradar, sports, sports_core, stripe_billing, telegram, trading,
uw_mcp, uw_predictions, watchdog, weather

# Registered AFTER main registry:
app.include_router(websockets.router)  # WS endpoints
```

`_CRITICAL_ROUTERS` (failures abort startup): `auth`, `trading`, `exchanges`, `health`

### All 27 Routers — Summary

See **Appendix A** for complete path + method + auth table.

| Router | Path Prefix | Primary Domain |
|---|---|---|
| `admin.py` | `/api/admin` | Multi-user admin |
| `ai_ml.py` | `/api/llm`, `/api/swarm`, `/api/ollama` | LLM, swarm, ML training |
| `auth.py` | `/api/auth` | Auth + WebAuthn |
| `calibration.py` | `/api/calibration` | Calibration status |
| `concierge.py` | `/api/concierge` | AI chat |
| `crypto.py` | `/api/crypto` | Crypto state |
| `events.py` | `/api/events` | Events vertical |
| `exchanges.py` | `/api/kalshi`, `/api/polymarket`, `/api/robinhood` | Exchange interfaces |
| `health.py` | `/api/health`, `/api/monitoring`, `/api/risk-console` | System health |
| `live_scores.py` | `/api/live-scores` | Live game state |
| `notifications.py` | `/api/notifications` | Push/email/schedule |
| `orchestrator.py` | `/api/orchestrator` | Pipeline orchestration |
| `pipeline_logs.py` | `/api/pipeline/logs` | Cycle audit trail |
| `portfolio.py` | `/api/portfolio`, `/api/positions`, `/api/bankroll`, `/api/orders` | Position aggregation |
| `signals.py` | `/api/signals` | EV scanner output |
| `sportradar.py` | `/api/sportradar` | Sportradar passthrough |
| `sports.py` | `/api/espn`, `/api/predictions`, `/api/elo`, `/api/odds` | Sports data |
| `sports_core.py` | `/api/sports`, `/api/stats-cache`, `/api/intangibles`, `/api/clv` | Sports core |
| `stripe_billing.py` | `/api/stripe` | Billing |
| `telegram.py` | `/api/telegram` | Telegram webhook |
| `trading.py` | `/api/trading`, `/api/settings`, `/api/risk`, `/api/circuit-breakers` | Control plane |
| `uw_mcp.py` | `/api/uw-mcp` | UW MCP bridge |
| `uw_predictions.py` | `/api/uw` | Polymarket surveillance |
| `watchdog.py` | `/api/watchdog` | Daily audit |
| `weather.py` | `/api/weather` | Weather vertical |
| `websockets.py` | `/ws/*` | WebSocket channels |

### Middleware Stack

```python
# Order matters (outermost first):
1. CORS middleware       — configured via CORS_ORIGINS env
2. Request logging       — structured JSON, correlates request_id
3. Session-based auth    — cookies → sessions table
4. Rate limiting         — API_RATE_LIMIT_PER_MINUTE env
```

**Auth strategy for rebuild:**
- FastAPI `auth.py` router handles `POST /api/auth/login` → sets session cookie
- `frontend/server.js` currently has a parallel `pg`/`bcryptjs` Node auth layer
- **Rebuild decision:** Consolidate to FastAPI auth only. Remove `pg`/`bcryptjs` from `frontend/server.js`. Frontend `server.js` becomes a pure proxy with no auth logic.

### WebSocket Channels

| Channel | Purpose |
|---|---|
| `WS /ws/positions` | Live position updates |
| `WS /ws/pipeline` | Live cycle / gate events |
| `WS /ws/trades` | Live trade execution events |
| `WS /ws/temperature/{city_code}` | Live temperature stream per city |
| `WS /ws/llm-logs` | Live LLM call logs |
| `WS /ws/orderbook/{ticker}` | Live orderbook (Kalshi passthrough) |
| `WS /ws/alerts` | System alerts |

---

## 6. The Trading Loop

### Architecture Decision

The trading loop is **NOT** a single file. It is assembled across:
- `src/api/server_core.py:_trading_loop_inner()` (~800 lines) — **source to extract**
- `src/api/unified_server.py` — launches the loop as asyncio background task
- `src/trading/paper_trading_runner.py` — parallel paper track

**Rebuild action:** Extract `_trading_loop_inner()` and all supporting state (`_halted`, `_halt_reason`, `_kalshi_cache`, `_deferred_init_complete`) into `src/api/trading_loop.py`. Delete `server_core.py`.

### Sequence Diagram

```
Cycle Tick (~2 min interval)
│
├─ [GATE 1: HALT STATE]
│   └── _halted == True → sleep 30s, skip cycle
│
├─ [GATE 2: PERFORMANCE MONITOR]
│   └── _check_monitor_health()
│       ├── Kalshi loss streak ≥ 8 → auto-pause
│       ├── Kalshi loss streak ≥ 5 → reduce sizing
│       └── Kalshi daily P&L ≤ -$50 → auto-pause
│
├─ [GATE 3: CORRELATED LOSS CHECK]
│   └── unified_risk_manager.periodic_correlation_check()
│       └── Cross-position correlation in 1h window > 3% → halt
│
├─ BANKROLL SNAPSHOT
│   └── effective_bankroll = live_kalshi_balance × buying_power_pct
│   └── cap = 100% (or pct from risk_limits.json)
│
├─ MARKET HOURS CHECK
│   └── blackout_hours_et: 23:00 → 11:00 ET → skip Kalshi verticals
│
├─ SPORTS VERTICAL (if sportsTradingEnabled AND in_market_hours)
│   ├── Schedule fetch (Sportradar + ESPN)
│   ├── Market discovery (KXNBAGAME-*, KXMLBGAME-*, props)
│   ├── For each candidate pick:
│   │   ├─ [GATE 4: DEDUP] position_dedup.py — already in position?
│   │   ├─ ML INFERENCE: 698 features → 145 selected → stacking ensemble
│   │   │   (LGB + XGB + CatBoost → meta-learner)
│   │   ├─ CALIBRATION: per-sport calibrator (CFB/MLB/NBA/NCAAB/NFL/NHL)
│   │   ├─ [GATE 5: EDGE FLOOR] edge_floors.py — below min_edge → skip
│   │   ├─ [GATE 6: SWARM] swarm_debate.py — 6 personas, 2 rounds
│   │   ├─ [GATE 7: LLM GATEKEEPER] llm_gatekeeper.py — final veto
│   │   ├─ [GATE 8: HEALTH GATE] health_gates.py — calibration, freshness, divergence
│   │   ├─ [GATE 9: RISK MANAGER] risk_manager.py — exposure, correlation, Kelly cap
│   │   ├─ [GATE 10: POSITION DEDUP (second check)] position_dedup.py
│   │   └─ ORDER SUBMISSION → execution_engine.py → kalshi_client.py
│   │
│   └── Player Props sub-pipeline
│       ├── Pull 270+ prop lines (SportsLine + OUTLIER)
│       ├── Match to 1,377+ Kalshi prop markets
│       ├── Inject player stats (NBA/MLB/NFL/NHL stats)
│       └── Standard gates (edge → swarm → gatekeeper → risk → execute)
│
├─ OUTLIER SCANNER
│   ├── api.outlier.bet → ~1,200 EV+ bets
│   ├── Filter EV ≥ 5%
│   ├── Resolve series → Kalshi ticker
│   ├── Match to ESPN game
│   ├── Enrich with 135 features
│   ├── ML ensemble re-prediction
│   ├── LLM approval (game-level)
│   └── Risk gate → execute
│
├─ CRYPTO BINARY (KXBTC15M, 1H, daily)
│   ├── KalshiMarketLoader._price() (dollar-era API migration)
│   ├── Walk-forward XGB + LGB ensemble prediction
│   ├── [GATE: posterior_dev ≥ ±3% from 0.50] — kill coin-flips
│   ├── LLM approval required for ALL crypto binary trades
│   ├── Kelly sizing × calibrator.sizing_scale
│   └── Execute (max $150 exposure, 100 trades/day)
│
├─ WEATHER VERTICAL
│   ├── tier_scheduler picks candidates
│   ├── Quality gates: source_agreement ≥ 0.35, edge ≥ 6%, not blacklisted
│   ├── Weather Swarm (6 personas)
│   ├── Weather LLM gatekeeper
│   ├── WEATHER SANITY (count cap)
│   ├── WEATHER HEALTH GATE (ECE ≤ 0.035)
│   ├── 3-layer city dedup (1 per city per day)
│   └── Execute (max $25/trade, low-confidence half-sized)
│
├─ EVENTS / ECONOMICS VERTICALS (EVENT_ECONOMICS = LIVE, rest = PAPER)
│   └── base_engine.py → LLM probability estimate → category Kelly mult → execute
│
├─ CRYPTO SPOT (Robinhood) — PAPER ONLY
│   └── Independent path, swing-trading config, min 4h hold
│
└─ CYCLE STATS UPDATE
    └── cycles_completed++, last_cycle timestamp, sleep until next cycle
```

### Crucial Invariants

- `cycle_exposure_added` **must be initialized at function scope** (not branch scope) — was a bug causing `UnboundLocalError` in OUTLIER/crypto branches
- `_halted` flag is single-source-of-truth, mutated only by `_check_monitor_health()` or explicit API
- `_kalshi_cache.balance_ts` checked before every cycle — stale → refresh from API
- `_deferred_init_complete` must be `True` before first cycle (READY-gate)
- LLM gatekeeper bypass is **force-disabled in production** — every Kalshi-side trade requires LLM approval

### 12-Gate Defense in Depth

| Gate # | Layer | Component | Decision |
|---|---|---|---|
| 1 | Halt state | `_halted` in trading_loop.py | Skip cycle entirely |
| 2 | Performance monitor | `evaluation/monitoring/performance_monitor.py` | Auto-pause on streak/PnL |
| 3 | Correlated loss | `shared/unified_risk_manager.py` | Periodic correlation halt |
| 4 | Vertical gate | `sportsTradingEnabled` / vertical toggles | Skip vertical |
| 5 | Calibration brake | `unified_calibrator.sizing_scale` | Multiply Kelly by 0 |
| 6 | Health gates | `trading/health_gates.py` | Block individual trade |
| 7 | Edge gate | per-vertical `min_edge` | Block individual trade |
| 8 | Swarm gate | `trading/swarm_debate.py` | Block individual trade |
| 9 | LLM gatekeeper | `trading/llm_gatekeeper.py` | Block individual trade |
| 10 | Risk manager | `trading/risk_manager.py` | Cap exposure, kelly, correlation |
| 11 | Position dedup | `trading/position_dedup.py` | Block duplicate market+side |
| 12 | Resting order check | Kalshi API | Block if open GTC exists |

---

## 7. Verticals (Honest Status)

### Production-Grade Verticals (Deploy Immediately)

| Vertical | Depth | Revenue Path | Files |
|---|---|---|---|
| **Sports** | Deep | Primary | `src/api/routers/sports.py`, `sports_core.py`, `src/features/`, `src/models/` |
| **Weather** | Deep | Secondary | `src/weather/` (12 files, v6 model, 10 sources) |
| **Crypto Binary** | Deep | Secondary | `src/crypto/binary_trading_engine.py` (3,836 lines) |
| **Player Props** | Medium | Part of Sports | `src/features/player_features.py`, props sections in sports.py |

### Exchange Integrations (Not Verticals)

| Integration | Status | Files |
|---|---|---|
| **Kalshi** | Live — primary | `src/trading/kalshi_client.py` |
| **Polymarket** | Surveillance + arb only | `src/trading/polymarket_client.py`, `polymarket_order_manager.py` |
| **Robinhood** | Paper only | `src/trading/robinhood_client.py` |
| **OUTLIER scraper** | Data source, not vertical | `src/data/outlier_scraper.py` |

### Events / Economics (Partially Live)

| Category | Status | Kelly Multiplier |
|---|---|---|
| `EVENT_ECONOMICS` (CPI, GDP, FOMC, jobs, commodities) | **LIVE** | 0.15 |
| Politics, Geopolitics, Tech/AI, Entertainment, Elections | **PAPER** | 0.05–0.08 |

Implementation: `src/events/` (6 files + 7 modules), `src/verticals/` (6 engine files + base).

### Stub/Deferred Verticals (Do NOT claim as production)

| Vertical | Engine File | Size | Data Feed | Decision |
|---|---|---|---|---|
| Elections | `elections_engine.py` | 99 LoC | `data_feeds/elections_data.py` (189 LoC) | Keep as paper v2 |
| Entertainment | `entertainment_engine.py` | 70 LoC | `data_feeds/entertainment_data.py` (180 LoC) | Keep as paper v2 |
| Geopolitics | `geopolitics_engine.py` | 127 LoC | `data_feeds/geopolitics_data.py` (333 LoC) | Keep as paper v2 |
| Tech/AI | `tech_ai_engine.py` | 99 LoC | `data_feeds/tech_data.py` (367 LoC) | Keep as paper v2 |
| Stocks/Commodities | `stocks_commodities_engine.py` | 153 LoC | `data_feeds/stocks_data.py` (235 LoC) | Keep as paper v2 |

All inherit from `src/verticals/base_engine.py` (954 LoC). Pattern: load markets → LLM research → swarm debate → category Kelly multiplier.

### Sports: League Support

| League | Type | Adapter |
|---|---|---|
| NBA | US Basketball | `src/data/adapters/nba_adapter.py` |
| NFL | US Football | `nfl_adapter.py` |
| MLB | US Baseball | `mlb_adapter.py` |
| NHL | US Hockey | `nhl_adapter.py` |
| NCAAB / CFB | College | `ncaab_adapter.py` |
| EPL, Serie A, La Liga, Bundesliga, Ligue 1, MLS, UCL | Soccer | `international_sports_adapter.py` |
| UFC | MMA | international adapter |
| WTA, ATP | Tennis | international adapter |
| PGA | Golf | BLOCKED (`risk_config.json.blocked_sports`) |
| FIBA, KBO, NPB | Basketball/Baseball | international adapter |

**League priorities** (`config/auto_trading_config.json`):

| League | Priority | Allocation |
|---|---|---|
| NBA | 1 | 35% |
| NHL | 2 | 30% |
| NCAAB | 3 | 10% |
| MLB | 4 | 10% |
| NFL | 5 | 5% |
| EPL | 6 | 3%, max_daily_trades 10 |

---

## 8. ML Pipeline (ACTUAL State)

### What Is Actually Training

**SYSTEM.md claimed 4 models (LGB + XGB + CatBoost + LSTM). Audit reality:**

| Model | File | Status in Trainer |
|---|---|---|
| LightGBM | `src/models/lgbm_model.py` | ✅ **Actively trained** |
| XGBoost | `src/models/xgb_model.py` | ✅ **Actively trained** |
| CatBoost | `src/models/catboost_model.py` (200 LoC) | ⚠️ **File exists, `catboost_model.pkl` exists — NOT called by `model_trainer.py`** |
| LSTM | `src/models/lstm_model.py` (490 LoC) | ❌ **Orphaned — not called, deferred to v2** |
| Transformer | `src/models/transformer_model.py` | ❌ **Unused — DELETE** |
| Meta-learner | `src/models/meta_learner.py` | ✅ **Stacking head — keep** |
| Ensemble | `src/models/ensemble.py` | ✅ **Weighted ensemble wrapper** |

**Rebuild decision:** Wire CatBoost into `model_trainer.py::train_ensemble` (confirmed works in one-off runs — `catboost_info/` dir and `catboost_model.pkl` prove it runs). This completes the 3-model ensemble (LGB + XGB + CatBoost). LSTM stays deferred.

### Feature Pipeline

```
698 features (config/production_features.json)
    ↓ parity gate: removes features in training splits but not inference builder
487 post-parity features
    ↓ fake-coverage gate: removes features ≥80% at default value
393 trainable features
    ↓ importance pruning (threshold=0.001)
145 selected (config/feature_contract.json + models/champion_metadata.json)
```

### Training Infrastructure (`src/ml_training/` — 15 modules)

| File | Purpose |
|---|---|
| `model_trainer.py` | Main trainer — walk-forward, fold construction, per-base-learner, stacker |
| `improvement_orchestrator.py` | Nightly directives — risk rules, feature ideas, retrain triggers |
| `postmortem_engine.py` | Per-trade post-mortems (model_edge stored as % points) |
| `trade_analyzer.py` | Trade-level analysis |
| `autoresearch_engine.py` | Bayesian experiment search |
| `hypothesis_generator.py` | Hypothesis generation |
| `online_learner.py` | SGD `partial_fit` after settlement batches |
| `unified_improvement_loop.py` | Unified coordination |
| `drift_watchdog.py` | Feature/model drift detection |
| `schema.py` | DB schema for 15 ML tables |

### Walk-Forward Training

4-window split (from real training log):
```
train=51,839 samples (from 2022-06-29)
select=10,794 samples (from 2023-11-20)
cal=8,250 samples (from 2024-12-15)
test=9,924 samples (no date overlap verified)
```

### Online Learning

SGD `partial_fit` after every 5-game settlement batch. Config (`config/online_learning_config.json`):
- Filters out flat ($0 P&L) trades from training signal
- `partial_fit` updates ensemble weights without full retrain
- Smart vertical inference filters out non-sports settlements

### Per-Sport HPO (Already Done)

- `config/hpo_result_MLB.json`
- `config/hpo_result_NBA.json`
- `config/hpo_result_NHL.json`
- `config/optimized_hyperparams.json` (combined)

### Model Registry & Rollout (`src/model_upgrade/` — 26 modules)

Champion/challenger framework:
1. Challenger trains in shadow for N predictions
2. Compares metrics on holdout vs champion
3. Promotes if challenger beats champion on holdout AND meets per-sport bar
4. Auto-rollbacks via `model_rollback_log` table if post-promotion metrics regress

### Crypto Binary ML

`src/crypto/retrain_pipeline.py` (1,206 lines):
- Walk-forward XGBoost + LightGBM
- Promotion gates: ECE ≤ 0.15, MIN_SHARPE ≥ 0.1, BRIER threshold 0.002
- Progressive promotion logic (relaxed gates after N failed promotions)
- 140k+ labeled training samples from `crypto_training_data` table

---

## 9. Calibration System

### 11 Modules in `src/calibration/`

| File | Lines | Purpose |
|---|---|---|
| `unified_calibrator.py` | 1,212 | Main per-vertical calibrator with `sizing_scale` brake |
| `manager.py` | 787 | Orchestration — which calibrator to use |
| `calibrator.py` | 540 | Base calibrator (Isotonic + Platt) |
| `per_segment_calibrator.py` | 415 | Per-segment splits (sport × market_type × segment) |
| `sport_calibration.py` | 402 | Per-sport calibration |
| `per_model_sport_calibrator.py` | 322 | Per (model, sport) calibrator |
| `calibration_recorder.py` | 222 | Records predictions vs outcomes to `calibration_records` |
| `bakeoff.py` | 211 | Challenger calibrator bakeoff in shadow |
| `drift_detector.py` | 176 | ECE drift detection → triggers recalibration |
| `metrics.py` | 137 | Brier, ECE, reliability |
| `unified_metrics.py` | 131 | Cross-calibrator metrics |
| `calibration_stabilizer.py` | 96 | Smooths sizing_scale to prevent oscillation |

### `sizing_scale` Mechanism

Every vertical has a `VerticalCalibrator` with:
- `_sizing_scale` — multiplier on Kelly fraction (1.0 = full, 0.0 = halted)
- `_current_ece` — recent Expected Calibration Error
- `ece_halt` — ECE above which scale → 0 (all trades blocked)
- `ece_reduce` — ECE above which scale linearly decays to 0.25

| Vertical | `ece_halt` | `ece_reduce` |
|---|---|---|
| Sports | 0.12 | 0.07 |
| Weather | 0.10 | 0.05 |
| Crypto | 0.15 | 0.08 |

### Per-Sport Calibrators (6)

CFB, MLB, NBA, NCAAB, NFL, NHL — each trained separately using Isotonic regression on out-of-fold predictions.

### Kelly Graduation

Three stages in `config/risk_limits.json`:

| Stage | Kelly Fraction | Promotion Requirement |
|---|---|---|
| Quarter | 0.25 | Base (starting) |
| Third | 0.333 | 30 sustained days at ECE < 0.04 |
| Half | 0.50 | 60 sustained days at ECE < 0.03 |

### Drift Detection

`drift_detector.py` continuously compares recent prediction-vs-outcome distribution against calibration set. Triggers automatic recalibration when drift exceeds threshold. Results stored in `feature_drift_alerts` table.

### Interfaces Used in Trading Loop

```python
# Per-sport calibration
prob = sport_calibrator.calibrate(model_prob, sport="NBA", market_type="moneyline")

# Sizing gate check
if unified_calibrator.sizing_scale <= 0:
    # CALIBRATION HALT — skip trade
    
# Drift check
drift_detector.check(recent_predictions, recent_outcomes)
```

---

## 10. Swarm Intelligence + LLM Gatekeeper

### 6-Persona Swarm (`src/trading/swarm_debate.py`, 1,982 lines)

**Sports swarm personas:**
1. `sharp_edge` — Sharp bookmaker lines, market efficiency, Kalshi/Pinnacle divergence
2. `injury_roster` — Injuries, lineup changes, rest, back-to-back, travel fatigue
3. `momentum_form` — Recent form, win/loss streaks, scheduling spots
4. `public_contrarian` — Public betting patterns, fade-the-public, inflated lines
5. `matchup_historian` — Head-to-head, coaching tendencies, style clashes, venue
6. `risk_guardian` — Portfolio risk, correlation, position sizing, downside protection

**Weather swarm personas (different set, `WEATHER_ANALYST_PERSONAS`):**
1. `meteorological_analyst` — NWS/ECMWF/GFS model consensus vs Kalshi price
2. `forecast_uncertainty` — Forecast spread, model disagreement, confidence intervals
3. `station_bias_expert` — Airport METAR vs city center, urban heat island
4. `settlement_timing` — Diurnal temperature cycles, front arrival timing
5. `source_agreement_validator` — Multi-source consensus quality, data freshness
6. `wx_risk_guardian` — Weather bet sizing, city exposure correlation

### Two-Round Debate Protocol

```
Round 1 (single batched LLM call):
  ALL 6 personas evaluate simultaneously (temperature=0.3, max_tokens=3000)
  Each returns: { probability, confidence, recommended_action, key_factors, reasoning, risk_flags }
  
Round 2 (moderator synthesis, separate LLM call):
  Synthesizer sees all 6 Round 1 verdicts + per-analyst track records
  Aggression mode (conservative | moderate | aggressive | max_alpha) injected
  Returns: { consensus_probability, confidence_band, agreement_rate, approved, recommended_action }
```

### Persona Weight Manager (`src/trading/swarm_weight_manager.py`, 659 lines)

- Tracks per-persona Brier score on settled trades
- Bayesian weight updates: recent wrong analyst → lower weight in consensus
- Per-analyst metrics in `swarm_analyst_metrics` table (scoped by `home_away × spread_bucket × injury_context`)
- Benched analysts in `swarm_analyst_benchings` table
- Reinstatement: `POST /api/swarm/reinstate-analyst`

### LLM Gatekeeper (`src/trading/llm_gatekeeper.py`, 260 lines)

Final veto layer **after** swarm approval:
- Receives: swarm output + market state + position context
- Returns: TRADE/PASS verdict + confidence + suggested size + risk tier
- Can adjust size (capped at per-bet cap — e.g. $25 for weather)
- **Bypass policy:** Force-disabled in production. ALL Kalshi-side trades require gatekeeper.

### Multi-Provider Failover

Default provider: **Anthropic** (`claude-sonnet-4-6`, `blend_weight=0.6`, `timeout=45s`)

Supported providers (all configurable in `config/auto_trading_config.json`):
Anthropic, OpenAI, Google Gemini, Meta, xAI, Mistral, DeepSeek, Cohere, Ollama (local)

Encrypted config: `.llm_config_cache.enc` stores provider keys. `llm_config` table holds runtime config.

### Per-Role Model Assignment

| Role | Model Choice |
|---|---|
| Swarm personas (6) | Cheaper/faster (GPT-4o-mini, Claude Haiku) |
| Final gatekeeper | Heavier reasoning (Claude Sonnet, GPT-4o) |
| Concierge | Conversational model |
| AutoResearch | High-context model for hypothesis generation |
| BTC-specific | `OPENAI_BTC_MODEL` env var |

### Swarm Cost Tracking

- `swarm_daily_budget` in `bankroll_config.json` (currently 0 = unlimited)
- Per-debate cost stored in `swarm_debates` table
- `GET /api/swarm/cost-breakdown`

### Debate Cache

`_debate_cache` keyed by `sha1(game_id+market_type+features_hash)`, TTL=900s (15 min).
Repeat opportunities within 15 minutes return cached consensus — no LLM calls.

### Appendix F Reference

Full verbatim swarm prompts preserved in **Appendix F** of this document. These come directly from `src/trading/swarm_debate.py` and must not be altered during rebuild without re-evaluating swarm performance.

---

## 11. Risk Management (12 Gates)

See §6 for gate overview in trading loop context.

### Risk Limits (`config/risk_limits.json`)

```json
{
  "sports": {
    "max_open_positions": 9999,
    "max_exposure": 10000,
    "max_exposure_pct": 0.10,
    "max_single_trade": 50,
    "max_single_trade_pct": 0.05,
    "daily_loss_limit": 60,
    "daily_loss_pct": 0.06,
    "kelly_fraction": 0.25
  },
  "weather": {
    "max_single_trade": 15,
    "max_single_trade_pct": 0.015,
    "daily_loss_limit": 40,
    "daily_loss_pct": 0.04,
    "kelly_fraction": 0.125
  },
  "combined": {
    "max_total_exposure_pct": 0.10,
    "max_daily_loss": 100,
    "max_daily_loss_pct": 0.10,
    "max_drawdown_pct": 0.20,
    "use_pct_limits": true
  },
  "drawdown_tiers": [
    {"drawdown_pct": 0.05, "kelly_scale": 0.75},
    {"drawdown_pct": 0.10, "kelly_scale": 0.50},
    {"drawdown_pct": 0.15, "kelly_scale": 0.25}
  ],
  "correlation_protection": {
    "drawdown_threshold_pct": 0.03,
    "window_seconds": 3600,
    "kelly_multiplier": 0.50,
    "cooldown_seconds": 3600
  },
  "circuit_breakers": {
    "api_error_rate_threshold": 0.10,
    "api_error_window_seconds": 300,
    "api_pause_seconds": 300,
    "latency_skip_threshold_s": 5.0,
    "consecutive_loss_cooloff": 3,
    "cooloff_seconds": 1800
  }
}
```

### Auto-Trading Config (`config/auto_trading_config.json`)

```json
{
  "kelly_fraction": 0.175,
  "buying_power_pct": 100.0,
  "min_ev_pct": 13.5,
  "max_daily_trades": 9999,
  "max_daily_loss_pct": 3.0
}
```

### Env Var Knobs

| Env Var | Purpose |
|---|---|
| `AXIOM_DAILY_LOSS_LIMIT` | Override daily loss limit |
| `AXIOM_KELLY_FRACTION` | Override Kelly fraction |
| `AXIOM_MAX_CONCURRENT_TRADES` | Override max concurrent trades |
| `AXIOM_MAX_DRAWDOWN_PCT` | Override max drawdown |
| `AXIOM_MAX_POSITION_SIZE` | Override max single position |
| `AXIOM_MIN_EDGE` | Override minimum edge floor |

### Kelly System (`src/trading/kelly.py` 448 lines, `kelly_graduation.py` 407 lines)

- Quarter/Third/Half Kelly graduation (sustained-days-based promotion)
- Fee-adjusted (Kalshi taker fees baked into edge calculation)
- Sharpe-aware scaling (`sharpe_allocation.py` 241 lines)
- Drawdown-tier adjustments (3 tiers: 5%/10%/15% drawdown → 0.75/0.50/0.25 Kelly scale)
- Bankroll-percentage caps
- Longshot bias correction (`longshot_bias.py` 120 lines) — adjusts Kelly downward for extreme-probability bets (model_prob ≤ 0.10 or ≥ 0.90)

### Health Gates (`src/trading/health_gates.py`, 274 lines)

Per-trade pre-execution checks:
- `calibration_status` — sizing_scale > 0
- `feature_completeness` ≥ 75%
- `model_divergence` — ensemble members shouldn't diverge too much
- `data_freshness` — odds, lines, weather data within staleness window
- `data_corruption` — LLM-flagged corruption detection
- `price_divergence` — Kalshi implied vs sharp book consensus

### Ruin Monte Carlo (`src/trading/ruin_montecarlo.py`, 241 lines)

Simulates probability of ruin given current bankroll, edge distribution, Kelly fraction. Surfaces as risk-of-ruin % on dashboard. Endpoint: `GET /api/risk-console/monte-carlo`

### Correlation Limits (`_check_correlation_limits`)

- `max_positions_per_league` (sports)
- `max_sports_positions` (total)
- `max_positions_per_city` (weather)
- `max_weather_positions` (total)
- 11 commodity correlation pairs (oil-nat_gas, corn-soybeans-wheat, gold-silver, copper-nickel, coffee-sugar)

### Blocked Sports / Blackout Hours

- Blocked: `risk_config.json.blocked_sports` = PGA, golf, LPGA, pga_tour
- Blackout: `blackout_hours_et`: 23:00 → 11:00 ET

---

## 12. Execution Layer

### Trading Module Summary (`src/trading/` — 43 files, 22,129 LOC)

| File | Lines | Purpose |
|---|---|---|
| `kalshi_client.py` | 960 | Kalshi REST + WS, RSA-PSS auth |
| `execution_engine.py` | 1,268 | Order submission orchestrator |
| `order_manager.py` | 536 | Order lifecycle (submitted → resting → filled → settled) |
| `smart_limit.py` | 493 | Smart limit pricing |
| `fill_probability.py` | 352 | Fill probability inference |
| `fill_model_trainer.py` | 941 | Train fill probability model |
| `settlement_feedback.py` | 911 | Settlement → online learner pipeline |
| `position_tracker.py` | 542 | Position state tracking |
| `position_dedup.py` | 127 | Dedup gate |
| `paper_trader.py` | 528 | Paper trading engine |
| `paper_trading_runner.py` | 571 | Paper trading runner |
| `polymarket_client.py` | 1,093 | Polymarket REST + WS |
| `polymarket_order_manager.py` | 457 | Polymarket order lifecycle |
| `robinhood_client.py` | 633 | Robinhood REST |
| `order_simulator.py` | 306 | Order simulation |

### Kalshi Client (`kalshi_client.py`)

**Auth:** RSA-PSS private key (`kalshi_private_key.pem` or `KALSHI_API_PRIVATE_KEY` env as PEM string)

Key env vars:
```bash
KALSHI_API_KEY_ID        # Account key ID
KALSHI_API_PRIVATE_KEY   # RSA private key (PEM string)
KALSHI_PRIVATE_KEY_PATH  # Alternative: path to PEM file
KALSHI_ENV               # demo | prod
```

Dollar-era field handling (post-API migration):
- Uses `yes_bid_dollars`, `no_price_dollars`, `last_price_dollars`, `volume_fp`, `open_interest_fp`
- Cent-field fallback for backwards compat
- Positions cached at 30s TTL, balance at 30s TTL

**Important:** `trades.price` is in **dollars** (0.0–1.0), `positions.avg_price` is in **cents** (94.0000 = $0.94). These must not be confused.

### Order Lifecycle

```
submitted → resting (GTC limit) → filled → settled → feedback loop
                    ↓ cancel if unneeded
                partial_fill → reconciled position
```

Stored in `order_lifecycle` table. Dedup gate checks for open GTC orders before submitting new position in same market.

### Smart Limit Pricing (`smart_limit.py`)

Expected utility optimizer:
```
U = P(fill) × edge - opportunity_cost - adverse_selection_penalty
```

Adverse selection penalty (`config/adverse_selection_params.json`):
- Base: 0.3× edge
- Time-to-close multipliers: 2.5× (closing), 1.5× (mid), 1.0× (standard), 0.7× (far)
- Price-extreme multipliers: 1.3× (extreme), 1.1× (near-extreme)
- Volume multipliers: 1.3× (low), 0.8× (high)

### Settlement Feedback (`settlement_feedback.py`, 911 lines)

On market settlement:
1. Calculate realized P&L
2. Update `positions.realized_pnl`
3. `_backfill_trade_pnl_from_settlements` — per-fill attribution **(BUGGY — see Appendix H)**
4. Feed outcome to online learner (SGD `partial_fit`)
5. Update calibrator
6. Update swarm persona weights
7. Record CLV (closing line value)
8. Insert into `trade_postmortems`
9. Update `vertical_daily_pnl`

### CLV Tracking (3-Component Decomposition)

- `model_clv` — was the model edge real?
- `execution_clv` — did we get a good price?
- `timing_clv` — did we enter at the right time?

**Note:** CLV auto-halt is suppressed per user policy (`[AGENT-FIX]`).

### Polymarket Client

Currently surveillance-only. Execution path exists but inactive. `uw-mcp/` handles data stream.

### Robinhood Client

Paper mode only (0% WR, -$348 P&L on 130 trades historically). Swing-trading config: minimum 4h hold, higher edge requirement, larger scan intervals.

---

## 13. Self-Healing & AutoResearch

### Three-Layer Process Supervision

| Layer | File | Trigger | Recovery |
|---|---|---|---|
| L1 — In-process | `start_backend.py` | uvicorn child SIGCHLD | Respawn child within 5s |
| L2 — External | `scripts/backend_watchdog.py` | 30s port-down | Detached subprocess spawn |
| L3 — Workflow runner | Replit `.replit` | Watchdog crash | `autoStart=true` restart |

Watchdog HTTP surface (port 8099):
- `GET /status` — overall health
- `GET /spawn_count` — respawn count
- `GET /last_spawn` — last respawn cause + timestamp

### Daily Audit Watchdog (`src/watchdog/daily_audit.py`)

16 autonomous checks:
1. Vertical mislabeling
2. Calibration drift per vertical
3. Feature coverage gaps (rising fake_coverage %)
4. `model_prob NULL` rate spike
5. Stale positions (open beyond market close)
6. Duplicate trades within dedup window
7. AutoResearch stagnation (no improvements for N days)
8. Weather source health (Open-Meteo / NWS / HRRR / AccuWeather)
9. Edge distribution anomalies
10. Kelly sizing extremes
11. Online learning backlog (unprocessed settlements)
12. Settlement pipeline health
13. Pipeline compliance (Kalshi API conformance)
14. Win rate anomalies (per-vertical, per-sport)
15. Config consistency
16. P&L data integrity

Auto-fix capabilities:
- Auto-pause vertical with sudden win-rate crash
- Backfill missing `model_prob`
- Trigger recalibration
- Email detailed report (Postmark)

Tables: `watchdog_audit_log`, `watchdog_market_cooldowns`

### AutoResearch (`src/ml_training/autoresearch_engine.py`)

Config (`config/autoresearch_config.json`):
- `experiment_interval_hours`: 6
- `min_settled_trades`: 50
- `exploration_rate`: 0.3
- `auto_promote`: true
- `shadow_validation_enabled`: true
- `shadow_min_trades`: 15
- `shadow_brier_improvement_pct`: 3.0
- `bayesian_significance`: true, `bayesian_credible_threshold`: 0.7

Fitness: three-axis combination (Brier score + win rate + ROI)

Tables:
- `autoresearch_experiments` — every experiment
- `autoresearch_config` — runtime config
- `improvement_velocity` — improvement rate tracking
- `research_journal` — research notes
- `shadow_experiments` — shadow runs
- `shadow_picks` — shadow picks
- `shadow_postmortems` — post-mortems
- `retrain_history` — retrain history
- `model_health_alerts`
- `model_metrics`
- `model_rollback_log`

### Online Learner (`src/ml_training/online_learner.py`)

SGD `partial_fit` after every 5-game settlement batch. Updates ensemble weights without full retrain. Filters out flat ($0 P&L) trades. Smart vertical inference filters out non-sports settlements.

### Improvement Orchestrator (`src/ml_training/improvement_orchestrator.py`)

Runs every 4h. Generates ranked directives:
- Risk rule changes
- Feature engineering ideas
- Retraining triggers
- Config tuning suggestions

Alert thresholds (`config/unified_improvement_config.json`):

| Metric | Threshold |
|---|---|
| `brier_score_max` | 0.35 |
| `win_rate_min` | 35% |
| `roi_min` | -15% |
| `roi_7d_min` | -$500 |
| `weather_min_samples_for_degraded` | 30 |

---

## 14. Frontend — React + Vite

### Stack

| Dependency | Version | Purpose |
|---|---|---|
| React | 18.2.0 | UI framework |
| Vite | 7.3.1 | Build tool + dev server |
| TypeScript | 5.3.2 | Type safety |
| Tailwind CSS | 3.3.6 | Utility-first styling |
| TanStack Query | 5 | Data fetching + caching |
| react-router-dom | 6.20.0 | Client-side routing |
| Recharts | 2.10.0 | Charts + visualization |
| axios | 1.13.6 | HTTP client |
| lucide-react | 0.577.0 | Icons |
| react-markdown | 10.1.0 | Markdown rendering |
| Express + http-proxy-middleware | 4.22.1 / 3.0.5 | Node proxy server |
| pg + bcryptjs | 8.20.0 / 3.0.3 | **REBUILD: Remove — use FastAPI auth** |

### Design System

- **Aesthetic:** Futuristic dark trading terminal, glassmorphism, cyan/purple accents
- **Fonts:** Inter (UI), JetBrains Mono (numbers/code)
- **Background:** Subtle grid
- **Charts:** Recharts (streaming via WebSocket)
- **Special component:** `AxiomGravity` — orbital visualization of live Kalshi positions (gravity-based layout)

### Auth Decision for Rebuild

**Current state:** Dual auth surfaces — FastAPI `auth.py` router + Node `frontend/server.js` with `pg`/`bcryptjs`.

**Rebuild decision:** Consolidate to **FastAPI JWT auth only**.
- Remove `pg`, `bcryptjs`, `cookie-parser` from `frontend/package.json`
- `frontend/server.js` becomes pure proxy — no user lookup, no session management
- Frontend `AuthContext` reads from `GET /api/auth/me` (FastAPI)

### 123 Pages — See Appendix B for Complete Inventory

Key pages by functional group:

| Page | Lines | Role |
|---|---|---|
| `Positions/PositionsPage.tsx` | 1,987 | Primary trading view — per-position with LLM context |
| `BankrollTrackerPage.tsx` | 1,882 | Bankroll history, drawdown, Sharpe |
| `AutoResearchPage.tsx` | 1,627 | AutoResearch experiments |
| `Weather/WeatherPage.tsx` | 1,497 | Weather contracts, model probs, source health |
| `SettingsPage.tsx` | 1,485 | Master settings, LLM config |
| `AIHubPage.tsx` | 1,434 | LLM provider config, swarm status |
| `Trading.tsx` | 1,364 | Main trading dashboard |
| `RiskControlsPage.tsx` | 1,095 | Risk parameter command center |
| `PipelineMonitorPage.tsx` | 1,020 | Live cycle-by-cycle audit |
| `Overview/` (20 components) | — | Mission Control dashboard widgets |

### Custom Hooks (22)

`useAxiomEdge`, `useBankroll`, `useCryptoBinary`, `useCryptoMarketData`, `useCryptoPredictions`, `useDebounce`, `useHealthData`, `useKalshiSportsAnalytics`, `useLLM`, `useMarketData`, `useNotifications`, `useRealtimePnL`, `useRealtimeResilience`, `useReconnectingWebSocket`, `useSettings`, `useSportsAnalytics`, `useSportsRecommendations`, `useTemperatureStream`, `useWeatherAnalytics`, `useWeatherData`, `useWeatherPnL`, `useWebSocket`

### Contexts

- `AuthContext.tsx` — auth state (to be backed by FastAPI auth in rebuild)
- `WebSocketContext.tsx` — WebSocket connection lifecycle

### Vite Config

```typescript
// vite.config.ts key settings:
// proxy /api/* → backend (dev mode)
// build output: dist/
// React plugin: @vitejs/plugin-react
// Testing: vitest
```

### 28 Component Groups

| Group | Key Components |
|---|---|
| `Trading/` (16) | ActivePositions, LiveTradingDashboard, PipelineMonitor, OrderBook, TradeModal |
| `Settings/` (15) | AIModelSettings, AutoTradingSettings, APIKeyWizard |
| `Crypto/` (11) | ArbitragePanel, MLTrainingCard, PerformanceMetrics |
| `Weather/` (8) | Weather widgets |
| `ui/` (8) | Button, Card, Input, Modal (shadcn-style primitives) |
| `Navigation/` (5) | Sidebar, Topbar, Mobile menu, Breadcrumb, NavItem |
| `GoLive/` (4) | Paper-to-live transition wizard |
| `AxiomGravity/` (1) | **Orbital position visualization** |

---

## 15. Mobile App Expo

### Current State: Internal Read-Only Dashboard

**5 tab screens only.** Not production-ready for iOS App Store.

| Tab | File | Content |
|---|---|---|
| Dashboard | `index.tsx` | Bankroll, today's P&L, open positions |
| Positions | `positions.tsx` | Positions list + pull-to-refresh |
| Pipeline | `pipeline.tsx` | Live cycle events |
| Research | `research.tsx` | AutoResearch experiments |
| Settings | `settings.tsx` | Config editor |

### Stack

- Expo 52, React Native 0.76.7, expo-router 4
- TanStack Query 5, @react-navigation/bottom-tabs 7
- Inter + JetBrains Mono (Google Fonts)
- expo-haptics, expo-font, expo-splash-screen

### Rebuild Decision

**Defer full iOS build to v2.** Keep as internal dev dashboard accessible via `expo start`.

Missing from production:
- `eas.json` — not present
- App Store signing assets — not present
- Push notification setup — `expo-notifications` not in deps
- Deep-linked routes beyond tab root
- Native modules

---

## 16. UW MCP Service

### Status: Fully Implemented — No Rebuild Changes Needed

`uw-mcp/` — separate Node.js + TypeScript service. Confirmed working.

### Stack

| Package | Version | Purpose |
|---|---|---|
| hono | 4.7.10 | HTTP framework |
| @hono/node-server | 1.14.1 | Node adapter |
| @modelcontextprotocol/sdk | 1.12.1 | MCP client |
| node-cron | 4.2.1 | Pipeline scheduler |
| pg | 8.20.0 | Direct DB access |
| zod | 3.24.4 | Validation |
| pino | 9.6.0 | Structured logging |
| tsx + vitest | — | Dev + test |

### Structure

```
uw-mcp/src/
├── index.ts                    # Entry point
├── api/
│   ├── routes.ts               # General routes
│   ├── bridge_routes.ts        # Bridge to Python backend
│   └── pipeline_routes.ts      # Pipeline routes
├── bridge/
│   └── axiom_bridge.ts         # HTTP bridge to Axiom FastAPI backend
├── db/
│   ├── schema.ts               # Drizzle ORM schema
│   └── store.ts                # DB store
├── mcp/
│   ├── client.ts               # MCP client
│   ├── registry.ts             # Tool registry
│   └── extractor.ts            # Data extractor
└── pipelines/
    ├── base.ts                 # Pipeline base class
    ├── signals.ts              # Signal pipeline
    ├── signal_scan.ts          # Scan for signals
    ├── morning_briefing.ts     # Morning briefing
    ├── congress_monitor.ts     # Congressional trading alerts
    ├── earnings_scanner.ts     # Earnings scanner
    ├── deep_dive.ts            # Deep dive analysis
    └── scheduler.ts            # node-cron driver
```

### 7 Pipelines + Cron Schedule

| Pipeline | Purpose |
|---|---|
| `congress_monitor` | Congressional trading activity alerts |
| `darkpool` | Dark pool order flow |
| `earnings_scanner` | Earnings surprise detection |
| `morning_briefing` | Daily morning market brief |
| `signal_scan` | General signal scanning |
| `signals` | Signal aggregation |
| `deep_dive` | Deep dive on specific tickers/markets |

Cron schedule in `pipelines/scheduler.ts` — VERIFY intervals against `UW_POLL_*_INTERVAL_SECONDS` env vars.

### Python Integration

- `src/api/routers/uw_mcp.py` — proxy endpoints (`GET /api/uw-mcp/status`, `POST /api/uw-mcp/query`)
- `src/api/routers/uw_predictions.py` — surveillance data (`GET /api/uw/predictions`, `GET /api/uw/signals`)
- `src/data/uw_mcp_bridge.py` — bridge helper

### Key Env Vars

```bash
UNUSUAL_WHALES_API_KEY
UW_MCP_SERVICE_URL
UW_MCP_TIMEOUT
UW_MCP_CB_THRESHOLD
UW_MCP_CB_COOLDOWN
UW_POLL_CONGRESS_INTERVAL_SECONDS
UW_POLL_DARKPOOL_INTERVAL_SECONDS
# ... (11 poll interval vars total)
```

---

# PART III — BUILD MANIFEST

---

## 17. Python Dependencies (Exact Versions)

### `requirements.txt`

```
fastapi==0.115.0
starlette==0.38.6
uvicorn==0.35.0
pydantic==2.12.5
numpy==2.0.2
pandas==2.2.3
scipy==1.13.1
httpx==0.28.1
aiohttp>=3.9.0,<4.0.0
scikit-learn>=1.3.0
jinja2==3.1.6
pytest==8.4.2
pytest-asyncio>=0.21.0,<1.0.0
pytest-benchmark==5.1.0
websockets==12.0
psycopg2-binary
cryptography
xgboost>=2.0.0
lightgbm>=4.0.0
catboost>=1.2.0
imbalanced-learn>=0.12.0
stripe>=14.0.0
hmmlearn==0.3.3
```

**Python version:** 3.11 (per `Dockerfile FROM python:3.11-slim`)

### `requirements-evaluation.txt`

VERIFY — additional evaluation dependencies (SHAP, matplotlib, seaborn likely). Read file during rebuild.

### Key System Dependencies (from `replit.nix`)

gcc, g++, curl (for psycopg2-binary and cryptography builds)

---

## 18. Node Dependencies

### Frontend (`frontend/package.json`)

```json
{
  "dependencies": {
    "react": "18.2.0",
    "react-dom": "18.2.0",
    "react-router-dom": "6.20.0",
    "@tanstack/react-query": "^5",
    "recharts": "2.10.0",
    "axios": "1.13.6",
    "lucide-react": "0.577.0",
    "react-markdown": "10.1.0",
    "express": "4.22.1",
    "http-proxy-middleware": "3.0.5",
    "pg": "8.20.0",
    "bcryptjs": "3.0.3",
    "cookie-parser": "1.4.7"
  },
  "devDependencies": {
    "vite": "7.3.1",
    "@vitejs/plugin-react": "5.1.4",
    "typescript": "5.3.2",
    "tailwindcss": "3.3.6",
    "@playwright/test": "1.40.0",
    "vitest": "4.0.18"
  }
}
```

**Rebuild note:** After consolidating to FastAPI auth, remove `pg`, `bcryptjs`, `cookie-parser` from frontend dependencies.

**Node version:** 18+ (Vite 7 requirement)

### UW MCP (`uw-mcp/package.json`)

```json
{
  "dependencies": {
    "hono": "4.7.10",
    "@hono/node-server": "1.14.1",
    "@modelcontextprotocol/sdk": "1.12.1",
    "node-cron": "4.2.1",
    "pg": "8.20.0",
    "zod": "3.24.4",
    "pino": "9.6.0",
    "dotenv": "16.5.0"
  }
}
```

### Mobile (`mobile/package.json`)

```json
{
  "dependencies": {
    "expo": "~52.0.0",
    "expo-router": "~4.0.0",
    "react-native": "0.76.7",
    "react": "18.3.1",
    "@tanstack/react-query": "^5",
    "@react-navigation/bottom-tabs": "^7",
    "@react-navigation/native": "^7",
    "react-native-reanimated": "~3.16.0",
    "react-native-gesture-handler": "~2.20.0",
    "react-native-screens": "~4.4.0",
    "@expo-google-fonts/inter": "*",
    "@expo-google-fonts/jetbrains-mono": "*"
  }
}
```

---

## 19. Environment Variables (.env.example)

All 135 variables. Grouped by domain. Defaults shown where safe to share.

```bash
# ============================================================
# CORE
# ============================================================
DATABASE_URL=                          # Postgres connection URL (required)
PORT=8000                              # Supervisor TCP proxy port
BACKEND_PORT=8001                      # uvicorn port
SESSION_SECRET=                        # Required for auth sessions
LOG_LEVEL=INFO                         # DEBUG | INFO | WARNING | ERROR
CORS_ORIGINS=http://localhost:3000     # Comma-separated allowed origins
GIT_SHA=                               # Optional: for version tracking
AXIOM_ENV=development                  # development | production
AXIOM_SAFE_BOOT=false                  # If true, skip trading loop init
AXIOM_TRADING_MODE=paper               # live | paper
AXIOM_PAPER_MODE=true                  # true | false (overrides TRADING_MODE)

# ============================================================
# TRADING KNOBS
# ============================================================
AXIOM_DAILY_LOSS_LIMIT=100             # Max daily loss in dollars
AXIOM_KELLY_FRACTION=0.175             # Base Kelly fraction
AXIOM_MAX_CONCURRENT_TRADES=50        # Max open positions
AXIOM_MAX_DRAWDOWN_PCT=0.20           # Max drawdown before halt
AXIOM_MAX_POSITION_SIZE=50            # Max single trade in dollars
AXIOM_MIN_EDGE=0.05                    # Min edge (5%)

# ============================================================
# EXCHANGES
# ============================================================
KALSHI_API_KEY_ID=                     # Kalshi API key ID
KALSHI_API_PRIVATE_KEY=                # RSA private key (PEM string)
KALSHI_PRIVATE_KEY_PATH=               # Alternative: path to PEM file
KALSHI_ENV=demo                        # demo | prod

POLYMARKET_API_KEY=                    # Polymarket API key
POLYMARKET_SECRET=                     # Polymarket secret
POLYMARKET_PASSPHRASE=                 # Polymarket passphrase
POLYMARKET_PRIVATE_KEY=                # Polymarket private key
POLYMARKET_WALLET=                     # Polymarket wallet address

ROBINHOOD_API_KEY=                     # Robinhood API key
ROBINHOOD_PRIVATE_KEY=                 # Robinhood private key

# ============================================================
# DATA FEEDS
# ============================================================
ODDS_API_KEY=                          # The Odds API v4
THEODDSAPI_KEY=                        # Alternative odds API key

SPORTSRADAR_MLB_API=                   # SportsRadar MLB
# VERIFY: may need per-sport vars:
# SPORTSRADAR_NBA_API, SPORTSRADAR_NFL_API, SPORTSRADAR_NHL_API, etc.

BETTINGPROS_API_KEY=                   # BettingPros API key
BETTINGPROS_EMAIL=                     # BettingPros email
BETTINGPROS_PASSWORD=                  # BettingPros password
BETTINGPROS_API=                       # BettingPros base API URL

SPORTSLINE_EMAIL=                      # SportsLine email
SPORTSLINE_PASSWORD=                   # SportsLine password

OUTLIERBET_EMAIL=                      # Outlier.bet email
OUTLIERBET_PASSWORD=                   # Outlier.bet password

METEOMATICS_USER=                      # Meteomatics username
METEOMATICS_PASS=                      # Meteomatics password
TOMORROWIO_API=                        # Tomorrow.io API key
ACCU_API=                              # AccuWeather API key

COINGECKO_API_KEY=                     # CoinGecko Pro API key
CRYPTOPANIC_API_TOKEN=                 # CryptoPanic token

TWITTER_BEARER_TOKEN=                  # Twitter/X Bearer Token

SERP_API_KEY=                          # SerpAPI key (research)
TAVILY_API_KEY=                        # Tavily API key (research)

# ============================================================
# LLM PROVIDERS
# ============================================================
ANTHROPIC_API_KEY=                     # Claude Sonnet (swarm + gatekeeper)
OPENAI_API_KEY=                        # GPT-4o (swarm alternate)
OPENAI_BTC_MODEL=gpt-4o                # Model for BTC-specific analysis
# NOTE: There is a typo in existing code: OPENAI_APY_KEY — this is a bug.
# Do NOT add OPENAI_APY_KEY. Use only OPENAI_API_KEY.
OLLAMA_URL=http://localhost:11434      # Local Ollama instance

# ============================================================
# EMAIL / NOTIFICATIONS
# ============================================================
GMAIL_EMAIL=                           # Gmail address (SMTP backup)
GMAIL_APP_PASSWORD=                    # Gmail app password
POSTMARK_SERVER_API_KEY=               # Postmark transactional email
TELEGRAM_BOT_TOKEN=                    # Telegram bot token
ADMIN_EMAIL=                           # Initial admin email
ADMIN_INITIAL_PASSWORD=                # Initial admin password

# ============================================================
# BILLING
# ============================================================
STRIPE_SECRET_KEY=                     # Stripe secret key
STRIPE_PUBLISHABLE_KEY=                # Stripe publishable key
STRIPE_WEBHOOK_SECRET=                 # Stripe webhook signing secret

# ============================================================
# UW MCP SERVICE
# ============================================================
UNUSUAL_WHALES_API_KEY=                # Unusual Whales API key
UW_MCP_SERVICE_URL=http://localhost:3001  # UW MCP service URL
UW_MCP_TIMEOUT=30000                   # ms timeout
UW_MCP_CB_THRESHOLD=5                  # Circuit breaker threshold
UW_MCP_CB_COOLDOWN=60000               # Circuit breaker cooldown (ms)
UW_CIRCUIT_BREAKER_COOLDOWN_SECONDS=60
UW_CIRCUIT_BREAKER_THRESHOLD=5
UW_POLL_CONGRESS_INTERVAL_SECONDS=300
UW_POLL_DARKPOOL_INTERVAL_SECONDS=60
UW_POLL_FINANCIALS_INTERVAL_SECONDS=3600
UW_POLL_FLOW_INTERVAL_SECONDS=30
UW_POLL_GEX_INTERVAL_SECONDS=300
UW_POLL_INSIDER_INTERVAL_SECONDS=600
UW_POLL_IV_INTERVAL_SECONDS=120
UW_POLL_OPTIONS_VOL_INTERVAL_SECONDS=120
UW_POLL_PREMIUM_INTERVAL_SECONDS=60
UW_POLL_SCREENER_INTERVAL_SECONDS=300
UW_POLL_TECHNICALS_INTERVAL_SECONDS=600
UW_POLL_TIDE_INTERVAL_SECONDS=300

# ============================================================
# MICROSERVICE URLS (for multi-service deployment — Option B)
# ============================================================
DATA_PIPELINE_URL=http://localhost:8001
DATA_PIPELINE_HOST=localhost
DATA_PIPELINE_PORT=8001
FEATURE_API_URL=http://localhost:8003
FEATURE_ENGINE_URL=http://localhost:8003
MODEL_API_URL=http://localhost:8004
MODEL_UPGRADE_URL=http://localhost:8004
PREDICTION_API_URL=http://localhost:8004
EVALUATION_URL=http://localhost:8005
EVALUATION_PORT=8005
EXEC_ENGINE_URL=http://localhost:8006
REALTIME_URL=http://localhost:8007
REAL_TIME_URL=http://localhost:8007
LIVE_MONITOR_URL=http://localhost:8007
WEATHER_DATA_URL=http://localhost:8011
WEATHER_MODEL_URL=http://localhost:8012
WEATHER_TRADER_URL=http://localhost:8013
UNIFIED_API_URL=http://localhost:8000
FRONTEND_URL=http://localhost:3000
MAC_STUDIO_BACKEND_URL=                # URL of Mac Studio backend (for Replit proxy)

# ============================================================
# EVAL THRESHOLDS
# ============================================================
EVAL_BRIER_THRESHOLD=0.25
EVAL_CLV_THRESHOLD_CENTS=2
EVAL_ECE_THRESHOLD=0.05
EVAL_GRADUATION_CONSECUTIVE_DAYS=30
EVAL_WIN_RATE_THRESHOLD=0.50

# ============================================================
# TIMERS
# ============================================================
CHECKPOINT_INTERVAL_SEC=300
GAP_CHECK_INTERVAL_SEC=60
LOOP_INTERVAL_SEC=120
MONITOR_CHECK_INTERVAL_SEC=30
SILENCE_THRESHOLD_SEC=600
ODDS_STALENESS_SEC=300
LIVE_INTANGIBLE_POLL_INTERVAL=60
PAPER_LOOP_INTERVAL_SEC=120
SHUTDOWN_TIMEOUT=30

# ============================================================
# MISC
# ============================================================
MAX_TRAIN_SAMPLES=100000
LLM_APPROVAL_TIMEOUT=45
API_RATE_LIMIT_PER_MINUTE=100
ORCHESTRATOR_ADMIN_KEY=               # Admin key for orchestrator
AUTH_BYPASS=false                      # Dev only: bypass auth
PIPELINE_DEMO_EVENTS=false            # Generate demo pipeline events
PAPER_DATA_DIR=data/paper_trading
STATE_FILE=data/state.json
SERVICE_NAME=                          # For service_launcher.py dispatch
SERVICE_PORT=                          # For service_launcher.py dispatch
AXIOM_SERVICE=                         # Which service to start (data_pipeline, etc.)
AXIOM_PORT=                            # Service-specific port
REPLIT_DEV_DOMAIN=                     # Auto-set by Replit
REPLIT_DOMAINS=                        # Auto-set by Replit
REPL_ID=                               # Auto-set by Replit
REPL_SLUG=                             # Auto-set by Replit
```

---

## 20. Database Schema (Single Source of Truth)

**CRITICAL GAP FROM AUDIT:** Zero SQL migration files exist in the current repo. Schemas are created at runtime via `CREATE TABLE IF NOT EXISTS` statements embedded across 33+ Python files.

**Rebuild requirement:** This section provides the canonical schema to extract into `migrations/0001_initial.sql`.

**Actual table count:** ~80 distinct tables (SYSTEM.md claimed 110 — inflated by ~30). Rebuild with the actual ~80 + additional tables found in `services/shared/` and `monitoring/`.

The full column-level schema is in **Appendix E**. Domain groupings:

### Domain: Auth

| Table | Purpose |
|---|---|
| `users` | User accounts |
| `sessions` | Active sessions (FK to users) |
| `webauthn_credentials` | WebAuthn passkeys |

### Domain: Trading Core

| Table | Purpose |
|---|---|
| `trades` | All executed trades |
| `positions` | Open/closed positions |
| `order_lifecycle` | Order state machine |
| `order_model_prob_cache` | Cached model_prob per order |
| `predictions` | Stored predictions |
| `trade_signals` | Generated signals |
| `closing_prices` | Closing prices for CLV |
| `post_fill_movements` | Post-fill price movements |
| `trade_postmortems` | Per-trade post-mortems |
| `trade_analyses` | Trade-level analysis |
| `trade_feature_vectors` | Feature vectors per trade |
| `trade_resolution_feedback` | Full resolution info |
| `trading_cycles` | Per-cycle stats |
| `trading_mode_log` | Mode change log |

**Key schema: `trades` table**

```sql
CREATE TABLE trades (
    id              SERIAL PRIMARY KEY,
    trade_id        VARCHAR(255) UNIQUE,
    market_id       VARCHAR(255) NOT NULL,
    vertical        VARCHAR(50)  DEFAULT 'sports',
    side            VARCHAR(30)  NOT NULL,        -- yes/no/LIVE_YES/LIVE_NO
    action          VARCHAR(30)  NOT NULL,        -- buy/sell
    quantity        NUMERIC      NOT NULL,
    price           NUMERIC(10,4) NOT NULL,       -- DOLLARS (0.0-1.0)
    fees            NUMERIC(10,4) DEFAULT 0,
    pnl             NUMERIC(10,2),                -- nullable until settlement
    signal_strength NUMERIC(10,4),
    model_edge      NUMERIC(10,4),                -- percentage points (8.0 = 8%)
    kelly_fraction  NUMERIC(10,4),
    trading_mode    VARCHAR(20)  DEFAULT 'paper',
    executed_at     TIMESTAMP    DEFAULT NOW(),
    metadata        JSONB        DEFAULT '{}',
    user_id         INT REFERENCES users(id),
    exchange        VARCHAR(32)  DEFAULT 'kalshi',
    league          VARCHAR(10),
    model_clv       REAL,
    execution_clv   REAL,
    timing_clv      REAL,
    edge            DOUBLE PRECISION,
    clv             DOUBLE PRECISION,
    model_prob      DOUBLE PRECISION,
    settled_at      TIMESTAMPTZ
);
CREATE INDEX idx_trades_executed_at ON trades(executed_at);
CREATE INDEX idx_trades_market_id ON trades(market_id);
-- Dedup gate (excludes crypto):
CREATE UNIQUE INDEX idx_trades_unique_daily
  ON trades(market_id, side, date(executed_at))
  WHERE vertical IS DISTINCT FROM 'crypto';
```

**Important:** `trades.price` in DOLLARS (0.0–1.0), `positions.avg_price` in CENTS (94.0000 = $0.94).

### Domain: Exchange Credentials

| Table | Purpose |
|---|---|
| `kalshi_credentials` | Kalshi API credentials per user |
| `polymarket_credentials` | Polymarket credentials per user |
| `robinhood_credentials` | Robinhood credentials per user |

### Domain: Sports

| Table | Purpose |
|---|---|
| `elo_ratings` | Per-team Elo ratings |
| `elo_processed_games` | Processed games (dedup) |
| `elo_season_resets` | Season reset history |
| `line_snapshots` | Line snapshots over time |
| `public_betting_pcts` | Public betting percentages |
| `fade_public_log` | Public-fade tracking |
| `intangibles_reports` | Intangibles per game |

### Domain: Crypto (11 tables)

`crypto_binary_trades`, `crypto_binary_decisions`, `crypto_binary_training_logs`, `crypto_training_data`, `crypto_features`, `crypto_predictions`, `crypto_prices`, `crypto_ohlcv`, `crypto_orderbook_snapshots`, `crypto_lob_snapshots`, `crypto_daily_summaries`

### Domain: Weather (~20 tables)

`weather_observations`, `weather_forecasts`, `weather_extended`, `weather_minutecast`, `weather_hrrr_forecasts`, `weather_features`, `weather_climatology`, `weather_conditional_clim`, `weather_ensemble`, `weather_ml_model`, `weather_emos_params`, `weather_pit_histogram`, `weather_crps`, `weather_regime_verification`, `weather_settlement_surprise`, `weather_station_bias`, `weather_trade_results`, `weather_trajectory`, `weather_verification`, `weather_calibration`, `weather_calibration_seasonal`, `weather_bucket_calibration`

### Domain: Calibration

`calibration_snapshots`, `calibration_tracking`

### Domain: Swarm & LLM

`swarm_debates`, `swarm_analyst_metrics`, `swarm_analyst_benchings`, `llm_predictions`, `llm_config`

### Domain: AutoResearch & Self-Improvement

`autoresearch_experiments`, `autoresearch_config`, `research_journal`, `shadow_experiments`, `shadow_picks`, `shadow_postmortems`, `retrain_history`, `ml_training_logs`, `model_health_alerts`, `model_metrics`, `model_rollback_log`, `improvement_velocity`

### Domain: Bankroll & Portfolio

`bankroll_events`, `portfolio_snapshots`, `vertical_daily_pnl`, `vertical_data_cache`, `ev_source_attribution`

### Domain: Online Learning & Feedback

`online_learning_buffer`, `mid_phase_decomposition`, `mid_phase_trade_decomposition`, `outcome_intelligence_agg`

### Domain: Notifications & Reporting

`notifications`, `notification_schedules`, `daily_summary_reports`, `email_report_log`, `email_send_log`, `unified_daily_summaries`

### Domain: System & Observability

`system_settings`, `system_incidents`, `concierge_conversations`, `watchdog_audit_log`, `watchdog_market_cooldowns`

### Domain: UW MCP / Polymarket

`uw_dark_pool`, `uw_flow_alerts`, `uw_market_snapshots`, `uw_mcp_pipeline_runs`, `uw_mcp_signals`, `uw_mcp_tool_results`, `uw_prediction_market_snapshots`, `uw_prediction_signals`, `uw_prediction_traders`

### Domain: Misc

`team_stats_cache`, `trades_junk_archive`, `stripe_price_map`, `feature_drift_alerts`, `feature_snapshots`, `snapshots`, `game_features`, `games`, `pnl_history`, `pnl_records`, `pnl_state`, `pnl_trades`

---

## 21. External APIs Required

### Minimum Viable (Paper Mode)

```bash
DATABASE_URL              # Postgres
ODDS_API_KEY              # Sports odds feed
SESSION_SECRET            # Auth
ADMIN_EMAIL               # Initial admin
ADMIN_INITIAL_PASSWORD    # Initial admin password
AXIOM_PAPER_MODE=true     # Paper mode
```

### Full Live Trading

In addition to paper mode minimum:
- `KALSHI_API_KEY_ID` + `KALSHI_API_PRIVATE_KEY` (RSA PEM) — live order placement
- `SPORTSRADAR_*_API` per sport — real-time game data
- `ANTHROPIC_API_KEY` + `OPENAI_API_KEY` — swarm analysts + gatekeeper
- `UNUSUAL_WHALES_API_KEY` — UW MCP service
- `METEOMATICS_USER`/`PASS` + `TOMORROWIO_API` + `ACCU_API` — weather vertical
- `COINGECKO_API_KEY`, `CRYPTOPANIC_API_TOKEN` — crypto vertical
- `STRIPE_*` — subscription billing
- `POSTMARK_SERVER_API_KEY` — transactional email
- `TELEGRAM_BOT_TOKEN` — Concierge AI alerts

### Optional (Enhances Signals)

- `OUTLIERBET_EMAIL`/`PASSWORD` — OUTLIER EV scanner
- `BETTINGPROS_*` — player prop lines
- `SPORTSLINE_*` — expert picks + prop lines
- `TWITTER_BEARER_TOKEN` — sentiment
- `SERP_API_KEY`, `TAVILY_API_KEY` — research pipelines
- `POLYMARKET_*` — surveillance (currently no live execution)
- `ROBINHOOD_*` — spot crypto (currently paper only)
- `GMAIL_EMAIL`/`APP_PASSWORD` — backup SMTP

---

## 22. Docker / Deployment

### Single Dockerfile (Multi-Stage)

**Rebuild decision:** Use one multi-stage `Dockerfile`. Delete `Dockerfile.data-pipeline`, `Dockerfile.evaluation`, `Dockerfile.model-upgrade`.

```dockerfile
# Dockerfile (template — verify against current)
FROM python:3.11-slim AS base
RUN apt-get update && apt-get install -y gcc g++ curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS backend
COPY src/ ./src/
COPY config/ ./config/
COPY models/ ./models/
COPY start_backend.py .
COPY start_production.py .
EXPOSE 8000 8001 8099
CMD ["python", "start_production.py"]

FROM node:18-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM backend AS production
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
# FastAPI serves static files from frontend/dist in production
```

### Production docker-compose Template

```yaml
# docker-compose.production.yml
version: "3.9"
services:
  backend:
    build:
      context: .
      target: production
    ports:
      - "8000:8000"
      - "8099:8099"
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - KALSHI_API_KEY_ID=${KALSHI_API_KEY_ID}
      - KALSHI_API_PRIVATE_KEY=${KALSHI_API_PRIVATE_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - SESSION_SECRET=${SESSION_SECRET}
      - AXIOM_PAPER_MODE=${AXIOM_PAPER_MODE:-true}
    restart: unless-stopped
    volumes:
      - ./data:/app/data
      - ./models:/app/models
      - ./config:/app/config

  uw-mcp:
    build:
      context: ./uw-mcp
    ports:
      - "3001:3001"
    environment:
      - UNUSUAL_WHALES_API_KEY=${UNUSUAL_WHALES_API_KEY}
      - DATABASE_URL=${DATABASE_URL}
    restart: unless-stopped
    depends_on:
      - backend
```

### Replit Frontend-Only Proxy Setup

When using Replit + Mac Studio split (Option A):

1. Backend runs on Mac Studio at some public URL → set `MAC_STUDIO_BACKEND_URL`
2. `frontend/server.js` proxies `/api/*` and `/ws/*` to `MAC_STUDIO_BACKEND_URL`
3. Replit deploys only the frontend (static + proxy server)
4. `replit.md` confirms: "Replit deployment is frontend-only"

```javascript
// frontend/server.js (rebuild — pure proxy, no auth)
const { createProxyMiddleware } = require('http-proxy-middleware');
const express = require('express');
const app = express();
const BACKEND = process.env.MAC_STUDIO_BACKEND_URL || 'http://localhost:8000';
app.use('/api', createProxyMiddleware({ target: BACKEND, changeOrigin: true, ws: true }));
app.use('/ws', createProxyMiddleware({ target: BACKEND, changeOrigin: true, ws: true }));
app.use(express.static('dist'));
app.get('*', (req, res) => res.sendFile('dist/index.html', { root: '.' }));
app.listen(process.env.PORT || 3000);
```

---

# PART IV — REBUILD PLAN

---

## 23. Phase 1: Foundation (Week 1)

**Goal:** Fresh repo skeleton with schema, env vars, and topology locked.

| Task | Action | Output |
|---|---|---|
| Fresh repo skeleton | Clone existing repo, delete junk per Appendix G | Clean repo |
| Canonical directory layout | Apply §4 layout — move/delete files | Clean `src/`, `services/` |
| `migrations/0001_initial.sql` | Grep 33 CREATE TABLE sources, deduplicate, emit SQL | `migrations/0001_initial.sql` |
| Alembic setup | `pip install alembic`, `alembic init alembic/`, configure `env.py` | `alembic/` |
| `.env.example` | Write all 135 vars from §19 | `.env.example` |
| `pyproject.toml` | Set Python 3.11, project metadata, build system | `pyproject.toml` |
| Lock requirements | Verify `requirements.txt` versions, freeze | `requirements.txt` |
| Topology decision | Option A documented, `docs/TOPOLOGY.md` written | `docs/TOPOLOGY.md` |
| Delete duplicates | `server_core.py`, `services/data-pipeline/`, `services/model-upgrade/`, dual feature stores | Cleaner repo |

**Validation:** `python -c "import src.api.unified_server"` succeeds with no import errors.

---

## 24. Phase 2: Core Backend (Week 2-3)

**Goal:** FastAPI shell with all 27 routers, DB connection, auth, Kalshi wired. Paper mode running.

| Task | Action | File |
|---|---|---|
| FastAPI app shell | Strip `unified_server.py` to minimal bootable shell | `src/api/unified_server.py` |
| Extract trading loop | Move `_trading_loop_inner()` + state to `trading_loop.py` | `src/api/trading_loop.py` |
| Register 27 routers | Follow `_ROUTER_REGISTRY` order from §5 | `unified_server.py` |
| DB connection | `asyncpg` pool in `lifespan`, migrate via `migrations/0001_initial.sql` | `src/api/db.py` |
| Alembic migration | Run `alembic upgrade head` against `DATABASE_URL` | Postgres tables created |
| Auth (FastAPI JWT) | Keep `src/api/routers/auth.py`, add JWT token response | `auth.py` |
| Remove Node auth | Strip `pg`/`bcryptjs` from `frontend/server.js` | `frontend/server.js` |
| Kalshi client | Copy `src/trading/kalshi_client.py` as-is, wire env vars | `kalshi_client.py` |
| Health endpoint | `GET /api/health` returning `{status, ready, trading_mode}` | `health.py` |
| Paper mode | Set `AXIOM_PAPER_MODE=true`, start trading loop | `.env` |
| Middleware | Wire CORS, auth, logging, rate limit | `middleware.py` |
| Process supervisor | Copy `start_backend.py` and `backend_watchdog.py` | Boot scripts |

**Validation:**
```bash
curl http://localhost:8000/api/health
# → {"status": "ok", "ready": false, "trading_mode": "paper"}
```

---

## 25. Phase 3: ML Pipeline (Week 3-4)

**Goal:** 3-model ensemble (LGB + XGB + CatBoost) training with per-sport calibration.

| Task | Action | File |
|---|---|---|
| Feature registry | Copy `config/production_features.json`, `config/feature_contract.json` | config/ |
| Feature pipeline | Wire canonicalize → parity gate → fake-coverage gate → importance pruning | `src/features/` |
| Build training splits | `scripts/build_training_splits.py` — walk-forward | `scripts/` |
| Wire LGB + XGB + CatBoost | Add CatBoost import + call to `model_trainer.py::train_ensemble` | `model_trainer.py` |
| Meta-learner | Confirm `src/models/meta_learner.py` stacking works with 3 base learners | `meta_learner.py` |
| Per-sport calibration | Load `sport_calibration.py` for CFB/MLB/NBA/NCAAB/NFL/NHL | `calibration/` |
| HPO outputs | Copy `hpo_result_{NBA,MLB,NHL}.json` to config/ | config/ |
| SHAP analysis | Run SHAP on LGB + XGB + CatBoost output | `scripts/` |
| Train run | `python scripts/train_ensemble_fast.py` | `models/champion_metadata.json` |
| AutoResearch engine | Wire `autoresearch_engine.py` + `hypothesis_generator.py` | `ml_training/` |

**Validation:**
```bash
python scripts/train_ensemble_fast.py
# → "Regenerated FEATURE_REGISTRY.md: 698 contract → 393 trainable → 145 selected (vN)"
```

**VERIFY:** After wiring CatBoost, confirm `models/catboost_model.pkl` is regenerated and `models/champion_metadata.json` lists CatBoost in `base_learners`.

---

## 26. Phase 4: Trading Loop (Week 4-5)

**Goal:** Full paper trading loop executing against Kalshi API (paper mode, no real orders).

| Task | Action |
|---|---|
| Signal generator | Wire `src/trading/signal_generator.py` into loop |
| EV engine | Wire `src/trading/bookmaker_ev_engine.py` |
| Edge floors | Wire `src/trading/edge_floors.py` (per-vertical, per-market-type) |
| Swarm debate | Wire `src/trading/swarm_debate.py` (6 personas, multi-provider) |
| Persona weights | Wire `swarm_weight_manager.py` |
| LLM gatekeeper | Wire `llm_gatekeeper.py` |
| Risk manager | Wire `risk_manager.py` (all 12 gates) |
| Order manager | Wire `order_manager.py` + `execution_engine.py` |
| Kalshi execution | Paper mode only: `AXIOM_PAPER_MODE=true` |
| Paper trader | Wire `paper_trader.py` + `paper_trading_runner.py` as parallel track |
| Settlement feedback | Wire `settlement_feedback.py` → online learner |
| Position dedup | Wire `position_dedup.py` as gates 4 + 11 |
| Loop invariants | Ensure `cycle_exposure_added` initialized at function scope |
| READY-gate | Ensure `_deferred_init_complete` before first cycle |

**Validation:**
```bash
# Start backend, watch logs for:
# "[TRADING] Cycle N: evaluated X picks, placed Y trades (paper)"
# "[SWARM] 6-persona debate complete: approved=true/false"
# "[KALSHI-PAPER] Order submitted: market=... qty=... price=..."
```

---

## 27. Phase 5: Frontend (Week 5-6)

**Goal:** All 123 pages loading, WebSocket working, paper trading visible on dashboard.

| Task | Action |
|---|---|
| Vite + React + Tailwind | Verify all deps install cleanly |
| React Query setup | Wire `QueryClientProvider` in root layout |
| Router setup | Verify all 123 routes resolve (no 404s) |
| WebSocket context | `WebSocketContext.tsx` connected to `/ws/*` channels |
| Auth (FastAPI) | `AuthContext.tsx` reading from `/api/auth/me` |
| Dashboard | `Overview/` widgets showing live data |
| Key pages working | Trading, Positions, Calibration, Settings, AccountsUnified |
| Remove Node auth | Strip `pg`/`bcryptjs` from `server.js` |
| Build | `npm run build` → `dist/` — no TypeScript errors |

**Validation:**
```bash
cd frontend && npm run dev
# → Open http://localhost:3000
# → Login page renders
# → Dashboard shows pipeline status
# → Positions page shows open positions
# → /ws/pipeline stream flowing (check browser DevTools WS tab)
```

---

## 28. Phase 6: Verticals (Week 6-8)

**Goal:** All 4 deep verticals running in paper mode with accurate metrics.

| Vertical | Key Tasks |
|---|---|
| **Sports (6 leagues)** | Sportradar + ESPN data flowing, ML inference per game, props pipeline (270 lines × 1,377 markets), calibration per sport |
| **Weather** | 10-source ensemble loading, KXHIGH*/KXTEMP*/KXLOW* markets scanning, weather swarm, 3-layer city dedup, max $25/trade |
| **Crypto Binary** | KXBTC15M/1H/daily loading, XGB+LGB walk-forward inference, posterior_dev gate, retrain pipeline |
| **Player Props** | NBA/MLB/NFL/NHL stats wired, over/under bet_side logic, direction conflict resolution |
| **OUTLIER scraper** | `src/data/outlier_scraper.py` fetching, EV≥5% filter, Kalshi ticker resolution, ESPN game match |
| **Polymarket surveillance** | `uw-mcp/` service up, `polymarket_client.py` fetching market data, NO live orders |

**For each vertical, validate:**
```
signals_scanned > 0
trades_placed_paper > 0
calibration_ece < halt_threshold
```

---

## 29. Phase 7: Observability (Week 8-9)

**Goal:** Full observability — know exactly what the system is doing and why.

| Task | Action |
|---|---|
| 3-layer process supervision | All 3 layers running and tested (kill uvicorn → verify L1 respawns, kill supervisor → verify L2 respawns) |
| Health gates | All 6 health gate checks firing correctly (calibration, completeness, divergence, freshness, corruption, price_divergence) |
| Feature truth table | `scripts/generate_feature_truth_table.py` running, output verifiable |
| Metric ladders | `config/metric_ladders.json` validated against current performance |
| Go-live decision endpoint | `GET /api/go-live/decision` returns honest yes/no with reasons |
| Notifications | Postmark sending trade emails (live mode only), Telegram Concierge responding |
| Daily audit watchdog | `src/watchdog/daily_audit.py` running all 16 checks |
| Log prefixes | All `[TRADING]`, `[OUTLIER]`, `[WEATHER-DIAG]`, `[SWARM]`, `[EXEC]` prefixes emitting |
| PipelineMonitor UI | Real-time cycle events visible in frontend |

---

## 30. Phase 8: Self-Improvement (Week 9-10)

**Goal:** AutoResearch + online learner + postmortem + drift detection all running.

| Task | Action |
|---|---|
| AutoResearch | `autoresearch_engine.py` running every 6h, experiments logged to DB |
| Online learner | SGD `partial_fit` triggering after settlement batches |
| Postmortem engine | `postmortem_engine.py` running after each settlement |
| Hypothesis generator | `hypothesis_generator.py` producing testable hypotheses |
| A/B testing | `src/evaluation/ab_tester.py` + `ab_testing_v2.py` active for challenger models |
| Drift watchdog | `src/ml_training/drift_watchdog.py` monitoring feature distributions |
| Champion/challenger | Challenger model trained, shadow-run, auto-promote if better |
| Model registry | `src/model_upgrade/` handling rollout + rollback |

**Validation:**
```sql
SELECT COUNT(*) FROM autoresearch_experiments WHERE status = 'completed';
-- → > 0 experiments running
SELECT COUNT(*) FROM online_learning_buffer WHERE processed = true;
-- → settlements being consumed
```

---

## 31. Phase 9: Polish + Investment-Ready (Week 10-12)

**Goal:** System is demonstrably working and investment-ready.

| Task | Details |
|---|---|
| Remove sport status overrides | `config/sport_status_overrides.json` — NHL and MLB incorrectly LIVE at AUC 0.58 should be gated. Fix AUC threshold gates to catch this automatically. |
| Run full odds enrichment | 60K games with `odds:{}` — run `scripts/enrich_odds_injuries.py` to backfill |
| 200+ paper trades track record | Accumulate paper trades across all 4 deep verticals |
| Core-4 Brier < 0.18 | Confirm NBA + NHL + MLB + NCAAB ensemble meets target |
| NCAAB Brier < 0.15 | Confirm model quality on NCAAB specifically |
| Fix per-fill P&L attribution bug | See Appendix H — use `abs(qty*price)` not signed cost share |
| Investment deck prep | Screenshots of dashboard, metrics, Brier curves, track record |
| SOC2 prep | Audit trail, access controls, data retention policy |
| Load testing | `scripts/load_test.py` — verify backend handles concurrent WebSocket load |
| Provisional patent check | Confirm filing status |

---

# PART V — APPENDICES

---

## Appendix A: Complete Router Inventory (27 Routers)

| # | File | Path Prefix | Auth Required | WS/REST | Endpoints |
|---|---|---|---|---|---|
| 1 | `admin.py` | `/api/admin` | Yes (admin role) | REST | 5 |
| 2 | `ai_ml.py` | `/api/llm`, `/api/swarm`, `/api/ollama` | Yes | REST | ~30 |
| 3 | `auth.py` | `/api/auth` | No (login/register public) | REST | 11 |
| 4 | `calibration.py` | `/api/calibration` | Yes | REST | ~8 |
| 5 | `concierge.py` | `/api/concierge` | Yes | REST | 4 |
| 6 | `crypto.py` | `/api/crypto` | Yes | REST | ~8 |
| 7 | `events.py` | `/api/events` | Yes | REST | ~8 |
| 8 | `exchanges.py` | `/api/kalshi`, `/api/polymarket`, `/api/robinhood` | Yes | REST | 45 |
| 9 | `health.py` | `/api/health`, `/api/monitoring`, `/api/risk-console`, `/api/services` | No (health public) | REST | ~28 |
| 10 | `live_scores.py` | `/api/live-scores` | Yes | REST | ~3 |
| 11 | `notifications.py` | `/api/notifications` | Yes | REST | ~6 |
| 12 | `orchestrator.py` | `/api/orchestrator` | Yes | REST | ~5 |
| 13 | `pipeline_logs.py` | `/api/pipeline/logs`, `/api/pipeline` | Yes | REST | ~4 |
| 14 | `portfolio.py` | `/api/portfolio`, `/api/positions`, `/api/bankroll`, `/api/orders` | Yes | REST | 30 |
| 15 | `signals.py` | `/api/signals` | Yes | REST | ~4 |
| 16 | `sportradar.py` | `/api/sportradar` | Yes | REST | ~6 |
| 17 | `sports.py` | `/api/espn`, `/api/predictions`, `/api/elo`, `/api/odds`, `/api/line-movement`, `/api/backtest`, `/api/clv`, `/api/intangibles` | Yes | REST | ~28 |
| 18 | `sports_core.py` | `/api/sports`, `/api/stats-cache`, `/api/public-betting` | Yes | REST | ~15 |
| 19 | `stripe_billing.py` | `/api/stripe` | Partial (webhook public) | REST | 3 |
| 20 | `telegram.py` | `/api/telegram` | No (webhook public) | REST | 1 |
| 21 | `trading.py` | `/api/trading`, `/api/settings`, `/api/risk`, `/api/circuit-breakers`, `/api/go-live` | Yes | REST | ~20 |
| 22 | `uw_mcp.py` | `/api/uw-mcp` | Yes | REST | 2 |
| 23 | `uw_predictions.py` | `/api/uw` | Yes | REST | 2 |
| 24 | `watchdog.py` | `/api/watchdog` | Yes | REST | ~4 |
| 25 | `weather.py` | `/api/weather` | Yes | REST | ~5 |
| 26 | `websockets.py` | `/ws/*` | No (verified in WS handshake) | **WS** | 7 channels |
| — | `pipeline_logs.py` (pre-registry) | `/api/pipeline` | Yes | REST | 2 extra |

**Total REST endpoints:** ~250 (consistent with SYSTEM.md claim)

---

## Appendix B: Complete Page Inventory (123 Pages)

### Top-Level Pages (flat, 47 files)

| Page | Lines | Purpose |
|---|---|---|
| `AIHubPage.tsx` | 1,434 | LLM provider config, swarm status |
| `AccountsUnifiedPage.tsx` | — | Unified accounts view |
| `AnalyticsPage.tsx` | 782 | High-level performance analytics |
| `AutoResearchPage.tsx` | 1,627 | AutoResearch experiments, promotions |
| `AxiomEdgePage.tsx` | — | Edge visualization |
| `BankrollTrackerPage.tsx` | 1,882 | Bankroll history, drawdown, Sharpe |
| `BetTogglesPage.tsx` | 471 | Master bet toggles per league |
| `CLVDashboardPage.tsx` | 538 | CLV decomposition dashboard |
| `CalibrationPage.tsx` | 1,022 | Per-vertical calibration status |
| `CryptoBinaryPage.tsx` | — | Crypto binary trading |
| `CryptoDashboardPage.tsx` | 888 | Crypto unified dashboard |
| `CryptoEVPicksPage.tsx` | 452 | Crypto EV picks |
| `CryptoIntelligencePage.tsx` | 626 | BTC signals, regime, sentiment |
| `CryptoMarketDataPage.tsx` | 734 | OHLCV, orderbook, derivatives |
| `CryptoPredictionsPage.tsx` | 692 | Model predictions for upcoming markets |
| `CryptoTradesPage.tsx` | 584 | Crypto trade history |
| `CryptoUnifiedPage.tsx` | — | Crypto unified view |
| `DiagnosticsPage.tsx` | 888 | Diagnostics tools |
| `ESPNStatsPage.tsx` | 299 | ESPN stats cache |
| `EVSourceComparisonPage.tsx` | 418 | EV source comparison |
| `EloRatingsPage.tsx` | 516 | Elo ratings per team |
| `EventsPage.tsx` | 856 | 11-tab events vertical view |
| `ExecutionQualityPage.tsx` | 212 | Execution quality metrics |
| `FeatureIntegrityPage.tsx` | 176 | Feature integrity check |
| `IntelligenceUnifiedPage.tsx` | — | Intelligence unified view |
| `KalshiAccountPage.tsx` | 721 | Kalshi account state |
| `LLMLogsPage.tsx` | 349 | LLM call logs |
| `LogsPage.tsx` | — | System logs |
| `NotificationCenterPage.tsx` | 347 | Notification center |
| `PipelineMonitorPage.tsx` | 1,020 | Live cycle-by-cycle audit |
| `PolymarketAccountPage.tsx` | 270 | Polymarket surveillance account |
| `RejectedTradesPage.tsx` | 265 | Rejected trades audit |
| `RiskConsolePage.tsx` | 965 | Live risk gate status |
| `RiskControlsPage.tsx` | 1,095 | Risk parameter command center |
| `RobinhoodAccountPage.tsx` | 1,016 | Robinhood spot account |
| `RobinhoodPortfolioPage.tsx` | 454 | Robinhood portfolio |
| `SettingsPage.tsx` | 1,485 | Master settings, LLM config |
| `ShadowTradingPage.tsx` | 553 | Shadow trading results |
| `SportradarPage.tsx` | 609 | Sportradar service status |
| `StatsCalibrationPage.tsx` | — | Stats calibration |
| `SystemUnifiedPage.tsx` | — | System unified view |
| `TradeBlockedCards.tsx` | 175 | Blocked trade cards |
| `Trading.tsx` | 1,364 | Main trading dashboard |
| `UnifiedAnalyticsPage.tsx` | — | Unified analytics |
| `UnifiedSystemPage.tsx` | — | Unified system view |
| `UnifiedTradingPage.tsx` | — | Unified trading view |
| `WeatherUnifiedPage.tsx` | — | Weather unified view |

### Sub-Directory Pages (76 files in feature folders)

| Folder | Files | Key Pages |
|---|---|---|
| `AICenter/` | 1 | `LLMTrendsTab.tsx` (611 lines) |
| `Admin/` | 1 | `AdminPage.tsx` (778 lines) |
| `Alerts/` | 1 | `AlertsPage.tsx` |
| `Auth/` | 1 | `LoginPage.tsx` (500 lines) |
| `Control/` | 1 | `ControlPage.tsx` |
| `Diagnostics/` | 2 | `SwarmPerformanceTab.tsx` (594), `SystemDiagnostics.tsx` (624) |
| `EVPicks/` | 8 | `PicksTable.tsx` (361), `PropsTable.tsx` (250), `PropCard.tsx` (182), `FeaturedCard.tsx` (302), `MatchupsView.tsx` (211), `PropsView.tsx`, `shared.tsx` (171) |
| `Health/` | 1 | `HealthPage.tsx` |
| `Intelligence/` | 2 | `MCPSignalsPage.tsx` (244), `SwarmIntelligencePage.tsx` (838) |
| `MLTraining/` | 4 | `MLTrainingPage.tsx` (386), `TradeAnalysisPanel.tsx` (283), `CategoryPerformancePanel.tsx` (249), `ModelEvolutionChart.tsx` |
| `NotificationCenter/` | 1 | `EmailBuilder.tsx` (445) |
| `Onboarding/` | 1 | `OnboardingPage.tsx` |
| `Overview/` | 20 | Mission Control widgets: `LivePipelineTracker.tsx` (1,142), `CoreMetricsRow.tsx` (248), `IntelligenceScorePanel.tsx` (199), `AutoResearchMLWidget.tsx` (280), plus 16 more |
| `Positions/` | 1 | `PositionsPage.tsx` (1,987) |
| `Pricing/` | 1 | `PricingPage.tsx` (359) |
| `Profile/` | 1 | `ProfilePage.tsx` (430) |
| `Public/` | 3 | `AboutPage.tsx` (279), `LandingPage.tsx` (380), `LeaderboardPage.tsx` (484) |
| `Signals/` | 1 | `SignalIntelligencePage.tsx` (634) |
| `Sports/` | 9 | `GameDetailPage.tsx` (1,198), `PlayerPropDetailPage.tsx` (692), `IntangiblesPage.tsx` (914), `SportManagementPage.tsx` (347), `ExternalPicksPage.tsx` (354), `MatchupPage.tsx`, `DataSources/BettingProsPage.tsx` (540), `DataSources/OutlierPage.tsx` (252), `DataSources/SportsLinePage.tsx` (387) |
| `Weather/` | 4 | `WeatherPage.tsx` (1,497), `CityDetail/CityDetailPage.tsx` (1,190), `WeatherEVPicksPage.tsx` (515), `SportsWeatherEVPage.tsx` (373) |

---

## Appendix C: All 145 Selected Features (Model v155)

Source: `models/champion_metadata.json::feature_names_full` + `FEATURE_REGISTRY.md`

### C.1 Elo & Power Ratings (4)
`home_elo`, `away_elo`, `elo_diff`, `home_elo_win_prob`

### C.2 KenPom NCAAB (9)
`home_kenpom_net`, `away_kenpom_net`, `kenpom_net_rating_diff`, `home_kenpom_adj_o`, `away_kenpom_adj_o`, `home_kenpom_adj_d`, `away_kenpom_adj_d`, `away_kenpom_tempo`, `kenpom_tempo_diff`

### C.3 Rest, Pace, Schedule (5)
`home_days_rest`, `away_days_rest`, `home_b2b`, `away_b2b`, `away_pace`

### C.4 Four Factors NBA/NCAAB (7)
`home_efg_pct`, `away_efg_pct`, `away_tov_rate`, `home_orb_rate`, `away_orb_rate`, `home_ft_rate`, `away_ft_rate`

### C.5 Market Pricing (5)
`kalshi_mid`, `kalshi_implied_home_prob`, `away_moneyline_implied_prob`, `spread`, `total`

### C.6 Form & Recent Performance (5)
`home_l10_winpct`, `away_l10_winpct`, `home_streak`, `away_streak`, `spread_model_edge`

### C.7 Opening Lines & CLV (4)
`opening_spread`, `opening_total`, `home_margin_last5`, `away_margin_last5`

### C.8 NFL Advanced (EPA / DVOA / QB) (20)
`home_off_epa_per_play`, `away_off_epa_per_play`, `home_def_epa_per_play`, `away_def_epa_per_play`, `home_pass_epa`, `away_pass_epa`, `home_rush_epa`, `away_rush_epa`, `away_off_dvoa`, `home_def_dvoa`, `away_def_dvoa`, `home_st_dvoa`, `away_st_dvoa`, `home_qb_cpoe`, `away_qb_cpoe`, `away_qb_epa`, `away_third_down_conv_pct`, `away_redzone_td_pct`, `home_turnover_diff_season`, `away_turnover_diff_season`

### C.9 Weather (1)
`wind_mph`

### C.10 MLB Pitching (FIP / xFIP / K-BB) (11)
`home_sp_fip`, `away_sp_fip`, `home_sp_xfip`, `away_sp_xfip`, `home_sp_era_last5`, `away_sp_era_last5`, `home_sp_k_per9`, `away_sp_k_per9`, `home_sp_bb_per9`, `away_sp_bb_per9`, `home_sp_hr_per9`

### C.11 MLB Hitting (wOBA / xwOBA / ISO) (8)
`home_woba`, `away_woba`, `home_xwoba`, `away_xwoba`, `home_iso`, `away_iso`, `home_barrel_pct`, `away_barrel_pct`

### C.12 NHL Advanced (Corsi / xGF / PDO) (11)
`home_cf_pct`, `away_cf_pct`, `home_xgf_pct`, `away_xgf_pct`, `home_pdo`, `away_pdo`, `home_pp_pct`, `away_pp_pct`, `home_pk_pct`, `away_pk_pct`, `home_sv_pct`

### C.13 Injury Risk (4)
`home_injury_count`, `away_injury_count`, `home_injury_risk_score`, `away_injury_risk_score`

### C.14 Sharp Money / Line Movement (10)
`steam_move_detected`, `sharp_money_pct`, `line_movement_direction`, `line_value_home`, `public_money_pct_home`, `opening_to_current_spread_move`, `opening_to_current_ml_move`, `reverse_line_move`, `closing_line_value_spread`, `closing_line_value_ml`

### C.15 Market Microstructure (5)
`kalshi_volume`, `kalshi_open_interest`, `kalshi_spread`, `bid_ask_imbalance`, `time_to_close_hours`

### C.16 Situational / Intangibles (10)
`home_advantage_factor`, `altitude_factor`, `is_dome`, `is_turf`, `travel_distance`, `back_to_back_home`, `back_to_back_away`, `division_game`, `playoff_spot_importance`, `revenge_factor`

### C.17 CLV (2)
`clv_spread`, `clv_total`
*(Note: `clv_moneyline` is FAKE_COVERAGE — excluded)*

### C.18 Miscellaneous / Model-Derived (24)
VERIFY against `models/champion_metadata.json` — these vary by training run. Includes interaction terms, calibrated sub-model outputs, and model-specific auxiliary features.

**Total verified: 121 named + 24 VERIFY = 145 selected**

---

## Appendix D: Complete Env Var List (135 Vars)

See §19 for full annotated list. Summary by group:

| Group | Count |
|---|---|
| Core | 11 |
| Trading Knobs | 6 |
| Exchanges | 12 |
| Data Feeds | 18 |
| LLM Providers | 4 |
| Email/Notifications | 5 |
| Billing | 3 |
| UW MCP | 16 |
| Microservice URLs | 17 |
| Eval Thresholds | 5 |
| Timers | 9 |
| Misc | 9 |
| **Total** | **135** |

---

## Appendix E: Complete Table Schema (~80 Tables)

Full column-level schema for all ~80 tables, deduplicated from 33 source files.

Column format: `table_name :: column_name (type), ...`

> **Action items for rebuild:**
> 1. Extract all `CREATE TABLE` statements from the 33 source files listed in the audit (§5.6 of audit)
> 2. Deduplicate (same table defined in multiple files — use `unified_server.py` copy as canonical)
> 3. Emit `migrations/0001_initial.sql`
> 4. Run `alembic stamp head` to mark as migrated

Key tables with full schemas documented in SYSTEM.md §18 and Appendix H:

### users
```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    display_name VARCHAR(255),
    role VARCHAR(50) DEFAULT 'user',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP,
    subscription_tier VARCHAR(50) DEFAULT 'free',
    avatar_url TEXT,
    onboarding_complete BOOLEAN DEFAULT false,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    payment_status VARCHAR(50),
    payment_failed_at TIMESTAMPTZ,
    has_used_pro_trial BOOLEAN DEFAULT false,
    email_daily_digest BOOLEAN DEFAULT true,
    email_weekly_summary BOOLEAN DEFAULT true
);
```

### system_settings
```sql
CREATE TABLE system_settings (
    key VARCHAR(255) PRIMARY KEY,
    value JSONB,
    updated_at TIMESTAMP DEFAULT NOW()
);
```

### swarm_debates
```sql
CREATE TABLE swarm_debates (
    id SERIAL PRIMARY KEY,
    game_id VARCHAR(255),
    matchup VARCHAR(255),
    league VARCHAR(50),
    analyst_verdicts JSONB,
    moderator_synthesis JSONB,
    consensus_probability DOUBLE PRECISION,
    confidence_band_low DOUBLE PRECISION,
    confidence_band_high DOUBLE PRECISION,
    agreement_rate DOUBLE PRECISION,
    approved BOOLEAN,
    recommended_action VARCHAR(50),
    single_llm_probability DOUBLE PRECISION,
    cost_estimate DOUBLE PRECISION,
    latency_ms DOUBLE PRECISION,
    outcome VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    market_type VARCHAR(50),
    game_context JSONB
);
```

For remaining ~77 tables: VERIFY from `src/api/unified_server.py` (21 CREATE TABLE ops) + `src/ml_training/schema.py` (15 ops) + remaining 31 source files. Full verbatim column dump is in SYSTEM.md Appendix H, §H.1–H.3 (lines 3390–3765).

---

## Appendix F: Swarm Prompts (Preserved)

Full verbatim prompts are in SYSTEM.md §Appendix F (lines 3064–3307). They must be copied verbatim to `src/trading/swarm_debate.py` in the rebuild — do not rephrase.

### F.1 Sports Analyst Personas (6)

1. `sharp_edge` — Sharp Edge Analyst: "You are the Sharp Edge Analyst. Your expertise is in reading sharp bookmaker lines (Pinnacle, Betfair) and identifying when Kalshi/Polymarket prices diverge from efficient market pricing..."
2. `injury_roster` — Injury & Roster Specialist: "You are the Injury & Roster Specialist. You focus exclusively on how injuries, lineup changes, rest days, back-to-back schedules..."
3. `momentum_form` — Momentum & Form Analyst: "You are the Momentum & Form Analyst. You study recent performance trends, win/loss streaks..."
4. `public_contrarian` — Public Sentiment Contrarian: "You are the Public Sentiment Contrarian. You analyze public betting percentages, social media sentiment..."
5. `matchup_historian` — Matchup Historian: "You are the Matchup Historian. You study head-to-head records, coaching tendencies in specific matchups..."
6. `risk_guardian` — Risk & Bankroll Guardian: "You are the Risk & Bankroll Guardian. Your role is to protect the bankroll..."

**IMPORTANT:** All 6 personas have the directive "IMPORTANT: You MUST always provide a probability and recommendation... Never refuse to give a verdict." This prevents LLM abstention.

### F.2 Weather Analyst Personas (6)

1. `meteorological_analyst` — focus on weather model consensus (NWS/ECMWF/GFS/HRRR)
2. `forecast_uncertainty` — focus on model disagreement, confidence intervals
3. `station_bias_expert` — focus on airport METAR vs city center, urban heat island
4. `settlement_timing` — focus on diurnal cycles, front arrival timing
5. `source_agreement_validator` — focus on consensus quality, data freshness
6. `wx_risk_guardian` — focus on portfolio risk, city correlation

### F.3 Round 1 Prompt (Verbatim — copy exactly)
See SYSTEM.md lines 3140–3180. Key structural elements:
- All 6 personas batched in one call (`temperature=0.3, max_tokens=3000`)
- Strict JSON array output: `[{"analyst_id": ..., "probability": ..., "recommended_action": "BET_YES|BET_NO|BET_OVER|BET_UNDER", "key_factors": [...], "reasoning": ..., "risk_flags": [...]}]`

### F.4 Round 2 Moderator Prompt (Verbatim — copy exactly)
See SYSTEM.md lines 3188–3237. Key elements:
- Aggression mode injected: `conservative | moderate | aggressive | max_alpha`
- Track records injected per analyst
- Approval criteria: `consensus_probability >= 0.52 or <= 0.48` AND `≥2 analysts agree`
- **ACTIVE TRADING MANDATE:** Do NOT default to PASS — approve when data supports directional lean ≥55% win probability

### F.5 Aggression Bias Strings
```python
AGGRESSION_MODERATOR_BIAS = {
  "conservative": "Strongly favor Risk Guardian's caution. Reject unless 4+ analysts agree...",
  "moderate": "Balance all opinions. 2+ analysts agreeing on direction justifies approval...",
  "aggressive": "Weight edge-finding analysts more heavily...",
  "max_alpha": "Maximum edge seeking. Even 2 analysts finding strong edge can override Risk Guardian...",
}
```

### F.6 LLM Cost Accounting
```python
_COST_PER_1K_TOKENS = {
  "openai": 0.005,
  "anthropic": 0.008,
  "google": 0.001,
  "mistral": 0.002,
  "deepseek": 0.001,
}
```

### F.7 Output Dataclasses (Copy Exactly)
```python
@dataclass
class AnalystVerdict:
    analyst_id: str
    analyst_name: str
    probability: float
    confidence: float
    recommended_action: str
    key_factors: list[str]
    reasoning: str
    risk_flags: list[str]

@dataclass
class SwarmConsensus:
    game_id: str
    matchup: str
    league: str
    consensus_probability: float
    confidence_band_low: float
    confidence_band_high: float
    agreement_rate: float
    approved: bool
    recommended_action: str
    moderator_reasoning: str
    risk_factors: list[str]
    individual_verdicts: list[AnalystVerdict]
```

---

## Appendix G: What to DELETE from Current Repo

### Confirmed Deletions

```bash
# Duplicate server file
rm src/api/server_core.py

# Duplicate service directories (keep underscore naming)
rm -rf services/data-pipeline/
rm -rf services/model-upgrade/

# Orphaned ML model (keep file, but do not call from trainer)
# src/models/lstm_model.py — KEEP FILE, mark deferred
# src/models/transformer_model.py — DELETE
rm src/models/transformer_model.py

# Duplicate feature store implementations
rm src/features/feature_store.py   # v1 — superseded by feature_store_v2.py
rm -rf src/features/store/          # Third duplicate

# Model artifact spam
find models/ -name "evaluation_report_v*.json" | sort | head -n -10 | xargs rm -f
# (keep last 10, delete older 290+)

# Interrupted weight files
find models/ -name "current_weights_v*.tmp.json" -exec rm {} \;

# CatBoost training metadata (can regenerate)
rm -rf catboost_info/  # or mv to artifacts/

# Root-level JS clutter
rm debug-odds.js
rm fetch-polymarket-soccer.mjs
rm fetch-soccer-live.mjs
rm fetch-soccer-schedule.mjs
rm generate-live-update.mjs
rm hou-isu-monitor.sh
rm market-inefficiency-scanner.js
rm market-inefficiency-scanner-monitor.js
rm test-kalshi-api.mjs
rm update-soccer-scores.js
rm update-soccer-scores.mjs

# Root-level large JSON files (move to config/research/ or archive)
mv historical-odds-researcher-FINAL.json config/research/ 2>/dev/null || rm historical-odds-researcher-FINAL.json
mv historical-odds-researcher-update.json config/research/ 2>/dev/null || rm historical-odds-researcher-update.json
mv historical-odds-researcher.json config/research/ 2>/dev/null || rm historical-odds-researcher.json
mv ncaab-data-collector.json config/research/ 2>/dev/null || rm ncaab-data-collector.json

# Runtime state at root (move to artifacts/)
mkdir -p artifacts
mv training_result.json artifacts/ 2>/dev/null || true
mv pipeline_state.json artifacts/ 2>/dev/null || true

# Legacy architecture docs
mv ARCHITECTURE.md docs/archive/

# Legacy server entry points
rm src/api/server.py      # AFTER VERIFY nothing imports it
rm src/api/app.py         # AFTER VERIFY
rm src/api/gateway.py     # AFTER VERIFY

# Per-service Dockerfiles (superseded by multi-stage Dockerfile)
rm Dockerfile.data-pipeline
rm Dockerfile.evaluation
rm Dockerfile.model-upgrade
```

### Verify Before Deleting

```bash
# Check if anything imports server.py / app.py / gateway.py
grep -r "from src.api.server " src/ || echo "safe to delete"
grep -r "from src.api.app " src/ || echo "safe to delete"

# Check kalshi_routes.py is superseded
grep -r "kalshi_routes" src/ --include="*.py" | grep -v "kalshi_routes.py"
# If no results outside the file itself: safe to delete

# Confirm routes.py is unused
grep -r "from src.api.routes" src/ --include="*.py"
```

---

## Appendix H: Known Bugs to Fix in Rebuild

| # | Bug | Evidence | Fix | Priority |
|---|---|---|---|---|
| 1 | **Per-fill P&L attribution bug** | `_backfill_trade_pnl_from_settlements` uses signed cost share; when positions have both buys AND sells, `total_cost` is net → per-fill shares blow up to 5×–125× true value | Use `abs(qty*price)` everywhere for cost share; shares always sum to 1 | CRITICAL |
| 2 | **Sport status overrides bypass gates** | `config/sport_status_overrides.json` has NHL and MLB incorrectly marked LIVE at AUC 0.58 | Remove manual overrides; replace with automatic AUC threshold gate (require AUC ≥ 0.65 before LIVE status) | HIGH |
| 3 | **Odds enrichment never run** | 60K games in DB with `odds:{}` — enrichment scripts exist but never executed | Run `scripts/enrich_odds_injuries.py` + `scripts/enrich_synthetic_elo_odds.py` against DB | HIGH |
| 4 | **`OPENAI_APY_KEY` typo** | Confirmed in grep — missing 'I' in API | Rename all occurrences to `OPENAI_API_KEY` | HIGH |
| 5 | **Dual frontend auth** | `frontend/server.js` uses `pg`/`bcryptjs` for sessions; separate from FastAPI auth router | Consolidate to FastAPI JWT auth; strip Node auth from `server.js` | HIGH |
| 6 | **Settlement feedback VERIFY** | `[AGENT-FIX 2026-04-XX]` note says "was fixed" but per-fill attribution bug persists | After fixing bug #1, run `POST /api/kalshi/repair-pnl` on full history | MEDIUM |
| 7 | **Version parity** | Registry / weights / symlink must all match v155 | Verify `models/champion_metadata.json` version matches loaded ensemble | MEDIUM |
| 8 | **Feature store dual implementation** | `feature_store.py` (v1), `feature_store_v2.py`, `store/feature_store.py` — three variants | Delete v1 + nested store; use `feature_store_v2.py` only | MEDIUM |
| 9 | **`category_kelly_adjustments.json` placeholder only** | Only has `league:Unknown` and `market_type:Market` entries | Populate with real per-league/per-market adjustments from SYSTEM.md §19 data | MEDIUM |
| 10 | **Stale APR-11 MIA positions** | Positions open beyond market close | Cleanup script + add stale position detection to watchdog | LOW |
| 11 | **Duplicate `_backfill_trade_pnl_from_settlements`** | Defined in both `unified_server.py` and `server_core.py` | After deleting `server_core.py`, single definition only | LOW |
| 12 | **`bankroll_config.json.allocated_amount`** | Shows $1,000 (stale); live bankroll from Kalshi API | Update config to match live bankroll or clearly document it's only initial allocation | LOW |
| 13 | **`replit.md` claims 5-model ensemble** | Inflated vs SYSTEM.md's 4-model claim vs audit's 2 actively trained | Update to honest 3-model (LGB + XGB + CatBoost) after CatBoost wired | LOW |
| 14 | **`_HEALTH_CACHE_TTL`** | Was 15s, doubled to 30s to reduce cold-cache hangs — may be too long | Monitor for stale health data; tune if needed | LOW |

---

## Appendix I: Investment Readiness Checklist

| Item | Target | Status |
|---|---|---|
| **Core-4 Brier < 0.18** (NBA + NHL + NCAAB + MLB) | 0.18 | ✅ ~0.180 per SYSTEM.md |
| **NCAAB Brier < 0.15** | 0.15 | ✅ per SYSTEM.md |
| **Overall Brier** | < 0.22 | ✅ 0.2094 (v155) |
| **AUC** | ≥ 0.70 | ✅ 0.7127 |
| **ECE** | < 0.025 | ✅ 0.0192 |
| **200+ paper trades track record** | 200 | ⬜ Accumulate |
| **Full odds enrichment run** | 60K games enriched | ⬜ Run scripts |
| **Sport status overrides removed** | No manual overrides | ⬜ Fix gating logic |
| **Per-fill P&L attribution fixed** | Correct per-fill PnL | ⬜ Fix bug |
| **CatBoost wired into trainer** | 3-model ensemble | ⬜ Wire + retrain |
| **0.env.example created** | 135 vars | ⬜ Done when committed |
| **SQL migration file created** | `migrations/0001_initial.sql` | ⬜ Extract from 33 sources |
| **Demo video** | 3-5 min system walkthrough | ⬜ Record |
| **Provisional patent** | Filed | VERIFY current status |
| **Pitch deck** | 12-slide investor deck | ⬜ Create |
| **SOC2 prep** | Audit trail, access controls | ⬜ Framework |
| **Load test passed** | 100 concurrent WS connections | ⬜ Run `scripts/load_test.py` |
| **Duplicate files cleaned** | As per Appendix G | ⬜ Execute deletions |
| **Auth consolidated (FastAPI only)** | No Node auth layer | ⬜ Strip `server.js` |

---

## Operational Runbook

### Debugging the Trading Loop

1. **Check health:** `curl http://localhost:8000/api/health`
   - `status`: ok/halted
   - `halted`, `halt_reason`
   - `trading_mode`: live/paper
   - `ready`: deferred init complete

2. **Check trading status:** `curl http://localhost:8000/api/trading/status`
   - `running`, `cycles_completed`, `trades_placed`
   - `last_error`

3. **Check performance monitor:** `curl http://localhost:8000/api/trading/performance-monitor`
   - `should_pause`, `kalshi_streak`, `kalshi_daily_pnl`

4. **Check watchdog:** `curl http://localhost:8099/status`

5. **Tail logs:** `/tmp/logs/Backend_API_*.log`

### Common Log Prefixes

| Prefix | Source |
|---|---|
| `[TRADING]` | Main loop status |
| `[OUTLIER]`, `[OUTLIER-DBG]`, `[OUTLIER-MATCH]` | Outlier scanner |
| `[WEATHER-DIAG]` | Weather pipeline gates |
| `[EVENTS]` | Events vertical |
| `[INFERENCE]` | ML inference |
| `[EXEC]` | Order execution |
| `[SWARM]` | Swarm debate |
| `[AGENT-FIX YYYY-MM-DD]` | Agent unilateral fixes |
| `CALIBRATION HALT` | Calibrator forced sizing_scale=0 |

### Common Operations

| Action | Command |
|---|---|
| Resume from halt | `POST /api/trading/resume` |
| Force emergency stop | `POST /api/kalshi/emergency-stop` |
| Toggle sport | Edit `config/bet_toggles.json` |
| Sync Kalshi fills | `POST /api/kalshi/sync-fills` |
| Repair P&L | `POST /api/kalshi/repair-pnl` |
| Force retrain | Trigger via MLTraining UI |
| Run daily audit | `POST /api/watchdog/run-audit` |
| Check watchdog spawns | `curl http://localhost:8099/spawn_count` |

### Emergency Stop

```bash
# Via API
curl -X POST http://localhost:8000/api/kalshi/emergency-stop

# Via shell
bash scripts/emergency_stop.sh
```

---

*End of Axiom-Predict Rebuild Spec v1.0 — 2026-04-21*

*This document is the authoritative rebuild guide. It supersedes SYSTEM.md.*
*When claims conflict, prefer this document (which incorporates audit corrections).*
*Items marked VERIFY require manual code inspection during rebuild.*

---

## Appendix J: Weather Vertical — Deep Technical Reference

### Weather Model v6 — Complete Architecture

**Primary file:** `src/weather/model.py` (1,802 lines)

The weather model operates on a fundamentally different paradigm than the sports model. Instead of a fixed ML ensemble, it is a **probabilistic mixture distribution** derived from 10 meteorological forecast sources, calibrated against METAR airport observations.

#### 10 Forecast Sources & Optimized Weights

| Source | Weight | Update Frequency | Notes |
|---|---|---|---|
| NWS Hourly | 0.302 | Hourly | Primary — National Weather Service hourly forecast |
| NWS Forecast | 0.225 | 6 hours | NWS standard forecast |
| Tomorrow.io | 0.139 | Sub-hourly | Paid API — hyperlocal |
| AccuWeather | 0.094 | 1 hour | RealFeel temp included |
| Open-Meteo | 0.077 | 1 hour | Free, reliable |
| ICON | 0.051 | 3 hours | European Consortium |
| JMA | 0.045 | 6 hours | Japan Meteorological Agency |
| GEM | 0.032 | 6 hours | Canadian Global Environmental Multiscale |
| GFS | 0.019 | 6 hours | Global Forecast System (NOAA) |
| ECMWF | 0.013 | 12 hours | European Centre — highest quality but slowest |

Weights from `config/weather_model_config.json` — optimized via sigma sweep (`src/weather/sigma_sweep.py`).

#### Sigma Model Parameters

```python
# Base uncertainty model
sigma_base = 3.806         # Degrees F base uncertainty
sigma_min = 1.0            # Floor
sigma_max = 5.0            # Ceiling
frontal_mult = 1.269       # Frontal system → wider uncertainty
stable_high_mult = 0.863   # Stable high pressure → narrower
overcast_mult = 0.980      # Overcast sky
windy_mult = 1.209         # Windy conditions → wider

# Per-city overrides (learned from METAR history)
per_city_sigma = {"DEN": 2.1, "NYC": 2.3, "AUS": 1.9}
```

#### Prediction Pipeline

```
1. Data fetcher (data_fetcher.py, 733 lines)
   - Polls 10 sources
   - Stores in weather_forecasts, weather_observations tables
   
2. Consensus (consensus.py, 333 lines)
   - Mixture Gaussian from all available sources
   - Weighted by source_weight × source_reliability × recency_factor
   - Outputs: ensemble_mean, ensemble_std, mixture distribution

3. Settlement model (settlement_model.py, 827 lines)
   - P(high_temp ≥ bucket_threshold | current_nowcast, time_remaining)
   - Key: uses METAR-specific bias corrections

4. Advanced prediction (advanced_prediction.py, 775 lines)
   - Trajectory analysis (temp_velocity, temp_acceleration)
   - Pressure tendency (3h surface pressure change)
   - Solar radiation budget
   - Marine influence detection

5. EMOS calibration (src/weather/ensemble_calibration.py, 457 lines)
   - Ensemble Model Output Statistics
   - Per-city: param_a, param_b, param_c, param_d
   - Stored in weather_emos_params table
   
6. Sigma sweep (sigma_sweep.py, 541 lines)
   - Hyperparameter search over sigma_base, multipliers
   - Minimizes CRPS (Continuous Ranked Probability Score)
   
7. HRRR client (hrrr_client.py, 192 lines)
   - NOAA High-Resolution Rapid Refresh model
   - Gridded forecast at 3km resolution
   - Hourly updates
```

#### Quality Gates (in order)

```python
# Gate 1: Source agreement
if source_agreement < 0.35:
    reject("LOW_SOURCE_AGREEMENT")

# Gate 2: Market tradeable
if not market.is_tradeable:
    reject("NOT_TRADEABLE")

# Gate 3: Model probability present
if market_prob is None:
    reject("NO_MODEL_PROB")

# Gate 4: City blacklist
if city_code in BLACKLISTED_CITIES:  # e.g. CHI (28% WR, -$489 historical)
    reject("CITY_BLACKLISTED")

# Gate 5: Edge floor
if edge < 0.06:
    reject("BELOW_EDGE_FLOOR")

# Gate 6: Weather health gate
if calibration_ece > 0.035:
    reject("CALIBRATION_HALT")
    
# Gate 7: 3-layer city dedup
if already_traded_city_today:
    reject("CITY_ALREADY_TRADED")
```

#### Anti-Stockyard (3-Layer City Dedup)

```python
# Layer 1: DB query at cycle start
cities_traded = db.query("SELECT DISTINCT city_code FROM trades 
                         WHERE vertical='weather' 
                         AND date(executed_at) = CURRENT_DATE")

# Layer 2: In-cycle tracking
cities_in_cycle = set()
# Immediately add to set when qualifying a candidate

# Layer 3: Execution-time DB re-check
# Re-verify before placing order (handles concurrent cycle edge case)
```

#### Sizing Rules

```python
max_trade = 25.0    # Down from $50 after audit found avg loss $47 vs avg win $23
low_confidence_half_size = model_prob < 0.55  # Half-size low-confidence trades
min_confidence_threshold = 0.50              # Reject if model_prob <= 0.50
```

#### Settlement Observation

Kalshi weather markets settle on **METAR airport observations**, not city center temperature. The model corrects for:
- Airport-to-city-center temperature offset (airports typically 2-5°F cooler)
- Urban heat island effect
- Station elevation adjustments
- Runway orientation effects

---

## Appendix K: Crypto Vertical — Deep Technical Reference

### Binary Trading Engine (`src/crypto/binary_trading_engine.py`, 3,836 lines)

The largest single crypto module. Handles KXBTC15M (15-min close), KXBTC1H, KXBTCD (daily), and bracket / up-down variants.

#### Model Architecture

```
Prediction pipeline:
prediction_engine.py (970 lines)      → primary predictor
  ↓ inputs:
    short_term_model.py (1,414 lines) → short-horizon LSTM (NOTE: this is crypto-specific, separate from sports LSTM)
    gru_btc_model.py (385 lines)      → GRU model
    bayesian_signal.py (532 lines)    → Bayesian aggregator
    regime_detector.py (652 lines)    → Regime context
  ↓ outputs:
    xgb_prob, lgb_prob, stacker_prob, calibrated_prob
```

#### Feature Engineering (`src/crypto/feature_engine.py`, 711 lines)

Features include:
- OHLCV from CoinGecko + Binance Public (at multiple timeframes: 5m, 15m, 1h, 4h)
- LOB imbalance at 5/10/20 levels (`crypto_lob_snapshots` table)
- Derivatives: funding rate, open interest, perpetual premium
- On-chain: large wallet flows, miner outflows (`onchain_feed.py` 418 lines)
- Sentiment: Fear & Greed Index, CryptoPanic news (`sentiment_feed.py` 393 lines)
- Intangibles narrative context (`crypto_intangibles.py` 822 lines)

#### Risk Config (Post-2026-04-19 Hardening)

```python
min_confidence = 70.0   # Raised from 62.8
min_edge = 5.0          # Raised from 3.25 (%)
min_EV = 5.0            # Raised from 2 (%)
posterior_dev = 0.03    # ±3% from 0.50 floor — kills coin-flip round-trips
max_trades_per_day = 100  # Down from 750
max_trades_per_hour = 15
max_exposure = 150.0    # Down from $300
daily_loss_limit = 50.0  # Down from $9,999
circuit_breaker_losses = 5  # Down from 10
```

#### Promotion Gates (Retrain Pipeline)

```python
# promotion gates in retrain_pipeline.py
ECE_THRESHOLD = 0.15
MIN_SHARPE = 0.1
BRIER_IMPROVEMENT = 0.002  # Must improve by 0.002 on holdout
# Progressive relaxation after N failed promotions:
if failed_promotions > 5:
    ECE_THRESHOLD *= 1.1  # Relax 10% per failure cycle
```

#### Post-Audit Fixes

```python
# [AGENT-FIX 2026-04-19]: Never re-buy a held ticker
if ticker in current_positions:
    skip("ALREADY_POSITIONED")

# [AGENT-FIX 2026-04-19]: Verify Kalshi accepted order before logging
order_response = kalshi_client.place_order(...)
if not order_response.get("order_id"):
    skip_logging()  # Do not log a trade that wasn't accepted

# Crypto binary trade email guard:
if trading_mode == "paper" or fill_price is None:
    skip_email()  # Never email for paper or unfilled
```

---

## Appendix L: Sports Data Pipeline — Deep Reference

### Sportradar Integration

**Cache status:** 49 JSON files populated in `data/sportradar/`:
- `schedules/` — per-sport schedules (MLB, NBA, NCAAB, NCAAFB, NHL)
- `injuries/` — per-sport injury reports

**Live feeds** (`src/data/sportradar_live.py`):
- Real-time game state
- Injury updates
- Live score feeds (consumed by `live_game_tracker.py`)

**Router:** `src/api/routers/sportradar.py`
```
GET /api/sportradar/schedule/{sport}    # Schedule for sport
GET /api/sportradar/injuries/{sport}    # Current injury report
GET /api/sportradar/standings/{sport}   # League standings
GET /api/sportradar/ml-features/{game_id}  # ML-ready features
GET /api/sportradar/status              # API key health
```

### ESPN Integration

**Public ESPN API** — no API key required:
```
GET /api/espn/games              # Today's games
GET /api/espn/games/all          # All games (multi-day)
POST /api/espn/ingest            # Manual ingest trigger
```

Used for: game schedules, box scores, injury reports (backup to Sportradar).

### Game Matching (`src/data/game_matcher.py`)

Critical component — matches OUTLIER scanner output (e.g., "Rockies/Dodgers") to Kalshi ticker (e.g., "KXMLBGAME-25-COL-LAD") and to ESPN game record.

Matching algorithm:
1. Fuzzy team name matching (handles "LA Dodgers" vs "Los Angeles Dodgers" vs "Dodgers")
2. Date + time window matching
3. Fallback to schedule-derived canonical team names

### Sports Adapters (`src/data/adapters/`)

| Adapter | League | Key Features Populated |
|---|---|---|
| `mlb_adapter.py` | MLB | Starting pitcher stats (FIP, xFIP, K/BB), batting (wOBA, xwOBA, ISO, barrel%), park factors |
| `nba_adapter.py` | NBA | Four factors (eFG%, TOV%, ORB%, FT%), Elo, pace, offensive/defensive rating |
| `nfl_adapter.py` | NFL | EPA, DVOA, QB CPOE/EPA, 3rd down %, red zone TD%, turnover differential |
| `nhl_adapter.py` | NHL | Corsi%, xGF%, PDO, PP/PK%, save %, shot rates |
| `ncaab_adapter.py` | NCAAB | KenPom metrics (Net, AdjO, AdjD, Tempo), conference, home court |

### Public Betting Data (`src/data/public_betting.py`)

```python
# Tables: public_betting_pcts, fade_public_log
# Sources: aggregated from available public betting databases
# Endpoints:
GET /api/public-betting/data
GET /api/public-betting/game
POST /api/public-betting/refresh
GET /api/public-betting/fade-opportunities
GET /api/public-betting/fade-stats
POST /api/public-betting/settle-fades
```

### Elo Engine (`src/data_feeds/elo_engine.py`)

```python
# Elo update rule:
new_elo = old_elo + K_FACTOR * (actual - expected)
expected = 1 / (1 + 10 ** ((opponent_elo - team_elo) / 400))
K_FACTOR = 20  # (varies by sport)

# Season reset: regress to mean
new_elo = (old_elo * 0.75) + (1500 * 0.25)  # example

# Tables: elo_ratings, elo_processed_games, elo_season_resets
```

### Line Movement Tracker (`src/data/line_movement.py`)

Monitors spread/moneyline changes over time:
- `line_snapshots` table — timestamped snapshots per market per book
- Steam move detection (rapid sharp-money-induced moves)
- Reverse line movement detection (public money going opposite to line)
- CLV snapshot at bet entry (`POST /api/clv/snapshot-closing`)

---

## Appendix M: Evaluation & CLV System

### CLV Service (`src/evaluation/clv_service.py`)

Three-component decomposition of every trade:

```
model_clv = entry_price - model_predicted_price
execution_clv = model_predicted_price - actual_fill_price
timing_clv = actual_fill_price - closing_line_price

total_clv = model_clv + execution_clv + timing_clv
```

**CLV auto-halt status:** SUPPRESSED per user policy. The endpoint `GET /api/trading/clv-summary` still shows trends.

**Endpoint:** `POST /api/clv/process` (trigger CLV computation for recent trades)

### Performance Monitor (`src/evaluation/monitoring/performance_monitor.py`)

Auto-pause decision logic:
```python
# Only counts Kalshi vertical trades (sports/weather/events)
# Excludes pnl=0 flat trades (prevents crypto micro-trades from blocking)

if effective_streak >= 8:  # 8 consecutive losses → pause
    set_halted(True)
elif effective_streak >= 5:  # 5 consecutive losses → reduce sizing
    reduce_kelly_fraction(0.5)
elif effective_daily_pnl <= -50.0:  # $50 daily loss → pause
    set_halted(True)
```

### A/B Testing (`src/evaluation/ab_tester.py`, `ab_testing_v2.py`)

```
Tables: ab_decisions, ab_outcomes, ab_tests

Flow:
1. Define A/B test: control config vs variant config
2. Route % of trades to variant (shadow or live)
3. Track outcomes for both groups
4. Statistical significance test (Bayesian)
5. Auto-promote variant if significant improvement
```

### Evaluation Reports

300+ evaluation reports in `models/evaluation_report_v*.json` from training runs.
**Rebuild action:** Archive to cold storage; keep only last 10.

### Correlation Engine (`src/evaluation/correlation_engine.py`)

```python
# Tables: correlation_snapshots

# Checks:
max_positions_per_league = config  # e.g. 3 NBA games simultaneously
max_sports_positions = 9999       # Total sports cap
max_positions_per_city = 1         # Weather: 1 per city per day
max_weather_positions = 5          # Total weather cap

# Commodity correlation pairs (11):
oil_nat_gas, corn_soybeans, corn_wheat, soybeans_wheat,
gold_silver, copper_nickel, coffee_sugar,
crude_natural_gas, wti_brent, ...
```

---

## Appendix N: Configuration File Schemas

Key config files with their primary parameters:

### `config/auto_trading_config.json`

```json
{
  "kelly_fraction": 0.175,
  "buying_power_pct": 100.0,
  "min_ev_pct": 13.5,
  "max_daily_trades": 9999,
  "max_daily_loss_pct": 3.0,
  "daily_loss_limit_enabled": true,
  "league_priorities": {
    "NBA": {"priority": 1, "allocation_pct": 35},
    "NHL": {"priority": 2, "allocation_pct": 30},
    "NCAAB": {"priority": 3, "allocation_pct": 10},
    "MLB": {"priority": 4, "allocation_pct": 10},
    "NFL": {"priority": 5, "allocation_pct": 5},
    "EPL": {"priority": 6, "allocation_pct": 3, "max_daily_trades": 10}
  },
  "swarm_llm": {
    "provider": "anthropic",
    "model_id": "claude-sonnet-4-6",
    "blend_weight": 0.6,
    "timeout_seconds": 45
  }
}
```

### `config/calibration-params.json`

```json
{
  "method": "isotonic",              // isotonic | platt
  "per_sport": true,
  "per_segment": true,
  "min_samples_calibrate": 30,
  "drift_threshold": 0.02,
  "stabilizer_smoothing": 0.3,
  "bakeoff_enabled": true,
  "bakeoff_min_samples": 50
}
```

### `config/bankroll_config.json`

```json
{
  "allocated_amount": 1000,          // STALE — real bankroll from Kalshi API
  "reserve_pct": 0.10,
  "sports_allocation_pct": 0.50,
  "weather_allocation_pct": 0.15,
  "crypto_allocation_pct": 0.25,
  "events_allocation_pct": 0.10,
  "swarm_daily_budget": 0            // 0 = unlimited
}
```

### `config/crypto_binary_config.json`

```json
{
  "paper_mode": false,               // NOTE: was flipped to live 2026-04-19
  "min_confidence": 70.0,
  "min_edge": 5.0,
  "min_EV_pct": 5.0,
  "posterior_dev_floor": 0.03,
  "max_trades_per_day": 100,
  "max_trades_per_hour": 15,
  "max_exposure": 150.0,
  "daily_loss_limit": 50.0,
  "circuit_breaker_losses": 5
}
```

### `config/autoresearch_config.json`

```json
{
  "experiment_interval_hours": 6,
  "min_settled_trades": 50,
  "exploration_rate": 0.3,
  "mutation_magnitude": 0.15,
  "auto_promote": true,
  "shadow_validation_enabled": true,
  "shadow_min_trades": 15,
  "shadow_brier_improvement_pct": 3.0,
  "max_regression_tolerance": 0.02,
  "significance_threshold": 0.15,
  "min_samples_for_promotion": 15,
  "bayesian_significance": true,
  "bayesian_prior_alpha": 1.0,
  "bayesian_prior_beta": 1.0,
  "bayesian_credible_threshold": 0.7,
  "momentum_search_enabled": true,
  "momentum_top_n": 3,
  "per_vertical_fitness": true,
  "smart_mutation_enabled": true,
  "max_random_mutations": 2,
  "hypothesis_ratio": 0.8,
  "hypothesis_generation_enabled": true
}
```

### `config/unified_improvement_config.json`

```json
{
  "orchestrator_interval_hours": 4,
  "brier_score_max": 0.35,
  "win_rate_min": 0.35,
  "edge_capture_min": 1.0,
  "roi_min": -0.15,
  "roi_7d_min": -500,
  "min_samples_for_degraded": 10,
  "weather_min_samples_for_degraded": 30
}
```

---

## Appendix O: Sample Cycle Log Walkthrough

From real production-mirror log (2026-04-21 15:37–15:42):

```
2026-04-21 15:37:33,901 [INFO] src.features.canonicalize:
  [canonicalize] Renamed 20 features:
    home_ortg → home_kenpom_adj_o
    away_ortg → away_kenpom_adj_o
    home_drtg → home_kenpom_adj_d
    home_orb_pct → home_orb_rate
    ... (15 more)
```
The canonicalize layer rewrites legacy column names to model-contract names. Without this step, the parity gate fails.

```
2026-04-21 15:37:33,951 [WARNING] unified_feature_builder:
  No valid game date for calendar features — defaulting to 0
```
Game-time-of-week features (`day_of_week`, `month`, `is_weekend`) cannot be computed for one row. Defaults to 0 with warning rather than aborting.

```
2026-04-21 15:37:34,001 [INFO] src.signals.sharp_money_tracker:
  [SignalGate] Suppressed 1415 signals from low-accuracy types:
    ['price_impact', 'steam_move', 'volume_spike']
```
Post-mortem-driven `SignalGate` dropping entire signal classes whose 30-day rolling accuracy fell below trust threshold. 1,415 candidate signals dropped before reaching swarm — large savings on LLM cost.

```
2026-04-21 15:37:34,052 [INFO] market_features:
  Computed market features for 0 games.
```
Zero NBA/NFL/MLB live games during this window (late April, NBA playoffs only). Crypto-binary and weather verticals carry the cycle.

```
2026-04-21 15:42:09,143 [INFO] src.ml_training.autoresearch_engine:
  AutoResearch: hypothesis efea5c19-c33 (crypto_binary) PASSED probation
2026-04-21 15:42:09,201 [INFO] src.ml_training.autoresearch_engine:
  AutoResearch: hypothesis 29076ef2-435 (crypto_binary) PASSED probation
... (~30 hypothesis IDs) ...
```
AutoResearch evaluated ~30 active crypto-binary hypothesis configurations. Each passed probation (live Brier ≤ historical Brier × 1.05).

### Cycle Throughput

| Segment | Typical Duration |
|---|---|
| Cycle start → SignalGate | ~150ms |
| Feature canonicalize | ~50ms |
| ML inference (single game) | ~20ms |
| Swarm debate (6 personas, batched) | ~2-5s |
| LLM gatekeeper | ~1-3s |
| Order submission (Kalshi API) | ~200-500ms |
| Full cycle (all verticals, no games) | ~30s |
| Full cycle (active NBA + weather + crypto) | ~90-120s |
| Loop sleep between cycles | ~120s (LOOP_INTERVAL_SEC) |

### Early Morning (4:43 AM) — Champion Promotion Log

```
2026-04-21 04:43:15 INFO: 4-window split:
  train=51839 (from 2022-06-29),
  select=10794 (from 2023-11-20),
  cal=8250 (from 2024-12-15),
  test=9924
2026-04-21 04:43:15 INFO: Full feature space: 698 features
2026-04-21 04:43:15 INFO: --- Phase 0b: Feature parity gate ---
2026-04-21 04:43:15 INFO: SSOT BUILDER ALIGNMENT:
  builder knows 311 features, contract has 698,
  overlap=285, missing=413 (40.8% coverage)
2026-04-21 04:43:15 INFO: Train-only (not in inference builder): 211
...
2026-04-21 04:48:06 INFO: Feature truth table generated
Regenerated FEATURE_REGISTRY.md:
  698 contract → 393 trainable → 145 selected (v155)
```

This log reveals an important detail: **only 285 of 698 features are present in both training AND the inference builder**. The parity gate drops features that can't be computed at inference time. This is the root cause of the 698→487→393→145 funnel.

**Rebuild action:** After wiring CatBoost, verify the parity gate output still produces 145 (or better) selected features.

---

## Appendix P: Key Operational Endpoints Reference

Complete set of frequently-used API endpoints for daily operations:

### Trading Control

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/trading/status` | Current loop status, cycle count, trades placed |
| POST | `/api/trading/start` | Start trading loop |
| POST | `/api/trading/stop` | Stop trading loop |
| POST | `/api/trading/resume` | Resume from halt |
| POST | `/api/kalshi/emergency-stop` | Emergency halt + cancel all orders |
| GET | `/api/trading/performance-monitor` | Auto-pause state |
| GET | `/api/circuit-breakers` | Circuit breaker status |
| GET | `/api/risk/status` | Risk gate status |
| GET | `/api/go-live/decision` | Paper-to-live readiness check |

### Settings & Config

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/settings` | All runtime settings |
| POST | `/api/settings` | Update settings |
| GET | `/api/trading/bet-toggles` | Per-league/vertical toggles |
| POST | `/api/trading/bet-toggles` | Update toggles |
| GET | `/api/bankroll/config` | Bankroll configuration |
| PUT | `/api/bankroll/config` | Update bankroll config |

### Kalshi Account

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/kalshi/account/balance` | Live balance |
| GET | `/api/kalshi/account/positions` | Open positions |
| GET | `/api/kalshi/account/fills` | Recent fills |
| GET | `/api/kalshi/account/orders` | Active orders |
| GET | `/api/kalshi/pnl` | P&L summary |
| POST | `/api/kalshi/sync-fills` | Force fill sync |
| POST | `/api/kalshi/repair-pnl` | Repair P&L attribution |

### ML & Calibration

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/calibration/status` | Per-vertical calibration state |
| POST | `/api/calibration/recalibrate` | Trigger recalibration |
| GET | `/api/calibration/drift` | Drift detection results |
| GET | `/api/monitoring/metric-ladders` | Graduation metric thresholds |
| GET | `/api/feature-integrity` | Feature truth table |
| GET | `/api/monitoring/performance-history` | Historical performance |

### Swarm & LLM

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/swarm/stats` | Swarm overall stats |
| GET | `/api/swarm/analyst-performance` | Per-analyst performance |
| GET | `/api/swarm/cost-breakdown` | LLM cost breakdown |
| GET | `/api/llm/status` | LLM provider status |
| GET | `/api/ollama/status` | Local Ollama status |

### Health & Watchdog

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | System health (public) |
| GET | `/api/health/all` | All service health |
| GET | `/api/watchdog/audit-log` | Daily audit history |
| POST | `/api/watchdog/run-audit` | Manual audit trigger |
| GET | `/api/risk-console` | Live risk gate status |
| GET | `/api/risk-console/monte-carlo` | Ruin probability |

---

## Appendix Q: Migration Extraction Guide

To generate `migrations/0001_initial.sql` from the 33 source files:

### Step 1: Extract All CREATE TABLE Statements

```bash
# Primary sources (by CREATE TABLE count):
grep -n "CREATE TABLE" src/api/unified_server.py      # 21 tables
grep -n "CREATE TABLE" src/ml_training/schema.py      # 15 tables
grep -n "CREATE TABLE" src/weather/model.py            # 6 tables
grep -n "CREATE TABLE" src/weather/settlement_model.py # 6 tables
grep -n "CREATE TABLE" src/crypto/binary_trading_engine.py  # 3 tables
grep -n "CREATE TABLE" src/crypto/data_pipeline.py     # 3 tables
grep -n "CREATE TABLE" src/shared/bankroll_manager.py  # 3 tables
grep -n "CREATE TABLE" src/shared/pnl_tracker.py       # 3 tables
grep -n "CREATE TABLE" src/features/feature_store.py   # 3 tables
grep -n "CREATE TABLE" src/weather/advanced_prediction.py  # 3 tables
# ... (remaining 23 files)
```

### Step 2: Deduplication Rules

```python
# Tables defined in BOTH unified_server.py AND server_core.py:
# Use unified_server.py as canonical (server_core.py will be deleted)
CANONICAL_SOURCE_PRIORITY = {
    "unified_server.py": 1,  # Highest priority
    "schema.py": 2,          # ml_training/schema.py
    "model.py": 3,           # weather/model.py
    "binary_trading_engine.py": 4,
    # ... others lower priority
}
```

### Step 3: Order Dependencies

```sql
-- Create in this order (dependencies):
CREATE TABLE users (...)       -- No dependencies
CREATE TABLE sessions (...)    -- FK → users
CREATE TABLE trades (...)      -- FK → users (optional)
CREATE TABLE positions (...)   -- FK → users (optional)
-- ... etc
```

### Step 4: Alembic Setup

```bash
pip install alembic
alembic init alembic/

# Edit alembic/env.py:
# from src.api.unified_server import metadata
# target_metadata = metadata

# Create initial migration:
# Copy migrations/0001_initial.sql content to alembic/versions/0001_initial_schema.py

# Stamp DB as migrated:
alembic stamp head
```

### Key Tables That Appear in Multiple Files (Deduplicate These)

| Table | Files Defining It | Canonical Source |
|---|---|---|
| `trades` | `unified_server.py`, `server_core.py` | `unified_server.py` |
| `positions` | `unified_server.py`, `server_core.py` | `unified_server.py` |
| `system_settings` | `unified_server.py`, `server_core.py` | `unified_server.py` |
| `crypto_binary_trades` | `binary_trading_engine.py`, `server_core.py` | `binary_trading_engine.py` |
| `weather_observations` | `model.py`, `server_core.py` | `model.py` |
| `calibration_snapshots` | `manager.py`, `unified_server.py` | `manager.py` |

---

## Appendix R: Scripts Directory — Active Scripts Reference

Of the 120+ scripts, these are the operationally important ones for the rebuild:

### Must Keep (Operational)

| Script | Purpose |
|---|---|
| `backend_watchdog.py` | Layer 2 process watchdog — always running |
| `emergency_stop.sh` | Kill switch — must work at all times |
| `train_ensemble_fast.py` | Triggered by Model Training workflow |
| `build_training_splits.py` | Build walk-forward train/val/test splits |
| `backfill_historical.py` | Backfill historical game data |
| `enrich_odds_injuries.py` | **Run this to fix 60K games with odds:{}** |
| `enrich_synthetic_elo_odds.py` | Synthetic Elo + odds enrichment |
| `paper_trading_runner.py` | Paper trading parallel track |
| `go_live_checklist.py` | Pre-live checklist validation |
| `generate_feature_truth_table.py` | Feature registry export |
| `load_test.py` | Backend load testing |
| `benchmark_latency.py` | Latency benchmarking |

### High Priority (Run During Rebuild)

| Script | When to Run |
|---|---|
| `backfill_model_prob.py` | After training — backfill model_prob on historical trades |
| `backfill_odds_api_scores.py` | After first Odds API connection |
| `backfill_referees.py` | Referee data for NFL/NBA features |
| `optimize_hyperparams.py` | After 3-model ensemble trains cleanly |
| `build_golden_dataset.py` | Build golden regression test set |
| `golden_regression.py` | Run regression suite before each release |

### Archive / Legacy (Move to `scripts/legacy/`)

All `capture-draftkings-*.{mjs,js}` scripts — legacy from OpenClaw era, superseded by live data pipeline.

All `axiom-llm-*.{mjs,sh}` scripts — internal LLM fine-tuning (not documented in SYSTEM.md; keep but document).

---

## Appendix S: UW MCP Cron Schedule

From `uw-mcp/src/pipelines/scheduler.ts`. All intervals configurable via `UW_POLL_*_INTERVAL_SECONDS` env vars.

| Pipeline | Default Interval | Purpose |
|---|---|---|
| Flow alerts | 30s | Unusual options flow |
| Premium alerts | 60s | Large options premium |
| Dark pool | 60s | Dark pool trades |
| IV surface | 120s | Implied volatility surface |
| Options volume | 120s | Options volume anomalies |
| GEX | 300s | Gamma exposure |
| Screener | 300s | Stock screener results |
| Congress monitor | 300s | Congressional trading disclosures |
| Technicals | 600s | Technical analysis signals |
| Insider activity | 600s | Insider trading disclosures |
| Earnings scanner | 3600s | Upcoming earnings + whisper |
| Financials | 3600s | Fundamental financial data |
| TIDE | 300s | TIDE flow model |
| Morning briefing | Daily (6:30 AM ET) | Full morning brief |
| Signal scan | Daily (7:00 AM ET) | Daily signal sweep |
| Deep dive | On-demand | Triggered by `POST /api/uw-mcp/query` |

Each pipeline:
1. Calls Unusual Whales API via MCP client
2. Extracts signals via `extractor.ts`
3. Stores to `uw_mcp_signals`, `uw_flow_alerts`, `uw_dark_pool`, etc.
4. Notifies Python backend via `axiom_bridge.ts`

---

## Appendix T: Glossary

| Term | Definition |
|---|---|
| **AGENT-FIX** | Code comment marker for unilateral agent fixes during R&D. 105 such fixes as of 2026-04-21. |
| **AutoResearch** | Continuous Bayesian-significance-tested model improvement. Generates hypotheses → tests in shadow → promotes if passing probation. |
| **Axiom Gravity** | Orbital visualization of live Kalshi positions (gravity-based layout in `AxiomGravity/` component). |
| **Bake-off** | Challenger calibrator/model run in shadow vs champion; promoted on Brier improvement. |
| **Brier score** | Mean squared error between predicted probability and binary outcome. Lower is better. Range: 0–1. |
| **CLV** | Closing Line Value. Diff between entry price and closing line. Proxy for long-term edge quality. |
| **Concierge** | LLM chat surface (Telegram + UI). Can answer questions, execute control actions, summarize daily P&L. |
| **Core-4** | NBA + NHL + MLB + NCAAB — the 4 deepest-trained sports verticals. Target: Brier < 0.18. |
| **Crypto binary** | Kalshi binary contracts on BTC price direction (KXBTC15M, 1H, daily). |
| **Dollar-era migration** | Kalshi API switched from cent-based (0–100) to dollar-based (0.0–1.0) price fields. |
| **ECE** | Expected Calibration Error. Average gap between predicted probability and empirical frequency. Target: < 0.025. |
| **Edge** | Model probability minus market implied probability. |
| **Fake coverage** | Feature is present in training data but ≥80% at its default (null/zero) value — uninformative. Filtered by fake-coverage gate. |
| **GTC** | Good-Til-Cancelled. Resting limit order that stays active until filled or explicitly cancelled. |
| **Health gate** | Per-trade pre-execution checks (calibration status, data freshness, model divergence, price divergence, feature completeness). |
| **Kelly** | Bet-sizing formula: fraction = edge / odds. Scales bet size proportionally to perceived edge. |
| **METAR** | Meteorological Aerodrome Report. Standard weather observation from airport stations — what Kalshi weather markets settle on. |
| **Online learner** | SGD `partial_fit` after settlements. Updates ensemble weights without full retrain. |
| **OUTLIER** | Outlier.bet API providing ~1,200 positive-EV bets per scan. Data source, not a vertical. |
| **Paper mode** | `AXIOM_PAPER_MODE=true`. Orders are simulated (not submitted to exchange). |
| **Persona** | One of 6 analysts in the swarm debate. Each has fixed expertise and system prompt. |
| **Pipeline tier** | RT (real-time, <1s latency), NRT (near-real-time, <60s), Batch (>60s). |
| **Posterior dev** | Deviation of calibrated probability from 0.50. Used to reject coin-flip trades (|posterior - 0.50| < 0.03 → skip). |
| **Parity gate** | Filters features present in training splits but not computable at inference. 698 → 487. |
| **Sizing scale** | Calibrator-driven multiplier on Kelly fraction. 0.0 = all trades blocked. 1.0 = full Kelly. |
| **Stacking ensemble** | Meta-model trained on out-of-fold predictions of base learners (LGB + XGB + CatBoost). |
| **Steam move** | Rapid sharp-money-induced line movement at a sharp book (Pinnacle, Betfair). |
| **Swarm** | 6-analyst LLM debate voting on each trade opportunity. Two rounds: independent verdicts + moderator synthesis. |
| **Vertical** | Top-level trading category (sports, weather, crypto, events, player props). |
| **Walk-forward** | Time-series-respecting model training: train on past, validate on future, never look ahead. |
| **Weber fraction** | VERIFY: Referenced in some weather config files. Likely a sensitivity threshold. |

---

*End of Axiom-Predict Rebuild Spec v1.0*
*Total sections: 31 main + 20 appendices*
*Target audience: Replit Agent rebuilding the system from scratch*
*Source documents: SYSTEM.md (3,765 lines) + rebuild-spec-audit.md (1,278 lines)*
*This document is self-contained and exhaustive. Items marked VERIFY require manual code inspection during rebuild.*
