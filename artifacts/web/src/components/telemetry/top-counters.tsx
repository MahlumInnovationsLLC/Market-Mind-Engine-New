import { useGetDashboardOverview, getGetDashboardOverviewQueryKey } from "@workspace/api-client-react";

function formatNumber(num: number | null | undefined, isCurrency: boolean = false): string {
  if (num === null || num === undefined) return "—";
  if (isCurrency) {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(num);
  }
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(num);
}

export default function TopCounters() {
  const { data: overview, isLoading } = useGetDashboardOverview({
    query: { refetchInterval: 20000, queryKey: getGetDashboardOverviewQueryKey() },
  });

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-64 bg-card" />;
  }

  if (!overview) return null;

  const { counts, realized_pnl, win_rate, open_positions, latest_cycle_at, latest_trade_at } = overview;
  
  const isLoss = realized_pnl !== null && realized_pnl !== undefined && realized_pnl < 0;
  const isWin = realized_pnl !== null && realized_pnl !== undefined && realized_pnl > 0;

  return (
    <div className="border border-border bg-card">
      <div className="border-b border-border p-2 px-3 bg-muted/20">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">KPI_METRICS</h2>
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
          <div className="font-bold text-lg">{formatNumber(counts.trades)}</div>
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
