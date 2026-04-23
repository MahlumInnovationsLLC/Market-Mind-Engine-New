import { useEffect } from "react";
import { useLocation, useSearch } from "wouter";
import {
  getGetMyAccessQueryKey,
  useGetMyAccess,
} from "@workspace/api-client-react";
import ActivePositions from "@/components/dashboard/active-positions";
import EquityCharts from "@/components/dashboard/equity-charts";
import HighlightsPanel from "@/components/dashboard/highlights-panel";
import KpiSummaryBar from "@/components/dashboard/kpi-summary-bar";
import OperatorControls from "@/components/dashboard/operator-controls";
import { PeriodProvider, PeriodStrip } from "@/components/dashboard/period-context";
import PipelineTracker from "@/components/dashboard/pipeline-tracker";
import TradeStatsPanel from "@/components/dashboard/trade-stats-panel";
import VerticalCards from "@/components/dashboard/vertical-cards";

import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

import HealthSnapshot from "@/components/telemetry/health-snapshot";
import RecentTrades from "@/components/telemetry/recent-trades";
import TradingCycles from "@/components/telemetry/trading-cycles";
import SwarmAnalysts from "@/components/telemetry/swarm-analysts";
import SwarmPersonasToday from "@/components/telemetry/swarm-personas-today";
import SwarmDebates from "@/components/telemetry/swarm-debates";
import VerticalPicker from "@/components/telemetry/vertical-picker";
import ActiveModelBadge from "@/components/telemetry/active-model-badge";

export default function Dashboard() {
  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });
  const isAdmin = !!access?.is_admin;

  const search = useSearch();
  const [location, setLocation] = useLocation();
  useEffect(() => {
    const focus = new URLSearchParams(search).get("focus");
    if (!focus) return;

    let cancelled = false;
    let attempts = 0;
    let flashTimer: number | undefined;

    const tryFocus = () => {
      if (cancelled) return;
      const el = document.querySelector<HTMLElement>(
        `[data-focus-id="${CSS.escape(focus)}"]`,
      );
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.dataset.focusFlash = "true";
        flashTimer = window.setTimeout(() => {
          delete el.dataset.focusFlash;
        }, 2500);
        // Strip the focus param so re-renders don't re-trigger.
        const next = new URLSearchParams(search);
        next.delete("focus");
        const qs = next.toString();
        setLocation(qs ? `${location}?${qs}` : location, { replace: true });
        return;
      }
      attempts += 1;
      if (attempts < 30) {
        window.setTimeout(tryFocus, 100);
      }
    };
    // Defer to next paint so freshly-mounted lists are in the DOM.
    const start = window.setTimeout(tryFocus, 50);
    return () => {
      cancelled = true;
      window.clearTimeout(start);
      if (flashTimer !== undefined) window.clearTimeout(flashTimer);
    };
  }, [search, location, setLocation]);

  return (
    <PeriodProvider>
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
            <PeriodStrip />
          </div>
        </header>

        <KpiSummaryBar />

        <VerticalPicker isAdmin={isAdmin} />

        <EquityCharts />

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

        <OperatorControls isAdmin={isAdmin} />
      </div>
    </PeriodProvider>
  );
}
