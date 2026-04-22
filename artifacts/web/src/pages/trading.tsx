import { useEffect, useMemo, useState } from "react";
import { useLocation, useSearch } from "wouter";
import {
  getGetMyAccessQueryKey,
  useGetMyAccess,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import LiveExecuteToggle from "@/components/telemetry/live-execute-toggle";
import EmergencyStop from "@/components/telemetry/emergency-stop";
import RecentTrades from "@/components/telemetry/recent-trades";
import TradingCycles from "@/components/telemetry/trading-cycles";
import ActivePositions from "@/components/dashboard/active-positions";
import TradeStatsPanel from "@/components/dashboard/trade-stats-panel";

type TabKey = "live" | "feed";
const TABS: { key: TabKey; label: string; subtitle: string }[] = [
  { key: "live", label: "Live", subtitle: "Command center · positions · cycles" },
  { key: "feed", label: "Feed", subtitle: "Chronological execution stream" },
];

function parseTab(qs: string): TabKey {
  const params = new URLSearchParams(qs);
  const t = params.get("tab");
  return t === "feed" ? "feed" : "live";
}

export default function TradingPage() {
  const search = useSearch();
  const [, setLocation] = useLocation();
  const [tab, setTab] = useState<TabKey>(() => parseTab(search));

  // Sync state ← URL on back/forward navigation.
  useEffect(() => {
    const next = parseTab(search);
    setTab((prev) => (prev === next ? prev : next));
  }, [search]);

  const onSelectTab = (next: TabKey) => {
    setTab(next);
    const params = new URLSearchParams(search);
    if (next === "live") params.delete("tab");
    else params.set("tab", next);
    const q = params.toString();
    setLocation(`/trading${q ? `?${q}` : ""}`, { replace: false });
  };

  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });
  const isAdmin = !!access?.is_admin;

  const subtitle = useMemo(
    () => TABS.find((t) => t.key === tab)?.subtitle ?? "",
    [tab],
  );

  return (
    <div className="space-y-5 font-mono" data-testid="page-trading">
      <header className="flex flex-col gap-2">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
            EXECUTION
          </div>
          <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
            Trading
          </h1>
          <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>
        </div>
        <div
          className="flex gap-1 self-start rounded-md border border-border bg-card p-1"
          role="tablist"
          aria-label="Trading view"
          data-testid="tabs-trading"
        >
          {TABS.map((t) => {
            const active = t.key === tab;
            return (
              <button
                key={t.key}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => onSelectTab(t.key)}
                className={`rounded px-4 py-1.5 text-xs uppercase tracking-widest transition-colors ${
                  active
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:text-foreground"
                }`}
                data-testid={`tab-trading-${t.key}`}
              >
                {t.label}
              </button>
            );
          })}
        </div>
      </header>

      {tab === "live" ? (
        <LiveTab isAdmin={isAdmin} />
      ) : (
        <FeedTab />
      )}
    </div>
  );
}

function LiveTab({ isAdmin }: { isAdmin: boolean }) {
  return (
    <div className="space-y-4" data-testid="panel-trading-live">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="space-y-3 lg:col-span-1" data-testid="card-command-center">
          <LiveExecuteToggle />
          {isAdmin && <EmergencyStop />}
        </div>
        <div className="lg:col-span-2">
          <TradeStatsPanel />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <ActivePositions />
        <GlassCard tone="solid" padding="md" data-testid="card-live-fills">
          <SectionHeader
            eyebrow="EXECUTION"
            title="Recent fills"
            subtitle="Latest 25 settled and pending fills."
          />
          <RecentTrades />
        </GlassCard>
      </div>

      <GlassCard tone="solid" padding="md" data-testid="card-live-cycles">
        <SectionHeader
          eyebrow="ORCHESTRATOR"
          title="Trading cycles"
          subtitle="Last 10 loops · candidates evaluated · trades placed."
        />
        <TradingCycles />
      </GlassCard>
    </div>
  );
}

function FeedTab() {
  return (
    <div className="space-y-4" data-testid="panel-trading-feed">
      <GlassCard tone="solid" padding="md" data-testid="card-feed-fills">
        <SectionHeader
          eyebrow="STREAM"
          title="Trade feed"
          subtitle="Every fill, newest first. Updates live from the orchestrator and settlement WS."
        />
        <RecentTrades />
      </GlassCard>
      <GlassCard tone="solid" padding="md" data-testid="card-feed-cycles">
        <SectionHeader
          eyebrow="ORCHESTRATOR"
          title="Cycle log"
          subtitle="Last 10 trading-loop iterations."
        />
        <TradingCycles />
      </GlassCard>
    </div>
  );
}
