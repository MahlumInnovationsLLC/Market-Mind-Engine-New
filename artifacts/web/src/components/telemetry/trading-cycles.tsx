import { useGetRecentCycles, getGetRecentCyclesQueryKey } from "@workspace/api-client-react";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";

function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = (seconds - m * 60).toFixed(0);
  return `${m}m ${s}s`;
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export default function TradingCycles() {
  const params = { limit: 10 };
  const { data, isLoading } = useGetRecentCycles(params, {
    query: { refetchInterval: 30000, queryKey: getGetRecentCyclesQueryKey(params) },
  });

  const { matches } = useVerticalFilter();

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-64 bg-card" />;
  }

  const allItems = data?.items ?? [];
  const items = allItems.filter((c) => matches(c.vertical));

  return (
    <div className="border border-border bg-card flex flex-col">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          TRADING_CYCLES
        </h2>
        <span className="text-xs text-muted-foreground">LAST 10 LOOPS</span>
      </div>
      <div className="overflow-auto">
        <table className="w-full text-xs text-left">
          <thead className="sticky top-0 bg-card border-b border-border text-muted-foreground uppercase text-[10px]">
            <tr>
              <th className="p-2 font-normal">START</th>
              <th className="p-2 font-normal">VERTICAL</th>
              <th className="p-2 font-normal text-right">DURATION</th>
              <th className="p-2 font-normal text-right">EVAL</th>
              <th className="p-2 font-normal text-right">PLACED</th>
              <th className="p-2 font-normal">STATUS</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {items.length === 0 ? (
              <tr>
                <td colSpan={6} className="p-4 text-center text-muted-foreground">
                  NO_CYCLES_FOUND
                </td>
              </tr>
            ) : (
              items.map((c) => {
                const hasError = !!c.errors;
                const placed = c.trades_placed ?? 0;
                return (
                  <tr key={c.id} className="hover:bg-muted/30 transition-colors">
                    <td className="p-2 text-muted-foreground whitespace-nowrap">
                      {fmtTime(c.started_at)}
                    </td>
                    <td className="p-2 uppercase text-primary">{c.vertical ?? "—"}</td>
                    <td className="p-2 text-right whitespace-nowrap">
                      {fmtDuration(c.duration_seconds)}
                    </td>
                    <td className="p-2 text-right">{c.picks_evaluated ?? "—"}</td>
                    <td
                      className={`p-2 text-right font-bold ${placed > 0 ? "text-win" : "text-muted-foreground"}`}
                    >
                      {c.trades_placed ?? "—"}
                    </td>
                    <td
                      className={`p-2 uppercase text-[10px] ${hasError ? "text-loss" : c.completed_at ? "text-success" : "text-muted-foreground"}`}
                      title={c.errors ?? undefined}
                    >
                      {hasError ? "ERROR" : c.completed_at ? "DONE" : "RUN"}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
