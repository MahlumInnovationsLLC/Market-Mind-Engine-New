import {
  getGetMyAccessQueryKey,
  useGetMyAccess,
} from "@workspace/api-client-react";
import { useFocusFromQuery } from "@/hooks/use-focus-from-query";
import EdgeTerminalPnlTile from "@/components/dashboard/edge-terminal-pnl-tile";
import EdgeTerminalRecentSignalsTile from "@/components/dashboard/edge-terminal-recent-signals";
import InputModeToggle from "@/components/dashboard/input-mode-toggle";
import {
  SourceFilterProvider,
  useSourceFilter,
} from "@/components/dashboard/source-filter-context";
import ActivePositions from "@/components/dashboard/active-positions";
import BankrollPanel from "@/components/dashboard/bankroll-panel";
import CryptoBacktestRetentionPanel from "@/components/dashboard/crypto-backtest-retention-panel";
import CryptoOhlcvRetentionPanel from "@/components/dashboard/crypto-ohlcv-retention-panel";
import SportradarLeaderboardsRetentionPanel from "@/components/dashboard/sportradar-leaderboards-retention-panel";
import SupabaseGrowthRetentionPanel from "@/components/dashboard/supabase-growth-retention-panel";
import EquityCharts from "@/components/dashboard/equity-charts";
import HighlightsPanel from "@/components/dashboard/highlights-panel";
import KpiSummaryBar from "@/components/dashboard/kpi-summary-bar";
import TopEdgesTile from "@/components/dashboard/top-edges-tile";
import BookmakerMetricsTile from "@/components/dashboard/bookmaker-metrics-tile";
import MacroReleaseSurprises from "@/components/dashboard/macro-release-surprises";
import LeaguesExposurePanel from "@/components/dashboard/leagues-exposure-panel";
import OperatorControls from "@/components/dashboard/operator-controls";
import OverFilledOrdersBadge from "@/components/dashboard/over-filled-orders-badge";
import CalibratorAb from "@/components/dashboard/calibrator-ab";
import PerLeagueCalibration from "@/components/dashboard/per-league-calibration";
import V1BacktestGateTile from "@/components/dashboard/v1-backtest-gate-tile";
import { PeriodProvider, PeriodStrip } from "@/components/dashboard/period-context";
import { PlacedByProvider } from "@/components/dashboard/placed-by-context";
import { PlacedByToggle } from "@/components/dashboard/placed-by-toggle";
import PipelineTracker from "@/components/dashboard/pipeline-tracker";
import TailRiskExposurePanel from "@/components/dashboard/tail-risk-exposure-panel";
import BestLineAlertsPanel from "@/components/dashboard/best-line-alerts-panel";
import TelegramConcierge from "@/components/dashboard/telegram-concierge";
import TournamentLeaderboardsPanel from "@/components/dashboard/tournament-leaderboards-panel";
import TournamentReplaysPanel from "@/components/dashboard/tournament-replays-panel";
import TournamentTimelinePanel from "@/components/dashboard/tournament-timeline-panel";
import TradeStatsPanel from "@/components/dashboard/trade-stats-panel";
import VerticalCards from "@/components/dashboard/vertical-cards";

import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

import HealthSnapshot from "@/components/telemetry/health-snapshot";
import RecentTrades from "@/components/telemetry/recent-trades";
import TradingCycles from "@/components/telemetry/trading-cycles";
import SwarmAnalysts from "@/components/telemetry/swarm-analysts";
import SwarmPersonasToday from "@/components/telemetry/swarm-personas-today";
import SwarmFallbacksToday from "@/components/telemetry/swarm-fallbacks-today";
import ProviderFallbackRates from "@/components/telemetry/provider-fallback-rates";
import SwarmDebates from "@/components/telemetry/swarm-debates";
import VerticalPerformance from "@/components/telemetry/vertical-performance";
import VerticalPicker from "@/components/telemetry/vertical-picker";
import ActiveModelBadge from "@/components/telemetry/active-model-badge";

export default function Dashboard() {
  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });
  const isAdmin = !!access?.is_admin;

  useFocusFromQuery();

  return (
    <PeriodProvider>
     <PlacedByProvider>
      <SourceFilterProvider>
       <DashboardBody isAdmin={isAdmin} />
      </SourceFilterProvider>
     </PlacedByProvider>
    </PeriodProvider>
  );
}

function DashboardBody({ isAdmin }: { isAdmin: boolean }) {
  const { isEdgeTerminal, showPipelineData, setShowPipelineData, pipelineVisible } =
    useSourceFilter();
  return (
      <div className="space-y-5 font-mono">
        <header className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              OPERATOR CONSOLE
            </div>
            <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
              Overview
            </h1>
            <p className="mt-1 text-xs text-muted-foreground">
              Live KPIs, equity curve, per-vertical performance, and the swarm.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <ActiveModelBadge />
            <OverFilledOrdersBadge />
            <InputModeToggle
              showPipelineData={showPipelineData}
              onTogglePipelineData={setShowPipelineData}
            />
            {/* `isEdgeTerminal` / `pipelineVisible` drive the tile
                visibility below; they're sourced from
                <SourceFilterProvider> so every tile reads the same
                value the toggle just flipped. */}
            <PlacedByToggle />
            <PeriodStrip />
          </div>
        </header>

        <KpiSummaryBar />

        {isEdgeTerminal ? (
          <>
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
              <EdgeTerminalPnlTile />
              {pipelineVisible ? <BookmakerMetricsTile /> : null}
            </div>
            <EdgeTerminalRecentSignalsTile />
          </>
        ) : null}

        {pipelineVisible ? (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <TopEdgesTile />
            {!isEdgeTerminal ? <BookmakerMetricsTile /> : null}
          </div>
        ) : null}

        <TailRiskExposurePanel />

        <BestLineAlertsPanel />

        <VerticalPicker isAdmin={isAdmin} />

        <EquityCharts />

        <BankrollPanel />

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <div className="xl:col-span-2">
            <TradeStatsPanel />
          </div>
          <PipelineTracker />
        </div>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <ActivePositions />
          <GlassCard
            tone="solid"
            padding="md"
            data-testid="card-recent-trades"
          >
            <SectionHeader
              eyebrow="EXECUTION"
              title="Recent fills"
              subtitle="Latest 25 settled and pending fills."
            />
            <RecentTrades />
          </GlassCard>
        </div>

        <VerticalCards />

        <LeaguesExposurePanel />

        <TournamentLeaderboardsPanel />

        <TournamentTimelinePanel />

        <PerLeagueCalibration />

        <CalibratorAb />

        <V1BacktestGateTile />

        <TournamentReplaysPanel />

        <MacroReleaseSurprises />

        <VerticalPerformance />

        <HighlightsPanel />

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <GlassCard
            tone="solid"
            padding="md"
            className="xl:col-span-2"
            data-testid="card-trading-cycles"
          >
            <SectionHeader
              eyebrow="ORCHESTRATOR"
              title="Trading cycles"
              subtitle="Last 10 swarm loops with placement counts."
            />
            <TradingCycles />
          </GlassCard>
          <GlassCard
            tone="solid"
            padding="md"
            data-testid="card-health-snapshot"
          >
            <SectionHeader
              eyebrow="SYSTEM"
              title="Health snapshot"
              subtitle="Subsystem health rolled up live."
            />
            <HealthSnapshot />
          </GlassCard>
        </div>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <CryptoOhlcvRetentionPanel />
          <CryptoBacktestRetentionPanel />
          <SportradarLeaderboardsRetentionPanel />
        </div>

        <SupabaseGrowthRetentionPanel />

        <GlassCard tone="solid" padding="md" data-testid="card-swarm-debates">
          <SectionHeader
            eyebrow="SWARM"
            title="Recent debates"
            subtitle="Most recent consensus verdicts."
          />
          <SwarmDebates />
        </GlassCard>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <GlassCard tone="solid" padding="md" data-testid="card-swarm-analysts">
            <SectionHeader
              eyebrow="SWARM"
              title="Analyst leaderboard"
              subtitle="All-time per-persona Brier, calibration, and accuracy."
            />
            <SwarmAnalysts />
          </GlassCard>
          <GlassCard tone="solid" padding="md" data-testid="card-swarm-personas-today">
            <SectionHeader
              eyebrow="SWARM"
              title="Personas today"
              subtitle="Today's per-persona Brier, accuracy, and lean breakdown (UTC)."
            />
            <SwarmPersonasToday />
          </GlassCard>
        </div>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <GlassCard tone="solid" padding="md" data-testid="card-llm-fallback-rates-wrap">
            <SectionHeader
              eyebrow="SWARM"
              title="Provider fallback rate"
              subtitle="Per-provider LLM fallback rate over the same trailing window the alert uses. Watch a row creep up before it pages."
            />
            <ProviderFallbackRates />
          </GlassCard>
          <GlassCard tone="solid" padding="md" data-testid="card-swarm-fallbacks-today">
            <SectionHeader
              eyebrow="SWARM"
              title="Provider fallbacks today"
              subtitle="Per-persona count of cross-provider LLM fallbacks (UTC). A spike means a primary model is failing."
            />
            <SwarmFallbacksToday />
          </GlassCard>
        </div>

        <TelegramConcierge />

        <OperatorControls isAdmin={isAdmin} />
      </div>
  );
}
