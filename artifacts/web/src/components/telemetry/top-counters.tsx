import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useGetDashboardOverview,
  getGetDashboardOverviewQueryKey,
  useGetVerticalsOverview,
  getGetVerticalsOverviewQueryKey,
} from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";
import { usePipelineSubscription } from "@/components/realtime/websocket-provider";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";

function formatNumber(num: number | null | undefined, isCurrency: boolean = false): string {
  if (num === null || num === undefined) return "—";
  if (isCurrency) {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(num);
  }
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(num);
}

export default function TopCounters() {
  const queryClient = useQueryClient();
  // T004: `useGetDashboardOverview` gained an optional `placed_by`
  // query param, which shifted its signature from `(opts)` to
  // `(params?, opts)`.  This panel is provenance-agnostic (raw counters
  // for the realtime telemetry pane), so we pass an empty params object
  // and keep the unfiltered behaviour.
  const { data: overview, isLoading } = useGetDashboardOverview(undefined, {
    query: { refetchInterval: 60_000, queryKey: getGetDashboardOverviewQueryKey() },
  });
  const { selected, matches, filterActive, hiddenActive } = useVerticalFilter();
  const verticalParams = { days: 7 };
  const { data: verticalOverview } = useGetVerticalsOverview(verticalParams, {
    query: {
      queryKey: getGetVerticalsOverviewQueryKey(verticalParams),
      staleTime: 60_000,
    },
  });

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: getGetDashboardOverviewQueryKey() });
  }, [queryClient]);

  const onPipelineEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      if (t === "cycle_complete" || t === "candidate_complete") invalidate();
    },
    [invalidate],
  );
  const onSettlementEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      if (t === "trade_settled" || t === "market_settle_complete" || t === "cycle_complete") {
        invalidate();
      }
    },
    [invalidate],
  );
  usePipelineSubscription(onPipelineEvent);
  useWebSocket({ path: "/api/ws/settlement", onMessage: onSettlementEvent, bufferSize: 50 });

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-64 bg-card" />;
  }

  if (!overview) return null;

  const filteredRows = (verticalOverview?.verticals ?? []).filter((v) =>
    matches(v.vertical),
  );
  const aggTrades = filteredRows.reduce((s, r) => s + (r.trades ?? 0), 0);
  const aggWins = filteredRows.reduce((s, r) => s + (r.wins ?? 0), 0);
  const aggLosses = filteredRows.reduce((s, r) => s + (r.losses ?? 0), 0);
  const aggPnl = filteredRows.reduce((s, r) => s + (r.pnl ?? 0), 0);
  const aggOpen = filteredRows.reduce(
    (s, r) => s + (r.open_positions ?? 0),
    0,
  );
  const aggDecided = aggWins + aggLosses;
  const aggWinRate = aggDecided > 0 ? aggWins / aggDecided : null;

  const counts = overview.counts;
  const realized_pnl = filterActive ? aggPnl : overview.realized_pnl;
  const win_rate = filterActive ? aggWinRate : overview.win_rate;
  const open_positions = filterActive ? aggOpen : overview.open_positions;
  const tradesCount = filterActive ? aggTrades : counts.trades;
  const latest_cycle_at = overview.latest_cycle_at;
  const latest_trade_at = overview.latest_trade_at;
  
  const isLoss = realized_pnl !== null && realized_pnl !== undefined && realized_pnl < 0;
  const isWin = realized_pnl !== null && realized_pnl !== undefined && realized_pnl > 0;

  return (
    <div className="border border-border bg-card">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">KPI_METRICS</h2>
        {filterActive ? (
          <span
            data-testid="badge-kpi-filtered"
            className="text-[10px] uppercase tracking-widest text-primary"
          >
            FILTERED ·{" "}
            {selected.length > 0
              ? selected.join(", ")
              : hiddenActive
                ? "hidden suppressed"
                : ""}
          </span>
        ) : null}
      </div>
      
      <div className="p-4 border-b border-border bg-background">
        <div className="text-xs text-muted-foreground mb-1 uppercase tracking-widest">REALIZED_PNL</div>
        <div className={`text-3xl font-bold ${isLoss ? "text-loss" : isWin ? "text-win" : "text-foreground"}`}>
          {formatNumber(realized_pnl, true)}
        </div>
      </div>

      <div className="grid grid-cols-2 divide-x divide-y divide-border text-xs border-b border-border">
        <div className="p-3">
          <div className="text-muted-foreground mb-1">WIN_RATE</div>
          <div className="font-bold text-lg">{win_rate !== null && win_rate !== undefined ? `${(win_rate * 100).toFixed(1)}%` : "—"}</div>
        </div>
        <div className="p-3">
          <div className="text-muted-foreground mb-1">OPEN_POS</div>
          <div className="font-bold text-lg text-primary">{open_positions ?? "—"}</div>
        </div>
        <div className="p-3">
          <div className="text-muted-foreground mb-1">TRADES</div>
          <div className="font-bold text-lg">{formatNumber(tradesCount)}</div>
        </div>
        <div className="p-3">
          <div className="text-muted-foreground mb-1">CYCLES</div>
          <div className="font-bold text-lg">{formatNumber(counts.trading_cycles)}</div>
        </div>
        <div className="p-3">
          <div className="text-muted-foreground mb-1">DEBATES</div>
          <div className="font-bold text-lg">{formatNumber(counts.swarm_debates)}</div>
        </div>
        <div className="p-3">
          <div className="text-muted-foreground mb-1">ML_LOGS</div>
          <div className="font-bold text-lg">{formatNumber(counts.ml_training_logs)}</div>
        </div>
      </div>

      <div className="p-3 text-[10px] text-muted-foreground space-y-1">
        <div className="flex justify-between">
          <span>LST_CYCLE:</span>
          <span>{latest_cycle_at ? new Date(latest_cycle_at).toLocaleTimeString() : "—"}</span>
        </div>
        <div className="flex justify-between">
          <span>LST_TRADE:</span>
          <span>{latest_trade_at ? new Date(latest_trade_at).toLocaleTimeString() : "—"}</span>
        </div>
      </div>
    </div>
  );
}
