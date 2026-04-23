import { useState } from "react";
import {
  useGetRecentCycles,
  getGetRecentCyclesQueryKey,
} from "@workspace/api-client-react";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import SlippageGuardRejectsPanel from "./slippage-guard-rejects";

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
  const [expandedCycle, setExpandedCycle] = useState<number | null>(null);

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
              <th
                className="p-2 font-normal text-right"
                title="Trades skipped because the orderbook was too thin / would erode the model edge"
              >
                GUARD
              </th>
              <th className="p-2 font-normal">STATUS</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {items.length === 0 ? (
              <tr>
                <td colSpan={7} className="p-4 text-center text-muted-foreground">
                  NO_CYCLES_FOUND
                </td>
              </tr>
            ) : (
              items.flatMap((c) => {
                const hasError = !!c.errors;
                const placed = c.trades_placed ?? 0;
                const guard = c.slippage_guard_rejects ?? 0;
                const isExpanded = expandedCycle === c.id;
                const rows = [
                  <tr
                    key={c.id}
                    data-focus-id={`cycle-${c.id}`}
                    data-testid={`cycle-row-${c.id}`}
                    className="hover:bg-muted/30 transition-colors scroll-mt-24 data-[focus-flash=true]:outline data-[focus-flash=true]:outline-2 data-[focus-flash=true]:outline-accent-cyan data-[focus-flash=true]:bg-accent-cyan/10"
                  >
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
                    <td className="p-2 text-right">
                      {guard > 0 ? (
                        <button
                          type="button"
                          onClick={() =>
                            setExpandedCycle(isExpanded ? null : c.id)
                          }
                          className="font-bold text-loss underline-offset-2 hover:underline focus:outline-none focus:underline"
                          aria-expanded={isExpanded}
                          aria-controls={`slippage-guard-panel-${c.id}`}
                          data-testid={`guard-toggle-${c.id}`}
                        >
                          {guard}
                        </button>
                      ) : (
                        <span className="text-muted-foreground">0</span>
                      )}
                    </td>
                    <td
                      className={`p-2 uppercase text-[10px] ${hasError ? "text-loss" : c.completed_at ? "text-success" : "text-muted-foreground"}`}
                      title={c.errors ?? undefined}
                    >
                      {hasError ? "ERROR" : c.completed_at ? "DONE" : "RUN"}
                    </td>
                  </tr>,
                ];
                if (isExpanded) {
                  rows.push(
                    <tr key={`${c.id}-detail`}>
                      <td
                        colSpan={7}
                        id={`slippage-guard-panel-${c.id}`}
                        className="p-0"
                      >
                        <SlippageGuardRejectsPanel cycleId={c.id} />
                      </td>
                    </tr>,
                  );
                }
                return rows;
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
