import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useGetRecentTrades, getGetRecentTradesQueryKey } from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";

function formatMoney(num: number | null | undefined): string {
  if (num === null || num === undefined) return "—";
  const formatted = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Math.abs(num));
  return num < 0 ? `-${formatted}` : formatted;
}

export default function RecentTrades() {
  const params = { limit: 25 };
  const queryClient = useQueryClient();
  const { data, isLoading } = useGetRecentTrades(params, {
    query: { refetchInterval: 60_000, queryKey: getGetRecentTradesQueryKey(params) },
  });

  const onPipelineEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      if (
        t === "candidate_complete" ||
        t === "cycle_complete" ||
        t === "trade_settled"
      ) {
        queryClient.invalidateQueries({ queryKey: getGetRecentTradesQueryKey(params) });
      }
    },
    [queryClient],
  );
  const onSettlementEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      if (t === "trade_settled" || t === "market_settle_complete") {
        queryClient.invalidateQueries({ queryKey: getGetRecentTradesQueryKey(params) });
      }
    },
    [queryClient],
  );
  const pipeline = useWebSocket({ path: "/api/ws/pipeline", onMessage: onPipelineEvent, bufferSize: 50 });
  useWebSocket({ path: "/api/ws/settlement", onMessage: onSettlementEvent, bufferSize: 50 });
  const { matches } = useVerticalFilter();

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-96 bg-card" />;
  }

  const allTrades = data?.items || [];
  const trades = allTrades.filter((t) => matches(t.vertical));
  const live = pipeline.status === "open";

  return (
    <div className="border border-border bg-card overflow-hidden flex flex-col h-[500px]">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <div className="flex items-center gap-2">
          <span
            className={`w-1.5 h-1.5 rounded-full ${live ? "bg-success animate-pulse" : "bg-muted-foreground"}`}
          />
          <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">RECENT_FILLS</h2>
        </div>
        <span className="text-xs text-muted-foreground">LAST 25</span>
      </div>
      
      <div className="overflow-auto grow">
        <table className="w-full text-xs text-left">
          <thead className="sticky top-0 bg-card border-b border-border text-muted-foreground uppercase text-[10px]">
            <tr>
              <th className="p-2 font-normal">TIME</th>
              <th className="p-2 font-normal">MARKET</th>
              <th className="p-2 font-normal">SIDE/ACT</th>
              <th className="p-2 font-normal text-right">QTY</th>
              <th className="p-2 font-normal text-right">PRICE</th>
              <th className="p-2 font-normal text-right">PNL</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {trades.length === 0 ? (
              <tr>
                <td colSpan={6} className="p-4 text-center text-muted-foreground">NO_TRADES_FOUND</td>
              </tr>
            ) : trades.map((trade) => {
              const isLoss = trade.pnl !== null && trade.pnl !== undefined && trade.pnl < 0;
              const isWin = trade.pnl !== null && trade.pnl !== undefined && trade.pnl > 0;
              const pnlClass = isLoss ? "text-loss" : isWin ? "text-win" : "text-muted-foreground";
              
              const isBuy = trade.action?.toLowerCase() === 'buy';
              const actionClass = isBuy ? "text-primary" : "text-loss";

              return (
                <tr key={trade.id} className="hover:bg-muted/30 transition-colors">
                  <td className="p-2 whitespace-nowrap text-muted-foreground">
                    {trade.executed_at ? new Date(trade.executed_at).toLocaleTimeString([], {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'}) : "—"}
                  </td>
                  <td className="p-2 max-w-[200px] truncate" title={trade.market_id || ""}>
                    {trade.market_title || trade.market_id || "—"}
                  </td>
                  <td className="p-2 whitespace-nowrap">
                    <span className="uppercase">{trade.side || "—"}</span> / <span className={`${actionClass} uppercase`}>{trade.action || "—"}</span>
                  </td>
                  <td className="p-2 text-right">{trade.quantity ?? "—"}</td>
                  <td className="p-2 text-right">{formatMoney(trade.price)}</td>
                  <td className={`p-2 text-right font-bold ${pnlClass}`}>
                    {formatMoney(trade.pnl)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
