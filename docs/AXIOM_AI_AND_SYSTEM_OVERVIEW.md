# Axiom Predict — Full System & AI Overview

> A complete reference: what the platform does, how it works end-to-end, and how every AI subsystem turns raw data into live prediction-market trades.

---

## Table of Contents

1. [What Axiom Is](#1-what-axiom-is)
2. [High-Level Architecture](#2-high-level-architecture)
3. [The Prediction Pipeline — From Data to Trade](#3-the-prediction-pipeline--from-data-to-trade)
4. [Vertical-by-Vertical AI Breakdown](#4-vertical-by-vertical-ai-breakdown)
   - 4.1 Sports (Pregame)
   - 4.2 Sports (Live / In-Game)
   - 4.3 Crypto
   - 4.4 Commodities
   - 4.5 Weather
   - 4.6 Elections
   - 4.7 Economic Events
   - 4.8 Entertainment
5. [Calibration Layer](#5-calibration-layer)
6. [The Swarm Debate & LLM Gatekeeper](#6-the-swarm-debate--llm-gatekeeper)
7. [AutoResearch / Feature Mining](#7-autoresearch--feature-mining)
8. [Model Training, Storage & Versioning](#8-model-training-storage--versioning)
9. [Backtesting & Drift Auto-Rollback](#9-backtesting--drift-auto-rollback)
10. [ELO Rating System](#10-elo-rating-system)
11. [Orchestrator & Trading Cycle](#11-orchestrator--trading-cycle)
12. [Edge Terminal — External Signal Ingestion](#12-edge-terminal--external-signal-ingestion)
13. [Bet Sizing (Kelly & Risk Gates)](#13-bet-sizing-kelly--risk-gates)
14. [Risk Management — All Gates](#14-risk-management--all-gates)
15. [Kalshi Order Lifecycle](#15-kalshi-order-lifecycle)
16. [Settlement & Outcome Resolution](#16-settlement--outcome-resolution)
17. [Data Feeds & External APIs](#17-data-feeds--external-apis)
18. [Web Dashboard](#18-web-dashboard)
19. [Mobile App](#19-mobile-app)
20. [Operator Console & Admin Controls](#20-operator-console--admin-controls)
21. [Billing, Auth & Subscriptions](#21-billing-auth--subscriptions)
22. [Key Environment Variables](#22-key-environment-variables)
23. [Lessons Learned](#23-lessons-learned)

---

## 1. What Axiom Is

Axiom Predict is a **prediction-market auto-trading platform**. Its core loop:

1. Ingest real-world data (sports scores, crypto prices, weather forecasts, economic releases, election polls, LLM-sourced signals, external tipster feeds).
2. Run per-vertical machine-learning models to produce a probability that a given Kalshi market resolves YES.
3. Compare that model probability to the Kalshi live market price to detect edge (where the market is mispriced).
4. Run a multi-agent LLM swarm debate on every candidate trade, then pass it through a 24-gate risk gauntlet.
5. Size the bet via fractional Kelly and place a limit or marketable order on Kalshi.
6. Track fills, manage open positions, settle resolved trades, and report P&L in real time.

The platform serves three audiences:
- **Operators / founders** — full access to the operator console, risk controls, kill switches, model health, and raw trading logs.
- **Paying subscribers** (pro / elite / founder tiers) — curated prediction feeds, swarm debate transcripts, leaderboards, and portfolio analytics.
- **Free-tier users** — limited predictions and public leaderboard views.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        AXIOM MONOREPO                               │
│                                                                     │
│  artifacts/api-server  (Python 3.11, FastAPI + asyncpg)             │
│  ├── Orchestrator / Trading Cycle                                   │
│  ├── Per-vertical ML Predictors                                     │
│  ├── Calibration Layer                                              │
│  ├── Swarm Debate (multi-agent LLM)                                 │
│  ├── LLM Gatekeeper (fail-closed)                                   │
│  ├── Risk Gate Gauntlet (24 gates)                                  │
│  ├── Kalshi API Client (REST + WebSocket)                           │
│  ├── Settlement Engine                                              │
│  ├── Data Feeds (ESPN, Sportradar, AccuWeather, OddsAPI, etc.)      │
│  ├── Edge Terminal (external tipster signal ingest)                 │
│  └── AutoResearch / Feature Mining                                  │
│                                                                     │
│  artifacts/web         (React + Vite, Clerk auth, TanStack Query)   │
│  └── Operator console, subscriber dashboard, live feed              │
│                                                                     │
│  artifacts/mobile      (Expo React Native, RevenueCat)              │
│  └── Subscriber mobile app, push notifications                      │
│                                                                     │
│  lib/api-spec          (OpenAPI 3.1 single source of truth)         │
│  lib/api-client-react  (Orval-generated React Query hooks)          │
│  lib/api-zod           (Orval-generated Zod schemas)                │
│  lib/db                (Drizzle ORM schema)                         │
└─────────────────────────────────────────────────────────────────────┘
          │                              │
          ▼                              ▼
   Supabase Postgres             Supabase Storage
   (primary DB via asyncpg)    (model artifacts, archives)
          │
          ▼
   Kalshi REST + WebSocket API
   (order placement, fills, orderbook)
```

**Key tech choices:**
| Layer | Tech |
|---|---|
| Backend | Python 3.11, FastAPI, asyncpg, httpx |
| Frontend | React 18, Vite, TanStack Query, Tailwind, shadcn/ui |
| Mobile | Expo (React Native), RevenueCat |
| Database | Supabase Postgres |
| Auth | Clerk (JWT, webhooks) |
| Billing | Stripe (web) + RevenueCat (mobile) |
| ML | scikit-learn, LightGBM, XGBoost, PyTorch (GRU), numpy |
| LLM | Anthropic Claude, OpenAI GPT, Google Gemini, Ollama (local) |
| Market | Kalshi prediction exchange |

---

## 3. The Prediction Pipeline — From Data to Trade

Every trade follows this exact path:

```
Feed Ingest → Feature Build → ML Predict → Calibrate → Edge Check
     ↓
Candidate Pool → Swarm Debate (LLM analysts) → LLM Gatekeeper
     ↓
Risk Gate Gauntlet (24 gates) → Kelly Sizing → Slippage Guard
     ↓
Kalshi Order Submit → Fill Tracking → Settlement → P&L Record
```

### Step-by-step

**1. Feed Ingest**
Data arrives via scheduled feed runners (`app/feeds/scheduler.py`). Each feed upserts its source rows into Supabase (scores, prices, odds, polls, weather). Feeds run on independent schedules (some every 30s, some hourly).

**2. Feature Build**
`app/features/builders.py` and `app/features/vector.py` assemble per-candidate feature vectors. Each vertical has its own feature schema. Context files (`nfl_context.py`, `mlb_context.py`, `soccer_context.py`, etc.) supply team strength, ELO, historical stats.

**3. ML Prediction**
The vertical's predictor (`app/crypto/predictor.py`, `app/weather/predictor.py`, etc.) runs `predict_proba` on the feature vector and returns a raw probability `p_model ∈ [0, 1]`.

**4. Calibration**
`app/calibrators.py` applies `apply_both()`: first a league-specific isotonic or Platt calibration fitted on historical trade outcomes, then a market-prior blend. This converts raw model probability into a calibrated prediction probability `p_cal`.

**5. Edge Calculation**
```
edge_pp = (p_cal - p_market) × 100   # in percentage points
```
Only candidates with `edge_pp ≥ min_edge_pp` (operator-configurable, default ~3pp) advance.

**6. Swarm Debate**
Up to N LLM analyst personas debate the candidate. Each takes a YES/NO stance, provides reasoning, and votes. A moderator synthesizes. Metrics extracted: agreement rate, confidence band, Risk Guardian stance.

**7. LLM Gatekeeper**
A separate Claude Sonnet instance (temperature 0.2) reads the moderator synthesis and applies strict approval criteria. Fail-closed: any JSON parsing error, schema violation, or provider failure forces `approved=False`. Only a valid boolean `true` with action `BUY_YES` or `BUY_NO` advances the candidate.

**8. Risk Gate Gauntlet (24 gates)**
Sequential gate evaluation. Any FAIL rejects the candidate and records the veto reason. Gates cover: position sizing, concentration, drawdown, model health, market price bounds, tail risk, daily loss, kill switch, vertical halt, duplicate trade, and more. (See §14 for all 24.)

**9. Kelly Sizing**
`app/risk/kelly.py` computes fractional Kelly bet size:
```
f* = (p × b - q) / b    where b = (100 - price) / price (odds ratio)
f_actual = f* × kelly_fraction (default 0.25)
qty = floor(f_actual × bankroll / price_cents)
```
Subject to per-trade and per-vertical caps.

**10. Slippage Guard**
Live orderbook depth is walked to estimate fill slippage. If slippage would erase more than `AXIOM_SLIPPAGE_GUARD_MAX_EDGE_PCT` (default 50%) of model edge, the order is either sliced (FOK) or rested (GTC) or rejected.

**11. Order Placement**
Kalshi limit order submitted via `app/kalshi/lifecycle.py`. A Kalshi WebSocket listener (`app/kalshi/ws.py`) captures fill events and updates the `trades` table in real time.

**12. Settlement**
`app/settlement/` resolves market outcomes by comparing Kalshi settlement data to trades. Resolved P&L is written to `trades.pnl` and `bankroll_events`.

---

## 4. Vertical-by-Vertical AI Breakdown

### 4.1 Sports — Pregame

**Leagues:** NBA, NHL, MLB, NFL, NCAAB, NCAAF, soccer (EPL, La Liga, Serie A, Bundesliga, Ligue 1, MLS, UCL), UFC, ATP/WTA tennis, F1, PGA golf. Player props: NBA, NHL, MLB, NFL, WNBA.

**Feature vector construction:**
- Team/player ELO ratings (`app/elo/`)
- Historical head-to-head metrics
- Rest days, home/away indicator
- Moneyline implied probability from market odds (as a feature, not the prediction)
- League-specific context (NFL pass rates, MLB starter ERA, NBA pace, PGA course history)
- Line movement signals from odds feeds
- Public betting percentages (where available)

**Model:** A generic meta-model (`app/predictor.py`) loaded from the Supabase artifact catalog. Architecture is ensemble (LightGBM + XGBoost stack); trained on historical Kalshi-settled outcomes. `predict_proba` returns YES class probability.

**Props markets:** Player prop predictors for each supported league use per-stat feature vectors (season averages, recent form, opponent splits, pace factors, injury news). Separate model per stat type (points, assists, rebounds, etc.).

**Calibration:** Per-league isotonic regression fitted on `v1_archive.trade_analyses` (667,092 historical trades).

---

### 4.2 Sports — Live / In-Game

**Files:** `app/learning/{nba,nfl,nhl,mlb,ncaab,ncaaf,soccer}_live_features.py`, `app/learning/{nba,nhl,soccer}_live.py`

**Soccer live model** (most sophisticated):
- Poisson/Dixon-Coles closed-form leg probabilities
- Current score, time elapsed, xG (expected goals), game state
- Market-direction blend between pregame prior and live state
- Recency-weighted xG smoothing
- Then SOCCER calibrator

**NBA/NFL/NHL/MLB/NCAAB/NCAAF live:**
- Game state features: current score differential, time remaining, possession, fouls/penalties, momentum indicators
- Feed from ESPN live scoreboard (`app/feeds/espn_scoreboard.py`, `app/feeds/public_live_state.py`)
- Runs through generic predictor with live feature vector appended
- `app/learning/in_game_loop.py` manages the polling cadence and live candidate generation
- `app/learning/shadow_predictor.py` runs a parallel shadow model for validation without affecting live sizing

---

### 4.3 Crypto

**Files:** `app/crypto/predictor.py`, `app/crypto/features.py`, `app/crypto/models.py`, `app/crypto/tickers.py`

**Feature vector (FEATURE_NAMES in crypto/features.py):**
| Feature | Description |
|---|---|
| Returns 5m/15m/1h | Log returns over each timeframe |
| Rolling volatility | 5-bar and 20-bar rolling std of returns |
| RSI (14) | Relative Strength Index |
| OBV | On-Balance Volume |
| Ticker threshold | Distance from Kalshi market strike |
| Direction | Above/below strike (binary) |
| Timeframe index | 5m=0, 15m=1, 1h=2 |

**Models:**
- **All tickers except BTC:** `BaselineLGBXGBStack` — LightGBM + XGBoost ensemble, timeframe-specific, trained artifact or fallback baseline.
- **BTC (5m and 15m):** Blend of `BaselineLGBXGBStack` (65%) + `BaselineGRUDirection` (35%), a directional GRU neural network.

**BTC blend formula:**
```python
p_final = 0.65 * p_stack + 0.35 * p_gru
```

**Calibration:** CRYPTO isotonic calibrator (n=2,289 historical trades, Brier 0.182).

**OHLCV data source:** `app/feeds/crypto.py` pulls from CoinGecko / CoinAPI on a per-ticker schedule; stored in `crypto_ohlcv` table with 90-day retention.

---

### 4.4 Commodities

**Files:** `app/commodities/features.py`, `app/commodities/models.py`, `app/commodities/predictor.py`, `app/commodities/training.py`, `app/commodities/tickers.py`, `app/commodities/correlations.py`

**Feature vector (12 features):**
| Feature | Description |
|---|---|
| Last price | Current spot price |
| Return 1-bar | Single-period log return |
| Return 5-bar | 5-period log return |
| Return 10-bar | 10-period log return |
| Volatility 5 | 5-bar rolling standard deviation |
| Volatility 20 | 20-bar rolling standard deviation |
| RSI (14) | Relative Strength Index |
| OBV z-score | On-Balance Volume normalized |
| Seasonality | Centered day-of-year sinusoid |
| log(K/price) | Log moneyness (strike vs. spot) |
| Above/below | Binary direction indicator |
| Timeframe index | 1h=0, 4h=1, 1d=2 |

**Model:** `BaselineLGBXGBStack` (timeframe-specific). Predict YES from `predict_proba` or clamped `predict`.

**Calibration:** COMMODITIES Platt calibrator (n=10 trades — thin, uses Platt because isotonic requires more samples).

**Correlation feature:** `app/commodities/correlations.py` adds inter-commodity correlation signals (e.g., Gold/Silver, Oil/Natural Gas) to the feature vector for related-market context.

---

### 4.5 Weather

**Files:** `app/weather/predictor.py`, `app/weather/gru.py`, `app/weather/emos.py`, `app/weather/precip_predictor.py`, `app/weather/precip_gru.py`, `app/weather/precip_emos.py`, `app/feeds/weather_payload.py`, `app/feeds/weather.py`, `app/feeds/weather_climatology.py`

**Temperature prediction (multi-source ensemble):**

1. **Payload assembly** (`weather_payload.py`): Pulls from Open-Meteo, NWS, HRRR, Tomorrow.io, and climatology. Produces a unified 12-step temporal trace with:
   - t2m, tmax, tmin at each forecast step
   - Current observation temperature
   - Climatological normal (baseline)
   - Normalized hour-of-day and day-of-year features

2. **GRU temporal model** (`gru.py`): Pure numpy GRU (no PyTorch at inference for portability). Reads the 12-step trace and outputs a bias correction and spread adjustment for the distribution.

3. **EMOS (Ensemble Model Output Statistics)** (`emos.py`): Combines the multi-source point forecasts using learned linear coefficients into a Normal predictive distribution N(μ, σ²). The threshold from the Kalshi market (e.g., "Will the high be above 85°F?") is evaluated as a tail CDF:
   ```python
   p_yes = 1 - Normal(μ, σ).cdf(threshold)
   ```

4. **Calibration:** WEATHER isotonic or Platt calibrator applied to the raw EMOS probability.

**Precipitation prediction:** Separate parallel pipeline (`precip_predictor.py`, `precip_gru.py`, `precip_emos.py`) using precipitation-specific HRRR fields (APCP accumulation, convective cape, relative humidity). Same GRU+EMOS pattern but with precipitation-appropriate distributions.

---

### 4.6 Elections

**Files:** `app/elections/predictor.py`, `app/elections/model.py`, `app/elections/polls.py`, `app/elections/arb.py`, `app/elections/tickers.py`

**Poll-of-polls model:**
1. Gather recent polls from the feed (`app/feeds/election_polls.py`)
2. Weight each poll by:
   - `sqrt(sample_size)` — larger polls count more but not linearly
   - 14-day exponential half-life — recency weighting
3. Apply state-level historical bias corrections
4. Optionally blend in Polymarket market price as a 30% log-odds nudge:
   ```python
   logit_final = 0.7 * logit_polls + 0.3 * logit_polymarket
   ```
5. Apply logistic transformation centered at 50%, clamped to [0.05, 0.95]

**Calibration:** ELECTIONS isotonic calibrator.

**Arbitrage detection** (`arb.py`): Detects cross-platform pricing discrepancies between Kalshi and Polymarket election markets for additional signal.

---

### 4.7 Economic Events

**Files:** `app/events_econ/predictor.py`, `app/events_econ/model.py`, `app/events_econ/calendar.py`, `app/events_econ/tickers.py`

**Statistical release model:**

1. **Consensus prior:** Wall Street consensus estimate from the economic calendar feed (`app/feeds/econ_calendar.py`). Falls back to the previous print if consensus is unavailable.

2. **Ticker parsing:** Parses the Kalshi ticker for threshold (e.g., "Will CPI be above 3.2%?") and direction (above/below). Special handling for FOMC (basis point hike/cut targets).

3. **Forecast-error distribution:** Rolling historical forecast errors by release type (CPI, PPI, GDP, NFP, etc.) define a per-release Normal distribution of surprises N(0, σ_type). Defaults exist for each major release type if history is thin.

4. **Tail probability:**
   ```python
   p_yes = 1 - Normal(consensus, σ).cdf(threshold)   # for "above" markets
   p_yes = Normal(consensus, σ).cdf(threshold)        # for "below" markets
   ```
   Clamped to [0.05, 0.95].

**Calibration:** EVENTS_ECON Platt calibrator (n=19 trades — uses Platt due to thin data).

---

### 4.8 Entertainment

**Files:** `app/entertainment/predictor.py`, `app/entertainment/model.py`, `app/entertainment/signals.py`, `app/entertainment/backfill.py`, `app/entertainment/promotion.py`, `app/entertainment/tickers.py`

**Signal sources:**
- Social sentiment signals (`app/feeds/entertainment_signals.py`)
- Award show polling aggregators
- Box office tracking (for film-related markets)
- Streaming chart data

**Model:** Logistic regression or LightGBM on the signal vector, depending on data availability. Falls back to a market-prior model when signals are sparse.

---

## 5. Calibration Layer

**File:** `app/calibrators.py`

Calibration corrects systematic over/under-confidence in raw model probabilities. Without calibration, a model might say 80% when the true frequency is 65%.

### Calibration methods used

| Method | Used when | Why |
|---|---|---|
| **Isotonic regression** | n ≥ ~50 samples | Non-parametric, monotone, fits arbitrary shapes |
| **Platt scaling** | n < 50 samples | Parametric logistic fit, more stable at low n |
| **Market prior blend** | Always (second pass) | Prevents extreme predictions far from market consensus |

### Per-league calibrators (fitted on v1 archive — 667K trades)

| League | n | Method | Brier Score |
|---|---|---|---|
| NBA | 3,158 | Isotonic | 0.109 |
| NCAAB | 1,356 | Isotonic | 0.169 |
| CRYPTO | 2,289 | Isotonic | 0.182 |
| NHL | 300 | Isotonic | 0.123 |
| MLB | 165 | Isotonic | 0.123 |
| PGA | 52 | Isotonic | 0.068 |
| EVENTS_ECON | 19 | Platt | — |
| COMMODITIES | 10 | Platt | — |

**`apply_both()`** runs the league-specific calibrator first, then applies a market-prior blend that smooths predictions toward the observed market base rate. This is the function called at the end of every vertical's prediction flow.

**Refitting:** `calibrators.refit_all()` re-fits all calibrators from `v1_archive.trade_analyses`. Run after loading new historical data.

---

## 6. The Swarm Debate & LLM Gatekeeper

**Files:** `app/swarm/personas.py`, `app/swarm/runner.py`

### The Swarm Debate

Before any trade is placed, the candidate goes to a **multi-agent LLM debate**. Personas are defined in `app/swarm/personas.py`.

**Persona types:**
| Persona | Role |
|---|---|
| Quantitative Analyst | Evaluates statistical edge and model confidence |
| Market Skeptic | Argues against the trade, finds holes |
| Risk Guardian | Hard veto authority on risk and sizing |
| Domain Expert | Sport/crypto/weather domain knowledge |
| Moderator | Synthesizes all views into a unified verdict |

**Debate flow:**
1. Each analyst receives: candidate market, model probability, calibrated probability, edge in pp, recent form, market context.
2. Each produces: stance (YES/NO/UNCERTAIN), confidence, reasoning.
3. Moderator synthesizes across all analysts into:
   - Agreement rate (fraction agreeing with the trade direction)
   - Confidence band (spread of individual confidences)
   - Risk Guardian stance (PASS/VETO — special weight)
   - Recommended action

**LLM providers:** Claude Sonnet (primary), GPT-5 (secondary), Gemini 2.5 Pro (tertiary). Mode controlled by `AXIOM_LLM_MODE`:
- `cloud` — uses hosted APIs
- `local` — routes all personas through local Ollama reasoning model
- `hybrid` — Ollama primary, hosted fallback

### The LLM Gatekeeper

A **separate final checkpoint** after the swarm debate. This is not one of the debate personas — it is an independent reviewer.

**Model:** Anthropic Claude Sonnet 4.5 at temperature 0.2 (low temperature for consistency).

**Input:** Candidate info + moderator synthesis + aggression mode + persona votes.

**Decision criteria (default mode):**
| Criterion | Threshold | Significance |
|---|---|---|
| Edge (pp) | ≥ 3pp | Must have genuine pricing inefficiency |
| Agreement rate | ≥ 0.50 | Majority of personas must agree |
| Confidence band | ≤ 0.30 | Personas must have aligned confidence levels |
| Risk Guardian | PASS | Must not veto |
| Moderator | PASS | Synthesis must recommend |

**Mode adjustments:**
- `conservative`: edge ≥ 5pp, agreement ≥ 0.60, band ≤ 0.20
- `aggressive`: edge ≥ 2pp, agreement ≥ 0.40, band ≤ 0.35, requires second PASS on Risk Guardian rule

**Fail-closed guarantees:**
- Missing `approved` field → `approved=False`
- Wrong type for `approved` → `approved=False`
- Unknown action value → `approved=False`, action forced to `PASS`
- Any JSON parse error → `approved=False`
- Provider exception (after fallback exhausted) → `approved=False`

**The gatekeeper never delegates to the moderator as a fallback.** If it cannot produce a valid approval, it vetoes.

Verdict persisted to `swarm_debates` and `llm_predictions` tables. WebSocket event `gatekeeper_done` streamed with `failed_closed` flag.

---

## 7. AutoResearch / Feature Mining

**Files:** `app/autoresearch/mining.py`, `app/autoresearch/scheduler.py`, `app/autoresearch/store.py`, `app/autoresearch/stats.py`, `app/autoresearch/feature_bridge.py`

AutoResearch is a background ML feature discovery system. It continuously searches for new predictive signals by:

1. **Mining** historical trade data for feature correlations with outcomes
2. **Testing** candidate features for predictive power (information gain, Brier improvement)
3. **Storing** validated features in `autoresearch_features` (Supabase, 90-day retention)
4. **Bridging** approved features into the live feature pipeline via `feature_bridge.py`

The scheduler (`autoresearch/scheduler.py`) runs mining jobs on a configurable cadence. The stats module tracks feature health (coverage, decay, lift). Features that lose predictive power are automatically retired.

---

## 8. Model Training, Storage & Versioning

### Training entry points

| Vertical | Training script / module |
|---|---|
| Sports (generic) | `app/sports/training.py`, orchestrated by model manager |
| Crypto | `app/crypto/trained_models.py`, `app/crypto/models.py` |
| Commodities | `app/commodities/training.py` |
| Weather GRU | `app/weather/gru.py` (fit method) |
| Weather EMOS | `app/weather/emos.py` (fit method) |

### Model storage

- **Location:** Supabase Storage bucket `axiom-predict-artifacts`, path `exports/`
- **Format:** Pickled sklearn/LightGBM/XGBoost objects + numpy GRU weights
- **Manifest:** `models_manifest.json` tracks model versions, checksums, predictor count
- **Current bundle:** v155 (includes all verticals, 8 league calibrators)
- **Loader:** `app/models_loader.py` — priority order: saved meta_model → latest versioned → archive fallback

### Model versioning

Each model version has:
- A version number
- A Brier score snapshot at training time
- A training date
- A checksum (verified by `verify_v155_artifacts.py`)

---

## 9. Backtesting & Drift Auto-Rollback

### Backtesting

**Files:** `app/crypto/backtest.py`, `app/crypto/backtest_scheduler.py`, `app/crypto/accuracy_report.py`, `app/features/drift.py`

- Per-vertical backtests run on historical Kalshi-settled data
- Crypto backtest scheduler (`backtest_scheduler.py`) runs on configurable cadence
- `accuracy_report.py` generates per-timeframe accuracy, calibration plots, Brier scores
- Backtest freshness is tracked and exposed in the operator console

**Surrogate backtest:** A lightweight "shadow" backtest that runs faster than the full pipeline, used for daily freshness checks.

### Drift auto-rollback

**Mechanism:**
1. After any model retraining, the new model's Brier score is compared to the previous version's
2. If the Brier score degrades beyond `DRIFT_TOLERANCE` (operator-configurable), the system automatically rolls back to the previous model version
3. Rollback is logged to `model_rollback_log` table
4. An operator alert fires with the degradation delta
5. The feature drift detector (`app/features/drift.py`) continuously monitors live prediction distributions against training baselines and alerts on distribution shift

**Drift queue:** Backlogs of drift candidates are managed and exposed in the operator console with severity ratings.

---

## 10. ELO Rating System

**Files:** `app/elo/scheduler.py`, `app/elo/__init__.py`, `app/feeds/team_elo_seed.py`

Axiom maintains a custom ELO rating for every team across all supported sports leagues. ELO ratings are:
- Seeded from historical win/loss records (`team_elo_seed.py`)
- Updated after every game result
- Sport-specific K-factors (NFL has higher variance than NBA, so higher K)
- Used as features in the sports pregame predictor

The ELO scheduler (`app/elo/scheduler.py`) keeps ratings current as game results come in from ESPN and Sportradar feeds.

---

## 11. Orchestrator & Trading Cycle

**Files:** `app/orchestrator/cycle.py`, `app/orchestrator/features.py`, `app/verticals.py`

The orchestrator is the main trading loop. Every cycle:

1. **Candidate generation:** For each active vertical, the vertical spec (`app/verticals.py`) defines which Kalshi markets are active and callable. The feature builder assembles a feature vector for each.

2. **Prediction + edge screen:** Each candidate gets a model probability and calibrated probability. Candidates below `min_edge_pp` are dropped.

3. **Position deduplication:** Candidates for markets where a position already exists are skipped (unless re-arm logic applies).

4. **Swarm debate queue:** Surviving candidates are sent to the swarm debate pool. Debate results return asynchronously.

5. **Gatekeeper check:** The LLM gatekeeper makes the final approval decision on each debate-resolved candidate.

6. **Risk gauntlet:** All 24 risk gates are checked in sequence.

7. **Order submission:** Approved, sized candidates go to `app/kalshi/lifecycle.py` for order placement.

**Cycle frequency:** Configurable per vertical. Sports may run every 5–15 minutes during active games; crypto runs every 1–5 minutes; weather and econ run on feed update events.

**Input modes:**
- `pipeline` — orchestrator runs its own prediction cycle for all active verticals
- `edge_terminal` — orchestrator defers to external tipster signals; its own prediction loop is paused for those verticals
- `AXIOM_PIPELINE_VERTICALS` — can run one vertical in pipeline mode while others are in edge_terminal mode

---

## 12. Edge Terminal — External Signal Ingestion

**Files:** `app/edge_terminal/poller.py`, `app/edge_terminal/scraper.py`, `app/edge_terminal/executor.py`, `app/edge_terminal/market_resolver.py`, `app/edge_terminal/llm_resolver.py`, `app/edge_terminal/control.py`

The Edge Terminal ingests signals from external tipster services (e.g., OutlierBet, SportsLine, BettingPros) and executes trades on their behalf, using Axiom's risk layer and Kalshi integration.

### Flow

```
External Tipster → Scraper (Playwright) → Signal Parser → Market Resolver
    → Re-arm Check → Risk Gates → Kelly Sizing → Kalshi Order
```

**Key details:**
- `poller.py`: Main async loop. **Every tick is bounded by `asyncio.wait_for`** to prevent wedging on Playwright timeouts. Tracks `last_poll_at` for liveness monitoring.
- `scraper.py`: Playwright-based scraper for tipster sites requiring browser automation.
- `market_resolver.py`: Maps tipster pick (team name, sport, bet type) to the correct Kalshi market ticker. Uses `resolve_match()` (not `resolve()` — see lessons).
- `llm_resolver.py`: When rule-based resolution fails, an LLM (Claude/GPT) attempts to identify the correct market from a fuzzy match.
- `executor.py`: Takes a resolved signal and runs it through the full risk gate gauntlet before placing.

**Signal confidence floor:** Signals below 60% model confidence are skipped. This is the most common reason signals don't fire, not a bug.

**Re-arm logic:** The `ON CONFLICT` path in signal storage re-arms only `skipped_unresolved` signals — never `placed` (would double-bet) or `skipped_gate` (market already settled).

**Soccer/spread edge cases:**
- Soccer "+/-0.5" markets route to the winner market (no half-goal spread exists on Kalshi)
- Team codes are FIFA codes, not first-three-letters abbreviations
- Model probability is pick-relative (YES = pick wins) — never flip the probability for NO-side bets

---

## 13. Bet Sizing (Kelly & Risk Gates)

**File:** `app/risk/kelly.py`, `app/risk/gates.py`

### Fractional Kelly

```
b = (100 - price_cents) / price_cents    # decimal odds ratio
f* = (p_cal × b - (1 - p_cal)) / b       # full Kelly fraction
f_actual = min(f* × kelly_fraction, kelly_max_fraction)
qty = floor(f_actual × bankroll / price_cents)
```

Default `kelly_fraction` = 0.25 (quarter-Kelly). Operator-adjustable at runtime.

### Sizing caps (applied in order)
1. Per-trade max quantity (`max_trade_qty`)
2. Per-vertical max notional exposure cap (`per_vertical_cap_pct × bankroll`)
3. Global max exposure cap (`global_exposure_cap_pct × bankroll`)
4. Tail-price size cap (reduced Kelly for OTM markets — `tail_price_size_cap` gate)
5. Jumbo quantity cap (`max_qty = jumbo_quantity_cap` regardless of Kelly)
6. Canary/ramp sizing: first 5 live trades forced to qty=1; then tiered ramp (5→20 qty over settled trade milestones)

---

## 14. Risk Management — All Gates

**File:** `app/risk/gates.py`

All 24 gates run in sequence. Any FAIL rejects the candidate.

| # | Gate | What it checks | Default threshold |
|---|---|---|---|
| 1 | `kill_switch` | Process-wide halt active | Halt = reject |
| 2 | `trading_mode` | DB trading mode = halt | Halt = reject |
| 3 | `vertical_halted` | Vertical-specific halt flag | Present = reject |
| 4 | `daily_loss` | Settled + unrealized P&L vs. daily loss limit | Configurable USD + % |
| 5 | `bankroll_floor` | Bankroll above minimum floor | `bankroll_floor_usd` |
| 6 | `uncalibrated_vertical` | Vertical has a fitted calibrator | Missing = reject |
| 7 | `model_health` | Model health alerts not critical | Critical = reject |
| 8 | `market_price_bounds` | Market price within tradeable range | [0.02, 0.98] |
| 9 | `min_edge` | Edge ≥ min threshold | `min_edge_pp` (pp) |
| 10 | `max_confidence_band` | Swarm confidence spread ≤ max | `max_confidence_band` |
| 11 | `min_agreement` | Swarm agreement rate ≥ min | `min_agreement_rate` |
| 12 | `tail_price_size_cap` | OTM tail bets: notional and max loss | `max_tail_notional_usd`, `max_tail_loss_usd` |
| 13 | `jumbo_quantity_cap` | Quantity below jumbo cap | `jumbo_quantity_cap` (default 999) |
| 14 | `sharpe_ratio` | Rolling Sharpe ≥ min | `sharpe_min_ratio` over last N trades |
| 15 | `ruin_monte_carlo` | Monte Carlo ruin probability ≤ max | `ruin_max_probability` over horizon |
| 16 | `segment_max_losses` | Consecutive losses in window ≤ max | `segment_max_losses` in `window_hours` |
| 17 | `global_exposure` | Total open notional ≤ global cap | `global_exposure_cap_pct × bankroll` |
| 18 | `vertical_exposure` | Vertical open notional ≤ vertical cap | `per_vertical_cap_pct × bankroll` |
| 19 | `max_concurrent_positions` | Open position count ≤ max | 25 |
| 20 | `best_line` | Kalshi price is at least as good as best available line | Cross-venue comparison |
| 21 | `canary` | Canary/ramp mode sizing check | Fail-closed to smallest cap |
| 22 | `duplicate_trade` | No identical (market, side, date) trade already placed | DB unique check |
| 23 | `slippage_guard` | Slippage ≤ max edge erasure % | 50% of model edge |
| 24 | `gatekeeper` | LLM gatekeeper approved | `approved=True` required |

**All gate trips are persisted** to `risk_gate_trips` (market, gate name, veto reason, timestamp). Tail gates send operator alerts with 6-hour deduplication.

---

## 15. Kalshi Order Lifecycle

**Files:** `app/kalshi/lifecycle.py`, `app/kalshi/client.py`, `app/kalshi/ws.py`, `app/kalshi/orderbook.py`, `app/kalshi/auth.py`, `app/kalshi/bus.py`

### Order types supported

| Type | Behavior |
|---|---|
| **GTC limit** | Rests at limit price until filled or cancelled |
| **Marketable FOK** | Sized down to immediately fillable depth (IOC emulation — Kalshi has no native IOC), takes partial fills, never rests |
| **Canary** | qty=1 GTC, used during ramp period |

**Marketable FOK flow:**
1. Walk live orderbook to find fillable depth at or better than limit price
2. Size order down to that depth
3. Submit as FOK — if the book moved, the order is cancelled (not rested)
4. Result: partial fills are accepted; the order never rests as a GTC

### Slippage check (pre-submit)
1. Fetch fresh orderbook from WebSocket cache
2. One-shot REST fallback if WS cache is stale
3. Walk book to compute `edge_erased_pct`
4. If `edge_erased_pct > threshold`: slice (FOK) or rest (GTC) or reject

### Fill tracking
- WebSocket listener (`app/kalshi/ws.py`) receives `order_fill` events in real time
- Fills written to `trades` table (qty, price, side, market_key, timestamp, placed_by)
- `app/kalshi/reconciler.py` runs periodic reconciliation against Kalshi's REST API to catch any missed fills
- `app/kalshi/tick_cache.py` and `app/kalshi/tick_cache_refresh.py` maintain a local cache of Kalshi market ticks for orderbook construction
- `app/kalshi/dead_letter_sweeper.py` recovers orders stuck in terminal-but-unrecorded states

### Kalshi market resolution
- `market_not_found` (404) on a closed/settled ticker = **clean skip** (not a failure), recorded as `skipped_unresolved`
- Settled/wrong-ticker 404s never trip alerts or count as failures

---

## 16. Settlement & Outcome Resolution

**File:** `app/settlement/` (multiple files), `app/kalshi/reconciler.py`

Settlement resolves whether a prediction was correct and books P&L.

**Flow:**
1. Kalshi settles a market → WebSocket `market_settled` event received
2. Settlement handler looks up all open trades for that market
3. Determines outcome (YES won or NO won) from Kalshi settlement data
4. Computes P&L:
   ```
   YES win:  P&L = qty × (100 - price_cents) / 100
   YES loss: P&L = qty × -price_cents / 100
   ```
5. Writes `pnl` to `trades` table
6. Writes a `bankroll_events` row (type=settlement, delta=P&L)
7. Updates rolling calibrator stats

**Day-end email:** Sent nightly. Includes:
- Trades placed today (all, including unsettled live trades — not just settled)
- Market, vertical, side, qty, price, mode, source
- Degrades to "no trades placed today" on quiet days

**Inferred winners:** When Kalshi settlement data is delayed, the system can infer outcomes from live score data (e.g., a sports game is over and the final score is known). Inferred settlements are flagged and re-run when the confirmed Kalshi settlement arrives.

---

## 17. Data Feeds & External APIs

**Files:** `app/feeds/` directory

| Feed | Source | Cadence | Data |
|---|---|---|---|
| ESPN scoreboard | ESPN undocumented API | Every 30s (live games) | Live scores, game state |
| ESPN soccer | ESPN | Every 60s | Soccer live scores, xG |
| Sportradar | Sportradar API | Per-game | Authoritative scores, stats |
| Odds API | TheOddsAPI | Hourly | Market odds across sportsbooks |
| Positive EV | Various | Hourly | Positive expected value picks |
| Public betting | Various | Hourly | Public betting percentages |
| Public live state | Various | Every 30s | Live line movements |
| Kalshi crypto markets | Kalshi API | Every 5m | Crypto market tickers + prices |
| Kalshi sports markets | Kalshi API | Hourly | Sports market tickers |
| Kalshi weather markets | Kalshi API | Hourly | Weather market tickers |
| Kalshi schedules | Kalshi API | Hourly | Upcoming market schedule |
| Crypto OHLCV | CoinGecko / CoinAPI | Every 5m | OHLCV by ticker |
| Weather payload | Open-Meteo, NWS, HRRR, Tomorrow.io | Hourly | Multi-source forecast |
| Weather climatology | NOAA normals | Daily | Historical temperature normals |
| Election polls | Polling aggregator | Daily | Poll data by race |
| Economic calendar | Financial data provider | Daily | Consensus estimates, releases |
| Unusual Whales | Unusual Whales API | Hourly | Options flow, dark pool |
| OutlierBet | Playwright scraper | Per-poll | Tipster picks |
| SportsLine | Playwright scraper | Per-poll | Tipster picks |
| BettingPros | API | Per-poll | Aggregated tipster picks |
| NFL snap counts | Sportradar | Post-game | Snap count data for props |
| NFL backfill | Sportradar | Daily | Historical NFL game data |
| Player props (NBA/NFL/MLB/NHL/WNBA) | BettingPros / OddsAPI | Hourly | Line data for props |

**Retry strategy:** All outbound HTTP calls use `app/http_retry.py` — exponential backoff with jitter, configurable max retries.

---

## 18. Web Dashboard

**Location:** `artifacts/web/` — React + Vite, Clerk auth, TanStack Query

### Routes

| Route | Purpose |
|---|---|
| `/` or `/app` | Main dashboard — KPI bar, equity curve, trades |
| `/positions` | Open and recent positions with filters |
| `/feed` | Live prediction feed with swarm debate transcripts |
| `/leaderboard` | Swarm persona leaderboard |
| `/predictions` | All predictions with confidence and outcome |
| `/operator` | Operator console (admin-only) |
| `/operator/health` | Model health, feed staleness, alerts |
| `/operator/risk` | Risk gate status, daily loss, kill switch |
| `/operator/verticals` | Per-vertical performance and halt controls |
| `/operator/trades` | Full trade log with placed_by filter |
| `/billing` | Stripe subscription management |
| `/settings` | Account and notification settings |

### Key widgets

| Widget | Metric |
|---|---|
| KPI Summary Bar | P&L Today, Win Rate, ROI (period), Total Volume, Open Positions, Sharpe |
| Equity Curve | Cumulative P&L over time, filterable by vertical and placed_by |
| Tail-Risk Exposure Tile | Live OTM concentration by price bucket (< $0.10, $0.10–$0.25, ...) |
| Bankroll Panel | Current bankroll, daily gain/loss, bankroll events |
| Active Positions | Open trades with unrealized P&L |
| Leagues Exposure | Risk concentration by league |
| Trade Stats Panel | Win rate, avg edge, avg odds, avg hold |
| Swarm Feed | Live debate transcripts with analyst votes |
| Settlement Feed | Recent settlements with outcome and P&L |

### Real-time data

Shared WebSocket provider (`useWebSocket` hook) connects to `app/kalshi/ws.py` relay and streams:
- Fill events
- Settlement events
- Gate trip alerts
- Model health alerts
- Orchestrator cycle completions

---

## 19. Mobile App

**Location:** `artifacts/mobile/` — Expo React Native

### Key screens

- **Dashboard** — KPI summary, equity curve, tail-risk tile
- **Feed** — Live prediction feed
- **Crypto Outcomes** — Crypto-specific P&L and position panel
- **Leaderboard** — Swarm persona rankings
- **Settings / Profile** — Account, subscription, notifications

### Subscriptions (RevenueCat)

- RevenueCat manages all in-app purchases on iOS and Android
- Entitlement webhooks sync subscription state to Supabase
- `@replit/revenuecat-sdk` integration with Replit-managed credentials
- Webhook delivery tracked in `rc_webhook_deliveries` (30-day retention)
- Push notifications via Expo Push Notification service

### WebSocket

Generation-guarded mounts prevent WebSocket leaks on navigation. Each mount has a generation counter; stale listeners from prior generations are discarded.

---

## 20. Operator Console & Admin Controls

**Location:** `artifacts/web/src/operator/`

The operator console gives full control over the trading system.

### Live controls

| Control | Effect |
|---|---|
| Global kill switch | Immediately halts all order submission across all venues |
| Vertical halt | Halts one vertical; other verticals continue; resting orders cancelled |
| Trading mode | paper / live / halt |
| Re-arm trading | Resumes after daily-loss halt (only if loss has recovered) |
| Aggression mode | conservative / default / aggressive — affects swarm thresholds |
| Kelly fraction | Adjust bet sizing in real time |
| Daily loss thresholds | USD and percent |
| LLM mode | cloud / local / hybrid |

### Monitoring panels

- **Model health alerts** — per-model Brier, drift, staleness
- **Feed staleness** — last-updated timestamp per feed, alert when stale
- **Orchestrator status** — cycle count, last cycle timestamp, candidates/trades per cycle
- **Settlement status** — pending settlements, inferred vs. confirmed
- **Risk gate trips** — recent gate trips with market and reason
- **Dead letter queue** — orders stuck in unresolved states
- **Slippage guard rejects** — orders rejected for edge erosion
- **Over-filled orders** — orders filled above expected qty
- **Canary/ramp status** — current ramp tier, settled trade count

### Search

Global search (`/api/operator/search/all`) covers: trades, users, billing events, audit log, swarm debates — unified across all entities.

---

## 21. Billing, Auth & Subscriptions

### Authentication

**Clerk** manages all user authentication:
- JWT-based session tokens verified on every authenticated API request
- Operator allow-list via Clerk user metadata (`is_operator: true`)
- Webhook events (user.created, user.updated) sync to Supabase `users` table
- Clerk webhooks verified with SVIX signature

### Subscription tiers

| Tier | Access |
|---|---|
| `free` | Limited predictions, public leaderboard |
| `pro` | Full prediction feed, debate transcripts, 30-day history |
| `elite` | All pro features + tail-risk tile, advanced analytics, priority |
| `founder` | All elite features + operator console access |

**Web billing:** Stripe manages subscriptions. Webhook events (invoice, subscription) sync entitlements to Supabase. Stripe portal for self-service management.

**Mobile billing:** RevenueCat on iOS/Android. Entitlements synced via webhook to Supabase.

**Free tier rate limits:** `app/free_tier.py` enforces view limits. `free_tier_views` table tracked with 30-day retention.

---

## 22. Key Environment Variables

| Variable | Purpose |
|---|---|
| `AXIOM_TRADING_MODE` | `paper` / `live` / `halt` |
| `AXIOM_LLM_MODE` | `cloud` / `local` / `hybrid` |
| `AXIOM_DAILY_LOSS_HALT_USD` | Daily loss hard limit in dollars |
| `AXIOM_DAILY_LOSS_HALT_PCT` | Daily loss as percent of start-of-day bankroll |
| `AXIOM_DAILY_LOSS_TZ` | Timezone for day boundary (default UTC) |
| `AXIOM_SLIPPAGE_GUARD_MAX_EDGE_PCT` | Max % of edge slippage may erase (default 50) |
| `AXIOM_SLIPPAGE_GUARD_REST_FULL_LIMIT` | GTC: rest full limit on thin book |
| `AXIOM_SLIPPAGE_GUARD_ALLOW_EMPTY_SIDE` | GTC: allow order when book side empty |
| `AXIOM_SLIPPAGE_GUARD_REST_EMPTY_SIDE_FOK` | FOK: convert to edge-capped GTC on empty side |
| `AXIOM_MARKETABLE_FOK` | Enable marketable FOK mode (IOC emulation) |
| `AXIOM_CANARY_TRADES` | Number of initial live trades at qty=1 (default 5) |
| `AXIOM_CANARY_RAMP_ENABLED` | Enable canary ramp sizing |
| `AXIOM_RAMP_CAPS` | Ramp tier quantity caps (comma-separated) |
| `AXIOM_RAMP_TIER_TRADES` | Settled trades required per ramp tier |
| `AXIOM_PIPELINE_VERTICALS` | Verticals to run in pipeline mode during edge_terminal |
| `OPERATOR_ALLOWED_EMAILS` | Comma-separated operator email allow-list |
| `ANTHROPIC_API_KEY` | Claude API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `GOOGLE_API_KEY` | Gemini API key |
| `KALSHI_API_KEY_ID` | Kalshi API key ID |
| `KALSHI_API_PRIVATE_KEY` | Kalshi RSA private key |
| `CLERK_SECRET_KEY` | Clerk backend secret |
| `STRIPE_SECRET_KEY` | Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signature secret |
| `POSTMARK_SERVER_API_KEY` | Transactional email (day-end report) |
| `SESSION_SECRET` | FastAPI session secret |

---

## 23. Lessons Learned

These are the hard-won operational lessons from building and running Axiom in production.

### Execution Engine

1. **`api-server` has no hot-reload.** Python file edits require an explicit workflow restart. A standalone `python -m` import test proves the file parses, but only a restart proves the running server sees the change.

2. **Single-task poller wedges forever on unbounded awaits.** The Edge Terminal poller can deadlock on any unbounded `await` (especially Playwright). Every tick `await` must be wrapped in `asyncio.wait_for`. Trust `last_poll_at` recency for liveness, not process existence.

3. **Re-arm boundary: ON CONFLICT re-arms only `skipped_unresolved`.** Never re-arm `placed` (causes double-bets) or `skipped_gate` (market settled, re-arm is pointless).

4. **`input_mode` must fail closed.** An invalid or unknown `input_mode` must default to the most restrictive behavior. The boot-once `READY` gate parks ALL verticals until the input mode is definitively resolved.

5. **`AXIOM_PIPELINE_VERTICALS` runs one vertical's full pipeline while global mode is `edge_terminal`.** This allows mixed-mode operation but must be explicitly set per vertical.

### Kalshi Mapping

6. **`market_not_found` (404) = clean skip, not a failure.** Settled, closed, or wrong-ticker 404s are expected. They are recorded as `skipped_unresolved` and never count toward failure metrics or trigger alerts.

7. **Kalshi has no native IOC.** Marketable FOK is emulated by sizing the order down to the immediately fillable depth at submission time. It takes partial fills and never rests as a GTC. Enable with `AXIOM_MARKETABLE_FOK=1`.

8. **Canary bypass:** Canary is off by default; turning `AXIOM_MARKETABLE_FOK` off re-enables standard resting GTC behavior.

9. **Slippage guard empty-side behavior:** An empty book side allows a resting limit by default (GTC only). Both "rest-the-limit" early-returns are FOK-gated so marketable FOK slices to fillable depth instead of resting or 409-ing.

### Market Resolution

10. **Edge Terminal resolver must use `resolve_match()` not `resolve()`.** Stubbing only `resolve()` makes None-ticker signals silently skip (zero fires) — use `resolve_match` as the entry point.

11. **Model probability is pick-relative, never flip for NO.** `model_prob` is always the probability that the pick wins. When the pick is the NO side, submit as a BUY NO order at the appropriate price — do not invert the probability.

12. **Soccer "+/-0.5" routes to the winner market.** No half-goal spread market exists on Kalshi. A spread ticker of "+/-0.5" is a moneyline proxy.

13. **Soccer team codes are FIFA codes.** Not first-three-letters abbreviations. The resolver must use FIFA country/club codes or matches will fail silently.

14. **Signed spread resolution:** The resolver owns target market + side. Ignore the scraper ticker for "+/-" spreads — the resolver's `_side_evidence` is authoritative.

### Calibration & Models

15. **A vertical with no feed upserts silently never trades.** If a vertical's feed runner is not running, no predictions rows are written, and the vertical appears to work but produces zero candidates. Always verify the feed is upsetting before concluding the model is broken.

16. **Thin calibrators use Platt, not isotonic.** EVENTS_ECON (n=19) and COMMODITIES (n=10) use Platt scaling. Isotonic regression is unreliable below ~50 samples.

17. **Brier scores improve substantially with calibration.** Raw model Brier scores are typically 0.02–0.05 higher than calibrated. Never report raw scores as the model's final accuracy.

### Risk & Sizing

18. **The 60% confidence floor is the most common reason Edge Terminal signals don't fire.** Before debugging market resolution, check signal confidence. A signal at 58% will be silently dropped.

19. **Tail bets (< $0.10 entry) drove virtually all historical losses.** The Feb-20 NBA NO bet (qty=4,394 at $0.04 → −$4,218) was larger than the entire YTD P&L. The `tail_price_size_cap` and `jumbo_quantity_cap` gates exist specifically because of this.

20. **Canary/ramp is fail-closed.** If canary state cannot be read from the DB, the system defaults to the smallest quantity cap (qty=1). This is intentional — better to size too small than too large during an uncertain ramp state.

21. **Daily loss gate aggregates settled + unrealized P&L.** Open positions with negative unrealized P&L count toward the daily loss limit, not just settled trades.

### Operations

22. **Day-end email previously counted only settled trades.** Edge Terminal live trades (placed today, not yet resolved) never appeared. The fix was to include all trades with `executed_at` today, not just those with `pnl IS NOT NULL`.

23. **Live-state staleness pager is suppressed in `edge_terminal` mode.** In Edge Terminal mode, sports prediction loops don't trade, so a stale sports feed is expected and non-actionable. The pager auto-restores when you switch back to pipeline mode.

24. **Signals live in Supabase, not Replit DB.** When debugging "trades not firing," query via the `app.db` asyncpg pool (Supabase). The Replit dev DB does not have signal data.

25. **Config and code changes are inert until the `api-server` workflow is restarted.** This is the single most common debugging dead-end — assuming a change is live when the server is still running old code.

---

*Last updated: August 2026. This document reflects the production codebase in `artifacts/api-server`, `artifacts/web`, and `artifacts/mobile`.*
