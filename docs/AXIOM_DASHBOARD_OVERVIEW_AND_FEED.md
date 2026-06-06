# Axiom Predict — Advanced Overview & Feed Tab Spec

A reference for the **Overview** tab and the **Feed** (Pipeline) tab of the
Axiom Predict web dashboard (`artifacts/web`): what exists today, and the full
set of critical widgets, metrics, and feed upgrades that would make them a
genuinely advanced, operator-grade console for a **live-money** Kalshi
auto-trader.

> Grounded in the actual app — file/component/endpoint names below are the real
> ones in `artifacts/web` and `artifacts/api-server`, not generic placeholders.

---

## 1. What exists today

### 1.1 Navigation (app shell)

Sidebar in `src/components/shell/sidebar.tsx`:

| Group | Tab | Route | File |
|---|---|---|---|
| Markets | **Overview** | `/app` | `src/pages/dashboard.tsx` |
| Markets | Verticals | `/verticals` | `src/pages/verticals.tsx` |
| Markets | Live Games | `/live-games` | `src/pages/live-games.tsx` |
| Portfolio | Positions | `/positions` | `src/pages/positions.tsx` |
| Portfolio | Trends | `/trends` | `src/pages/trends.tsx` |
| Intelligence | Intelligence | `/intelligence` | `src/pages/intelligence.tsx` |
| Intelligence | Calibration | `/calibration` | `src/pages/calibration.tsx` |
| Intelligence | Auto-research | `/research` | `src/pages/research.tsx` |
| Intelligence | Weather | `/weather` | `src/pages/weather.tsx` |
| Trading | Trading | `/trading` | `src/pages/trading.tsx` |
| Trading | **Pipeline (the Feed)** | `/pipeline` | `src/pages/pipeline.tsx` |

### 1.2 Overview tab (`dashboard.tsx`) — current widgets

- **KPI summary bar** (`kpi-summary-bar.tsx`) — P&L Today, Win Rate (all-time),
  ROI (period), Total Volume, Open Positions, Sharpe (trade-weighted).
- **Equity & performance** — `equity-charts.tsx` (cumulative P&L curve),
  `trade-stats-panel.tsx` (trade-performance breakdown).
- **Portfolio & exposure** — `bankroll-panel.tsx` (balance/available/allocation),
  `active-positions.tsx` (held contracts), `leagues-exposure-panel.tsx` (risk
  concentration by league).
- **The Swarm** — `swarm-debates.tsx` (LLM consensus verdicts),
  `swarm-analysts.tsx` (persona leaderboard: Brier score, accuracy).
- **Edge Terminal** — `EdgeTerminalPnlTile` / `RecentSignals` / `SignalMatches`
  (signals + P&L from the scraping/polling layer).

### 1.3 Feed tab = Pipeline (`pipeline.tsx`) — current behavior

A real-time stream of the autonomous engine, grouped by `cycle_id`:

- **Trading cycles** — lifecycle per cycle: start → candidate evaluation →
  gates → placement → complete.
- **Candidate blocks** — "Cached" (swarm consensus) vs "Live" (real-time model)
  probabilities, plus **gate verdicts** with pass/fail reasons (e.g.
  `commodities_correlation`, `bankroll_cap`).
- **Recent fills** (`recent-trades.tsx`) and a **settlement feed** (outcomes +
  P&L realization).

### 1.4 Backend endpoints feeding the tabs

| Widget | Endpoint | Key fields |
|---|---|---|
| KPI bar | `/api/dashboard/overview` | `win_rate`, `open_positions`, `realized_pnl` |
| KPI bar | `/api/public/equity-curve` | `daily_pnl`, `cumulative_pnl`, `trades` |
| Swarm debates | `/api/intelligence/recent-debates` | `matchup`, `consensus_probability`, `recommended_action`, `analyst_verdicts` |
| Swarm analysts | `/api/intelligence/swarm-personas-today` | `persona`, `brier_score`, `accuracy`, `count` |
| Recent fills | `/api/dashboard/recent-trades` | `market_title`, `quantity`, `price`, `pnl`, `exchange` |
| Verticals | `/api/dashboard/verticals` | `vertical`, `volume`, `pnl`, `sharpe`, `trades` |
| Pipeline feed | `/api/ws/pipeline` (WS) | `cycle_start`, `gate`, `candidate_complete`, `trade_placed` |
| Settlement | `/api/ws/settlement` (WS) | `trade_settled`, `market_settle_complete`, `pnl` |

**Verdict:** the bones are there (KPI bar, equity curve, exposure, a live
pipeline). The gap is **depth of analytics** on Overview and **richness +
explainability** in the Feed.

---

## 2. Advanced Overview — critical widgets

Organized by panel. Each entry gives the metric, the formula/definition, and why
it matters for an auto-trader.

### 2.1 Performance scorecard (the core)

#### Units (net / per-day / rolling)
- **What:** the betting-native, stake-agnostic P&L measure.
  `units = result / stake_unit`, where 1 unit = your standard bet size.
- **Show:** net units, units/day, and a rolling 7d / 30d sparkline.
- **Why:** raw dollars conflate edge with sizing. Units isolate *skill* —
  serious bettors trust units over $ because it normalizes across bankroll
  changes and variable stakes. With Edge Terminal's flat 5%-of-bankroll sizing,
  units make cross-period comparison honest.

#### P&L, fully decomposed
- **What:** split **realized** (settled) vs **unrealized** (open mark-to-market),
  across **today / 7d / 30d / all-time**.
- **Why:** *placed ≠ settled.* The day-end email originally read "no trades"
  because it counted only settled rows; same-day live positions had no
  `settled_at`. The Overview must show open exposure's MTM so intraday activity
  is never invisible. Realized answers "what did I bank," unrealized answers
  "what am I holding" — different questions, different columns.

#### ROI vs Yield
- **ROI:** profit ÷ bankroll (return on capital).
- **Yield:** profit ÷ total amount staked (handle) — the bettor's efficiency
  number.
- **Why:** a high ROI on a tiny bankroll and a thin yield on huge handle are
  very different businesses; show both.

#### Sharpe + Sortino + Drawdown
- **Sharpe:** mean return / stddev of returns (already present, trade-weighted).
- **Sortino:** mean return / **downside** deviation — fairer for skewed binary
  bet outcomes where upside variance shouldn't be penalized.
- **Max drawdown / current drawdown:** largest peak-to-trough equity decline,
  plus a live "how deep are we right now" gauge and the drawdown curve.
- **Why:** Sharpe alone hides tail risk; Sortino + drawdown describe survival.
  For live money, drawdown is the number that decides whether to keep sizing up.

#### CLV — Closing Line Value
- **What:** compare your **entry price** to the market's **closing price** for
  the same contract/side. Track avg CLV and % of bets with positive CLV.
- **Why:** CLV is the single best **leading** indicator of edge — it predicts
  long-run profit *before* outcomes settle. An auto-trader can be sample-starved
  on win rate for weeks; CLV tells you the engine is beating the market now.
  (Requires capturing closing price at settlement — see §4.)

#### Win rate vs expected (calibration delta)
- **What:** actual hit rate minus model-implied probability (volume-weighted).
- **Why:** instantly shows whether the models are honest. A persistent negative
  delta means overconfidence; positive means the confidence floor may be leaving
  edge on the table.

### 2.2 Bankroll & exposure

- **Equity curve with drawdown shading** + an optional **benchmark line**
  (flat-staking the same picks) to prove the engine adds value over naive
  staking.
- **Open-risk gauge** — total $ at risk vs bankroll, **Kelly utilization %**,
  and concentration by **vertical / league / single market** (extends
  `leagues-exposure-panel.tsx`). Flag when any single market or league exceeds a
  concentration cap.
- **Bankroll growth** — CAGR-style growth rate and a **time-to-2x** projection
  from the current trend.

### 2.3 Edge-engine health (auto-trader-specific)

#### Fire-rate funnel
- **Stages:** signals seen → resolved (ticker+side) → passed confidence floor →
  passed gates → placed → filled, each with **drop reasons**.
- **Why:** this is the visual answer to "why isn't it trading?" The common
  culprits are visible at a glance: the **60% confidence floor**
  (`skipped_low_confidence`), `skipped_unresolved` (market not listed yet —
  heavy in tennis/soccer), and `skipped_slippage` (thin book). Far better than
  guessing from logs.

#### Fill quality / slippage
- Avg slippage vs intended price, **fill rate**, partial-fill rate, and average
  **resting-order age** before fill or stale-sweep cancel.
- **Why:** the slippage guard and marketable-FOK/GTC hybrid make pricing
  non-trivial; this panel shows whether the execution layer is actually getting
  good fills or resting/missing.

#### Model calibration tile
- Brier score + a small **reliability curve** (predicted vs observed), sourced
  from the existing calibration data (`/calibration`).
- **Why:** keeps model honesty on the front page, not buried in a sub-tab.

#### Poller / mode status strip
- `last_poll_at` freshness (green/amber/red), current **input mode**
  (`edge_terminal` vs `pipeline`), and which verticals are **pipeline-exempt**
  (e.g. weather).
- **Why:** the Edge Terminal poller can **stall silently and permanently** — the
  process stays up while the loop is wedged, so "the app is up" ≠ "it's trading."
  A freshness light on the Overview catches this in seconds instead of hours.

### 2.4 Suggested Overview layout (top → bottom)

1. **Status strip** — mode, poller freshness, exempt verticals, halts.
2. **KPI bar (expanded)** — Net Units · Realized P&L · Unrealized P&L · Yield ·
   Sortino · Max Drawdown · CLV · Open Positions.
3. **Equity curve** (with drawdown shading + benchmark) | **Drawdown gauge**.
4. **Fire-rate funnel** | **Fill quality**.
5. **Exposure** (open-risk gauge + concentration) | **Bankroll growth**.
6. **Calibration tile** | **Swarm leaderboard**.
7. **Edge Terminal** tiles (P&L, recent signals, matches).

---

## 3. Advanced Feed (Pipeline) tab

### 3.1 Unified, filterable activity stream
- Merge **pipeline events**, **Edge Terminal fires**, and **settlements** into
  one timeline.
- **Filters:** vertical, status (placed / skipped_* / settled), outcome
  (win/loss), exchange, source (orchestrator vs edge_terminal), time range.

### 3.2 Rich per-event cards
- **Placement card:** market, side, entry price, **size in units**, model prob
  vs market prob, **edge**, Kelly fraction, and the **gate verdict trail**
  (which gates passed/failed and why).
- **Settlement card:** outcome, realized P&L (in $ and units), and **CLV
  captured** (entry vs close).
- **Skip card:** the reason inline — `skipped_low_confidence`,
  `skipped_unresolved`, `skipped_slippage`, `skipped_gate`,
  `skipped_already_placed` — so dark periods are *explained*, not silent.

### 3.3 Live status header
- Poller `last_poll_at` recency, current input mode, exempt verticals, and any
  vertical halts — mirroring the Overview status strip for operators living in
  the Feed.

### 3.4 Settlement ticker
- A running **realized P&L for the day** that ticks up/down as outcomes land,
  with a small intraday sparkline.

### 3.5 Why these upgrades
- The current Feed shows *that* things happened; the advanced Feed shows *why* —
  the gate trail, the skip reasons, and the CLV make it a debugging and
  edge-verification tool, not just a log. Given the engine's silent-skip failure
  modes (confidence floor, unresolved markets, stalled poller), explainability in
  the Feed is the fastest path from "it's quiet" to "here's why."

---

## 4. Data/back-end work implied (so estimates are honest)

Most advanced widgets need backend support, not just UI:

| Widget | New data needed |
|---|---|
| Units | A defined `stake_unit` + per-trade `result/unit` rollups |
| Unrealized P&L | Mark open positions to current market mid (WS orderbook cache) |
| CLV | Capture **closing price** per market at settlement; store on the trade |
| Sortino / Drawdown | Daily return series + downside-deviation + running peak |
| Fire-rate funnel | Aggregate `edge_terminal_signals` by status over a window |
| Fill quality | Persist intended vs filled price + resting-order age on fills |
| Yield | Total handle (sum of stake) alongside profit |
| Status strip | Expose `edge_terminal_poller_status` + input mode via an endpoint |

**Note:** `trades.price` is stored in **dollars** (e.g. 0.62). Any widget that
shows cents must convert (`round(price * 100)`), matching the convention used in
the day-end email renderer.

---

## 5. Prioritized rollout

**Phase 1 — highest leverage (edge truth + survival):**
Net Units · Realized/Unrealized P&L split · Sortino + Max Drawdown · **CLV** ·
Fire-rate funnel.

**Phase 2 — execution & risk depth:**
Yield · Fill quality/slippage · Open-risk gauge + concentration · Status strip
(poller freshness + mode).

**Phase 3 — analyst & feed polish:**
Calibration tile on Overview · Unified filterable Feed · Rich gate-trail cards ·
Settlement ticker · Bankroll growth / time-to-2x.

**Top three to build first:** **Units**, **CLV**, and **Max Drawdown** — they
answer the three questions that matter most for a live-money bot: *am I skilled
(units), am I beating the market right now (CLV), and can I survive the variance
(drawdown)?*
