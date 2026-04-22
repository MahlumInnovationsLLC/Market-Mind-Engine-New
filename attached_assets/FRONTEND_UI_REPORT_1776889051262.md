# Axiom Predict — Frontend UI Layout Report

_Last updated: 2026-04-22 • React 18 + TypeScript + Vite + Tailwind + React Router v6 + TanStack Query_

---

## 1. Tech Stack & Global Architecture

| Layer | Choice |
|---|---|
| Framework | React 18 (functional components + hooks) |
| Build | Vite 5 |
| Language | TypeScript (strict) |
| Routing | `react-router-dom` v6 (BrowserRouter, lazy routes, query-string tabs) |
| Data | `@tanstack/react-query` (staleTime 30s, gcTime 5min, retry 2) |
| Styling | Tailwind CSS 3 (`darkMode: 'class'`) + custom CSS variables |
| Realtime | WebSocket context (`WebSocketContext`) + `useReconnectingWebSocket` hook |
| Auth | `AuthContext` w/ session cookie; `AuthGate` wraps every route |
| Code-splitting | Every page is `React.lazy()` — `<Suspense fallback=<PageFallback>>` |

### Top-level providers (App.tsx)
```
QueryClientProvider
 └─ BrowserRouter
     └─ AuthProvider
         └─ AuthGate
             └─ AlertProvider
                 └─ TradingModeProvider
                     └─ WebSocketProvider
                         └─ DegradedModeProvider
                             └─ <Sidebar /> + <Header /> + <Routes />
                                 + AlertToaster + TradeToasts + WeatherAlertMonitor + ConciergeChat
```

---

## 2. Design System & Theming

### 2.1 Color tokens (`index.css` + `tailwind.config.js`)

**Dark theme (default — `:root`):**
| Token | Value | Usage |
|---|---|---|
| `--bg-primary` | `#0a0e1a` (navy-950) | App background |
| `--bg-secondary` | `#0d1526` (navy-900) | Sidebar, header, modals |
| `--bg-tertiary` | `#111b2e` (navy-850) | Cards, panels |
| `--text-primary` | `#e2e8f0` (slate-200) | Headings, body |
| `--text-secondary` | `#94a3b8` (slate-400) | Subtitles, labels |
| `--text-muted` | `#64748b` (slate-500) | Hints, footer text |
| `--accent-cyan` | `#00e5ff` | Primary actions, brand glow |
| `--accent-green` | `#00e676` | Profit, "live", success |
| `--accent-red` | `#ff1744` | Loss, alerts, stop |
| `--border-subtle` | `rgba(255,255,255,0.06)` | Card edges |
| `--border-glow` | `rgba(0,229,255,0.3)` | Active/focus borders |

**Tailwind extensions:**
- `colors.navy.{600..950}` → CSS-variable navy ramp
- `colors.accent.{cyan, blue, purple, magenta}`
- `boxShadow.glow-{cyan, blue, purple, red, green}` + `glass`

**Light theme (`html.light-theme`):** flips backgrounds to white/slate-50, accents to teal-700/emerald-700/red-600.

### 2.2 Custom utilities

| Utility | Definition |
|---|---|
| `.glass` | `rgba(13,21,38,0.6)` + `backdrop-blur(12px)` + 1px subtle border |
| `.glass-strong` | Same w/ 0.85 opacity, 20px blur |
| `.border-glow` | Cyan glow border |
| `.text-glow-{cyan/green/red}` | Color text-shadow |
| `.grid-bg` | 40×40 px cyan grid lines @3% opacity (cyberpunk feel) |

### 2.3 Animations (Tailwind keyframes)
| Class | Effect |
|---|---|
| `animate-pulse-slow` | 3s pulse |
| `animate-glow-pulse` | Opacity 0.4→1→0.4 (2s) |
| `animate-slide-in` | Translate-x −8px → 0 + fade (300ms) |
| `animate-scan-line` | Vertical scan line for "live data" feel |

### 2.4 Typography
- Font: `Inter`, system fallback, antialiased
- Weights used: 400 (body), 500 (labels), 600 (sub-headers), 700 (numbers/headers), 800–900 (page titles, brand)
- Tabular numbers in tables/cards: `tabular-nums` Tailwind class
- Page titles: `text-2xl font-bold/black text-white`
- Section headers: `text-sm font-semibold uppercase tracking-wider text-gray-400`

---

## 3. Global Chrome (visible on every authenticated page)

### 3.1 Sidebar (`components/Navigation/Sidebar.tsx`)
- **Width:** `w-60` (240px) on `lg+`; full drawer (`w-full sm:w-80`) below
- **Background:** `bg-navy-900/95 backdrop-blur-xl`, right border `white/[0.06]`
- **Mobile:** hamburger button top-left, slides in with backdrop
- **Brand block:** `<AxiomLogo size={36} />` + "AXIOM" (black, tracked) + "PREDICT" (uppercase, 9px) + connection dot (green/yellow/red, glow)
- **Collapsible sections** (`NavSection` component) — see §4
- **Footer:** Admin (gold) → Profile (cyan) → user name + tier badge (Free/Pro/Ent/Admin) + sign-out icon

### 3.2 Header (`components/Navigation/Header.tsx`)
- Sticky top bar showing trading mode (PAPER/LIVE pill), notifications bell, dark-mode toggle, search

### 3.3 Always-on overlays
| Component | Purpose |
|---|---|
| `<DegradedModeBanner>` | Yellow strip when backend is degraded |
| `<TradingAlertsBanner>` | Red banner for kill-switch / circuit breaker events |
| `<AlertToaster>` | Stacked toast notifications (top-right) |
| `<TradeToasts>` | WS-driven trade-executed pop-ups |
| `<WeatherAlertMonitor>` | Pop-ups for severe weather impacting open positions |
| `<ConciergeChat>` | Floating chat bubble (bottom-right) for AI assistant |

---

## 4. Navigation Structure (`navConfig.ts`)

The sidebar is grouped into **7 sections**. Each item may have a query-string-driven sub-menu. Active items get cyan-500/10 background + cyan-400 text.

| Section | Items |
|---|---|
| **Markets** | Sports (+11 sub), EV Picks, Crypto (+7 sub), Events (+10 sub), Weather (+4 sub) |
| **Portfolio** | Overview, P&L, Positions, Bankroll, Analytics |
| **Stats & Calibration** | Stats Center (ESPN/Elo/Calibration/CLV), Sportradar (Schedule/Injuries/Standings/ML Features/Status) |
| **Accounts** | Exchange Accounts (Kalshi / Kalshi Portfolio / Robinhood / Robinhood Portfolio / Polymarket) |
| **Intelligence** | AI Center (Config/Performance/Swarm/Signals/Logs/Edge), AutoResearch ★, ML Training ★, Shadow Trading ★, Axiom Gravity ★ (★ = accent badge) |
| **Trading** | Live Trading, Bet Toggles |
| **System** | System & Ops (Health/Pipeline/Controls/Diagnostics/Logs/Alerts/Settings), Reports, Leaderboard |

**Badges:** real-time counts (`activeSportsTrades`, `activeWeatherTrades`, `unreadAlerts`) appear as red pill on the right of nav items.

---

## 5. Routing Map (`App.tsx`)

### 5.1 Public (no auth required)
| Path | Page |
|---|---|
| `/landing` | `Public/LandingPage` — marketing splash |
| `/about` | `Public/AboutPage` — team/mission |
| `/pricing` | `Pricing/PricingPage` — tier comparison |
| `/leaderboard` | `Public/LeaderboardPage` — public top-traders |
| `/login` | `Auth/LoginPage` |

### 5.2 Authenticated (wrapped by `<Sidebar/>` + `<Header/>`)

| Route | Page | Notes |
|---|---|---|
| `/` | `Overview/OverviewPage` | Dashboard home |
| `/trading` | `UnifiedTradingPage` | Tabs: live / feed |
| `/positions` | `Positions/PositionsPage` | All open contracts |
| `/bankroll` | `BankrollTrackerPage` | Bankroll history |
| `/analytics` | `UnifiedAnalyticsPage` | Tabs: dashboard / performance / advanced |
| `/sports` | `Trading` (legacy entry) | Tab routing via `:tab` |
| `/sports/game/:gameId` | `Sports/GameDetailPage` | Per-game detail |
| `/sports/intangibles` | `Sports/IntangiblesPage` | Weather/lineups/injuries |
| `/sports/data-sources/:sourceId` | `DataSourceRouter` | bettingpros / sportsline / outlier |
| `/sports/management` | `Sports/SportManagementPage` | Enable/disable per-sport |
| `/ev-picks` | `EVPicks/EVPicksPage` | Filterable picks grid |
| `/ev-picks/prop/:propId` | `Sports/PlayerPropDetailPage` | Per-prop deep-dive |
| `/events` | `EventsPage` | Tabs: economics/politics/geopolitics/tech/etc |
| `/crypto` | `CryptoUnifiedPage` | Tabs: market/predictions/ev-picks/trades/intelligence/dashboard |
| `/weather` | `WeatherUnifiedPage` | Tabs: ev-picks/sports-ev/intelligence |
| `/weather/:cityCode` | `Weather/CityDetail/CityDetailPage` | City forecast detail |
| `/accounts` | `AccountsUnifiedPage` | Tabs for each exchange + portfolio |
| `/intelligence` | `IntelligenceUnifiedPage` | Tabs: AI config/performance/swarm/signals/logs/edge |
| `/system` | `SystemUnifiedPage` | Tabs: health/pipeline/controls/diagnostics/logs/alerts/settings/risk-console/blocked/rejected/exec-quality/feature-integrity |
| `/shadow-trading` | `ShadowTradingPage` | Live shadow vs production diff |
| `/axiom-gravity` | `components/AxiomGravity/AxiomGravity` | Visualizer (gravity-physics signal viz) |
| `/stats` | `StatsCalibrationPage` | Tabs: ESPN/Elo/Calibration/CLV |
| `/autoresearch` | `AutoResearchPage` | Hypothesis lifecycle |
| `/ml-training` | `MLTraining/MLTrainingPage` | Models, evolution chart, category perf |
| `/sportradar` | `SportradarPage` | Schedule/Injuries/Standings/ML/Status |
| `/bet-toggles` | `BetTogglesPage` | Per-vertical kill-switches |
| `/report` | `GraduationReport/GraduationReportView` | Standalone graduation summary |
| `/admin` | `Admin/AdminPage` | User mgmt (admin role only) |
| `/profile` | `Profile/ProfilePage` | Account settings |

### 5.3 Legacy redirects
~60 deprecated paths (`/crypto-binary`, `/risk`, `/control`, `/llm-logs`, etc.) are mapped to query-string variants of the unified pages via `<Navigate replace />`. Single source of truth lives in the redirect array in `App.tsx`.

---

## 6. Page-by-Page Layout Catalog

### 6.1 Overview (`/`) — `pages/Overview/OverviewPage.tsx`
- **Header:** `<OverviewHeader>` — title, last-updated time, refresh button
- **Connection states:** red error card + retry, or amber "Kalshi Not Connected" card with Settings link, or blue P&L explanation note
- **Layout sequence (vertical, `space-y-5`):**
  1. `<KPISummaryBar>` — 6-column metric strip (P&L today, win rate, ROI, total volume, open positions, sharpe)
  2. `<CoreMetricsRow>` — sport / crypto / weather mini-cards
  3. `<PeriodSummariesStrip>` — 1d / 7d / 30d / YTD pill toggle + numbers
  4. **2-column grid** (`lg:grid-cols-2`):
     - `<HighlightsPanel>` — top picks/best wins/biggest miss
     - `<IntelligenceScorePanel>` — model confidence + AutoResearch hits
  5. **3-column grid** (`lg:grid-cols-3`): `<CumulativePnlChart>` (spans 2) + side panel
  6. **2-column grid:** `<DailyPnlChart>` + `<TradeStatsPanel>`
  7. `<VerticalCards>` — sports/crypto/weather/events tiles
  8. `<ActivePositionsTable>` — sortable table of open contracts
  9. `<RecentTradesWidget>` + `<TodaysPicksSummary>`
  10. `<LivePipelineTracker>` — 6-stage pipeline strip with status dots
  11. `<SportPerformanceGrid>` — per-sport P&L heatmap
  12. `<SwarmIntelligenceWidget>` + `<AutoResearchMLWidget>` + `<SystemStatusWidget>` + `<QuickActionsWidget>`
  13. `<TradesDrilldownModal>` — opened on row click
- **Card style:** `rounded-xl border border-white/[0.06] bg-navy-850 p-4`
- **Section headers inside cards:** `text-sm font-semibold uppercase tracking-wider text-gray-400`

### 6.2 Trading (`/trading`) — `UnifiedTradingPage.tsx`
- **Title:** "Trading" + subtitle "Live positions, trade feed, and execution history"
- **Tab strip:** `flex gap-1 rounded-lg border border-white/[0.06] bg-navy-900/50 p-1` — each pill `rounded-md px-4 py-2 text-sm font-medium`
- **Tabs:** `live` (default), `feed`
- Sub-content lazily loaded via `<Suspense fallback={<TabFallback />}>`:
  - **Live tab:** Command Center → Live P&L → Positions → Live Pipeline
  - **Feed tab:** real-time trade stream (`<TradeFeed>` w/ per-row `<TradeRow>`)

### 6.3 Positions (`/positions`)
- Filter bar (vertical / status / exchange) → table with columns: Market, Side, Qty, Entry, Mark, P&L (color-coded), Edge, Age
- Hover row → cyan border-glow

### 6.4 Bankroll (`/bankroll`)
- `<BankrollTracker>` chart (line, recharts) + deposit/withdraw history table + KPI cards (current bankroll, all-time high, max drawdown)

### 6.5 Analytics (`/analytics`) — `UnifiedAnalyticsPage`
- Tabs: `dashboard` | `performance` | `advanced`
- Houses `<PerformanceDashboard>`, `<PerformanceAnalytics>`, `<AdvancedAnalytics>`, `<AIPerformanceDashboard>` — each a grid of charts (Recharts: line, bar, scatter, heatmap)

### 6.6 Sports (`/sports`) — `Trading.tsx`
- Top: vertical league switcher (NFL/NBA/MLB/NHL/NCAAF/NCAAB/SOCCER/UFC/...)
- Center: game grid (`<MatchCard>` cards with team logos, score, ML/spread/total, model probabilities, EV %)
- Sidebar (right `lg:`): `<PredictionFeed>` — ranked picks with confidence bar
- Footer panels: `<PortfolioPanel>`, `<RiskMonitor>`

#### 6.6.1 Game Detail (`/sports/game/:gameId`)
- 3-pane layout: matchup header (logos + records) → tabs (Predictions / Lines / Players / Matchup) → `<LineMovementChart>` (recharts area)
- Right rail: `<PipelineAuditTrail>` showing what fired the prediction

#### 6.6.2 Player Prop Detail (`/ev-picks/prop/:propId`)
- Hero: player photo + line + recommended side (OVER/UNDER pill)
- 4 KPI cards: model prob, market prob, edge, kelly%
- Stat trend line + last-5/10/season splits
- `<EVComparisonPanel>` — side-by-side EV across data sources

#### 6.6.3 Intangibles (`/sports/intangibles`)
- Grid of expandable cards: weather, injuries, travel, rest, lineup quality

#### 6.6.4 Data Sources (`/sports/data-sources/:sourceId`)
- Source tabs: BettingPros / SportsLine / Outlier — each shows source's picks vs. our model side-by-side, agreement %, ROI when agreed

### 6.7 EV Picks (`/ev-picks`) — `EVPicks/EVPicksPage`
- Filter bar: vertical, sport, min edge slider, min confidence slider
- View toggle: Featured / Matchups / Props
- `<FeaturedCard>` — large hero card with picks
- `<MatchupsView>` (game lines) and `<PropsView>` (`<PropsTable>` + `<PropCard>`)
- `<PicksTable>` — sortable table; row click → detail page
- `<PerformancePanel>` — historical EV pick ROI chart

### 6.8 Crypto (`/crypto`) — `CryptoUnifiedPage`
- Tabs (query-string): `market` | `predictions` | `ev-picks` | `trades` | `intelligence` | `dashboard`
- Modules per tab:
  - **Market:** `<EngineHeader>` (start/stop/paper toggle) + `<PredictionCards>` (5m/15m/1h horizons) + binance feed table
  - **Predictions:** historic prediction list w/ outcome
  - **EV Picks:** Kalshi BTC binary edge picks
  - **Trades:** `<TradesTable>` w/ filter (filled/paper/failed)
  - **Intelligence:** `<EnhancedFeaturesPanel>` + `<AutoResearchCard>` + `<MLTrainingCard>` + `<ScalpingCard>`
  - **Dashboard:** `<PerformanceMetrics>` + `<ConfigPanel>` + `<ArbitragePanel>`

### 6.9 Weather (`/weather`) — `WeatherUnifiedPage`
- Tabs: `overview` | `ev-picks` | `sports-ev` | `intelligence`
- City grid (cards with current temp, hi/lo, condition icon, EV badge); click → `/weather/:cityCode` detail
- City detail: hourly forecast chart + daily strip + Kalshi market for that city's hi/lo

### 6.10 Events (`/events`) — `EventsPage`
- Tabs: economics / politics / geopolitics / tech / entertainment / climate / calendar / calibration / trades
- Each tab: card grid of contracts with price (Yes/No), volume, predicted outcome, edge

### 6.11 Accounts (`/accounts`) — `AccountsUnifiedPage`
- Tabs: kalshi / kalshi-portfolio / robinhood / robinhood-portfolio / polymarket
- Each: connection status banner + balance cards + holdings table + connect/disconnect button

### 6.12 Intelligence (`/intelligence`) — `IntelligenceUnifiedPage`
- Tabs: AI Config / Performance / Swarm / Signals / Logs / Edge
- **AI Config:** sliders for confidence floors, edge thresholds; `<Settings/AggressionMeter>`
- **Performance:** `<AIPerformanceDashboard>` — model accuracy by sport + ROI
- **Swarm:** `<Intelligence/SwarmIntelligencePage>` — agent grid with status dots, agreement matrix
- **Signals:** `<Signals/SignalIntelligencePage>` — UW flow alerts feed (chronological)
- **Logs:** `<LLMLogsPage>` — chronological LLM call log w/ cost
- **Edge:** `<AxiomEdgePage>` — `<BTCDirectionalTab>` / `<ScalpEngineTab>` / `<SportsMLTab>` / `<WeatherTab>`

### 6.13 System (`/system`) — `SystemUnifiedPage`
- Horizontal scrollable tab bar: health / pipeline / controls / diagnostics / logs / alerts / settings (+ risk-console/blocked/rejected/exec-quality/feature-integrity)
- **Health:** `<SystemHealth>` cards: API, DB, Kalshi, Robinhood, Binance, SportsRadar, models — each a tile (status dot + latency + uptime)
- **Pipeline:** `<PipelineFlowchart>` (svg flow) + `<ConsoleLogsPanel>` (live tail)
- **Controls:** `<ControlPanel>` w/ `<EmergencyStopButton>` (large red), per-vertical toggles, `<TradingModeIndicator>` (PAPER/LIVE big pill)
- **Diagnostics:** `<SystemDiagnostics>` + `<SwarmPerformanceTab>`
- **Logs:** chronological log viewer with level filter chips (DEBUG/INFO/WARN/ERROR)
- **Alerts:** `<AlertCenter>` — list with severity color stripe; click row → `<AlertDetail>` modal
- **Settings:** `<SettingsPage>` — grouped form (notifications, theme, exchanges, model knobs, risk limits)
- **Risk Console:** `<RiskConsolePage>` — exposure heatmap + circuit breaker status + per-sport caps
- **Blocked Trades:** `<TradeBlockedCards>` — cards explaining why a trade was blocked
- **Rejected Trades:** `<RejectedTradesPage>` — table w/ reason codes
- **Exec Quality:** `<ExecutionQualityPage>` — slippage histogram + fill-time scatter
- **Feature Integrity:** `<FeatureIntegrityPage>` — per-feature drift/missing % bars

### 6.14 Stats Center (`/stats`)
- Tabs: ESPN Stats / Elo / Calibration / CLV
- `<ESPNStatsPage>` — team stat tables
- `<EloRatingsPage>` — sortable Elo table + history line per team
- `<CalibrationPage>` — calibration curve (recharts) per sport
- `<CLVDashboardPage>` — CLV histogram + per-source CLV table

### 6.15 AutoResearch (`/autoresearch`)
- KPI strip: hypotheses live / promoted / rejected / pending
- Hypothesis table: id, vertical, hypothesis text, lift, p-value, status pill, age
- Click row → details panel with backtest curve

### 6.16 ML Training (`/ml-training`)
- `<ModelEvolutionChart>` — line of model versions vs. brier/AUC/ECE
- `<CategoryPerformancePanel>` — stacked bars per category
- `<TradeAnalysisPanel>` — trade outcomes vs. model decision

### 6.17 Sportradar (`/sportradar`)
- Tabs: Schedule / Injuries / Standings / ML Features / Service Status

### 6.18 Bet Toggles (`/bet-toggles`)
- Master kill-switch (giant red toggle) + per-vertical / per-sport / per-market-type rows

### 6.19 Shadow Trading (`/shadow-trading`)
- Side-by-side: Live trades vs. Shadow trades; agreement %, P&L delta, top divergences

### 6.20 Axiom Gravity (`/axiom-gravity`)
- Full-screen physics-based signal visualizer (planets = sports, gravity wells = high-EV picks)

### 6.21 Reports (`/report`)
- `<GraduationReportView>` — markdown-style report with model graduation events, can be printed

### 6.22 Profile (`/profile`)
- Avatar + display name + email + tier + plan management + API key list + theme picker

### 6.23 Admin (`/admin`) — admin role only
- User table, subscription overrides, feature flags, system-wide kill switch

### 6.24 Onboarding (`/onboarding`)
- Multi-step wizard: welcome → exchange connect (Kalshi/Robinhood) → preferences → first deposit → done. Forced when `user.onboarding_complete === false`.

### 6.25 Pricing (`/pricing`) — public
- 3-column tier comparison (Free / Pro / Enterprise) — feature checklist with gradient borders on Pro

### 6.26 Leaderboard (`/leaderboard`) — public
- Ranked table of top traders (anonymized) with ROI, win rate, trades

---

## 7. Reusable Component Library

### 7.1 `components/ui/` (primitives)
`Card`, `Button`, `Badge`, `Pill`, `Modal`, `Drawer`, `Tabs`, `Tooltip`, `Skeleton` (`LoadingSkeleton` variants: `stat-row`, `chart`, `table`, `text`), `Toggle`, `Slider`, `Input`, `Select`.

### 7.2 `components/dashboard/`
- `CommandCenter` — start/stop engines, mode pill
- `LivePnL` — animated P&L counter w/ sparkline
- `TradeFeed` + `TradeRow` — virtualized realtime feed

### 7.3 `components/Crypto/`
- `EngineHeader`, `PredictionCards`, `TradesTable`, `ScalpingCard`, `MLTrainingCard`, `AutoResearchCard`, `EnhancedFeaturesPanel`, `ArbitragePanel`, `ConfigPanel`, `PerformanceMetrics`

### 7.4 `components/Weather/`
- `WeatherAlerts`, city tile, forecast chart, hourly strip

### 7.5 `components/Trading/`, `components/TradingWizard/`
- Order ticket, slippage estimator, sizing helper, paper/live switch

### 7.6 `components/AxiomEdge/`
- `BTCDirectionalTab`, `ScalpEngineTab`, `SportsMLTab`, `WeatherTab` — each a model-detail card grid

### 7.7 Operations / Risk / Validation
- `Operations/ControlPanel`, `EmergencyStopButton` (big red), `TradingModeIndicator`, `Risk/RiskMonitor`, `Validation/*`, `LiveControl/LiveControlPanel`, `GoLive/GoLiveDecision` + `GoLiveWizard`

### 7.8 Notifications & Alerts
- `Alerts/AlertSystem` (provider + toaster), `AlertCenter`, `AlertDetail`
- `Notifications/NotificationCenter`, `NotificationToast`
- `TradingAlerts/TradingAlertsBanner`
- `WebSocket/TradeToasts`

### 7.9 Pipeline / Audit
- `PipelineAuditTrail`, `Pipeline/PipelineFlowchart`, `Pipeline/ConsoleLogsPanel`

### 7.10 Misc
- `AxiomLogo` (SVG, glow option), `ConnectionBanner`, `ConfidenceBadge`, `ExchangeBadge`, `MatchCard`, `PredictionCard`, `PredictionFeed`, `PortfolioPanel`, `EVComparisonPanel`, `LineMovementChart`, `KalshiDashboard`, `RiskMonitor` (legacy + new), `ProviderLogos`, `Concierge/ConciergeChat`, `SelfImprovementHealth/SelfImprovementHealth`, `Graduation/GraduationReport`(+`Enhanced`), `Settings/AggressionMeter`, `AxiomGravity/AxiomGravity`

---

## 8. Hooks (`src/hooks/`)

| Hook | Responsibility |
|---|---|
| `useReconnectingWebSocket` | Auto-reconnect WS w/ status state |
| `useWebSocket` | Subscribe to typed channels |
| `useRealtimePnL` | Stream P&L deltas, derived KPIs |
| `useRealtimeResilience` | Backoff + heartbeat |
| `useNotifications` | Toast queue + DB-backed inbox |
| `useHealthData` | System health polling |
| `useMarketData` / `useCryptoMarketData` | Kalshi + Binance feeds |
| `useCryptoBinary` / `useCryptoPredictions` | Crypto engine state, predictions |
| `useSportsAnalytics` / `useKalshiSportsAnalytics` / `useSportsRecommendations` | Sport-side analytics |
| `useWeatherData` / `useWeatherAnalytics` / `useWeatherPnL` / `useTemperatureStream` | Weather pipeline |
| `useAxiomEdge` | Edge metrics |
| `useLLM` | LLM call wrapper |
| `useBankroll` | Bankroll history |
| `useSettings` | User settings CRUD |
| `useDebounce` | Standard input debounce |

---

## 9. Contexts

| Context | Purpose |
|---|---|
| `AuthContext` | `user`, `loading`, `login`, `logout`, `refreshUser`. Backed by HTTP-only cookie session. |
| `WebSocketContext` | Single shared WS, channel subscribe/unsubscribe |

---

## 10. Layout Patterns & Conventions

1. **Page shell:** `<div className="space-y-{4|5|6}">` for vertical rhythm
2. **Card:** `rounded-xl border border-white/[0.06] bg-navy-850 p-4` (or `p-5` / `p-6`)
3. **Glass card variant:** `glass rounded-xl p-4`
4. **Section header inside card:** `mb-3 text-sm font-semibold uppercase tracking-wider text-gray-400`
5. **KPI value:** `text-2xl font-bold text-white tabular-nums` + colored sub-label
6. **Color semantics:**
   - Green (`text-emerald-400` / `bg-emerald-500/10`) = profit / live / success
   - Red (`text-red-400` / `bg-red-500/10`) = loss / blocked / error
   - Amber (`text-amber-400` / `bg-amber-500/10`) = warning / paper / pending
   - Cyan (`text-cyan-400` / `bg-cyan-500/10`) = active selection / brand
   - Purple (`text-purple-400` / `bg-purple-500/10`) = AI / intelligence
7. **Tabs:** outer container `flex gap-1 rounded-lg border border-white/[0.06] bg-navy-900/50 p-1`; each tab `rounded-md px-4 py-2 text-sm font-medium`; active = `bg-cyan-500/10 text-cyan-400`
8. **Tables:** zebra (`bg-navy-850` / `bg-navy-900`), header `text-xs uppercase tracking-wider text-gray-500`, hover `bg-cyan-500/5`
9. **Charts:** Recharts with custom theme — gridlines `rgba(255,255,255,0.06)`, axes `text-gray-500`, lines cyan/green/red
10. **Responsive grid:** mobile single column → `sm:grid-cols-2` → `lg:grid-cols-3` → `xl:grid-cols-4`
11. **Sticky header inside scrollable lists:** `sticky top-0 bg-navy-900/90 backdrop-blur z-10`
12. **Skip-to-content link:** `<a href="#main-content" className="skip-to-content">` for a11y

---

## 11. Performance & UX Optimizations

- **Lazy routes** — every page is `React.lazy()`, falls back to `<PageFallback>` (skeleton stat-row + 2 chart skeletons + table skeleton)
- **Query caching** — `staleTime: 30s` avoids refetch storms
- **No automatic refetch on window focus** — prevents flicker during live trading
- **Service worker** registered in production (`/sw.js`); auto-unregistered + caches purged in dev
- **Mobile drawer** locks body scroll while open
- **WS auto-reconnect** w/ exponential backoff + status indicator dot in sidebar header
- **Light-theme override** swaps text-shadow off and remaps grayscale text colors

---

## 12. Authentication Flow

1. `<App>` mounts → `<AuthProvider>` calls `/api/auth/me` → sets `user` or `null`
2. `<AuthGate>` decides:
   - Public path (`/landing`, `/about`, `/pricing`, `/leaderboard`) → render that page
   - `/login` + already logged-in → redirect `/`
   - Loading + protected route → centered AxiomLogo + skeleton
   - No user → `<Navigate to="/landing">`
   - User exists but `onboarding_complete === false` → `<Navigate to="/onboarding">`
   - Otherwise → render full app shell with all providers

---

## 13. File Inventory Summary

| Folder | Files |
|---|---|
| `src/pages/` (top-level + subfolders) | **109** page/sub-page TSX files |
| `src/components/` | **~170** component TSX files (incl. tests) |
| `src/hooks/` | 22 |
| `src/contexts/` | 2 |
| **Total TSX/TS in pages+components** | **278** |

---

## 14. Quick Visual Identity

> Dark-first, **cyberpunk-finance** aesthetic: navy gradient backgrounds, cyan grid overlay, glass cards with subtle white borders, neon glow on actives, tabular numbers everywhere, sparingly used purple for AI features, color-coded P&L (green/red), pill-shaped tabs and badges. Inter font throughout. Animations are restrained — pulse for live data, slide-in for drawers, glow-pulse for important status dots.

— end of report —
