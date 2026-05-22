import {
  getGetDashboardEdgeTerminalPnlQueryKey,
  getGetEdgeTerminalStatusQueryKey,
  useGetDashboardEdgeTerminalPnl,
  useGetEdgeTerminalStatus,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function fmtUSD(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  return `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`;
}
function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(Number(n) * 100).toFixed(1)}%`;
}
function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try { return new Date(s).toLocaleTimeString(); } catch { return s; }
}

export default function EdgeTerminalPnlTile() {
  const pnlKey = getGetDashboardEdgeTerminalPnlQueryKey();
  const { data, isLoading, error } = useGetDashboardEdgeTerminalPnl({
    query: { queryKey: pnlKey, refetchInterval: 30_000, staleTime: 25_000 },
  });
  const statusKey = getGetEdgeTerminalStatusQueryKey();
  const { data: status } = useGetEdgeTerminalStatus({
    query: { queryKey: statusKey, refetchInterval: 30_000, staleTime: 25_000 },
  });

  const stale = !!status?.stale;
  const pnl = data?.cumulative_pnl ?? null;
  const pnlColor =
    pnl === null || pnl === undefined
      ? "text-foreground"
      : pnl >= 0
        ? "text-emerald-400"
        : "text-rose-400";

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-edge-terminal-pnl"
      className={stale ? "opacity-60" : undefined}
    >
      <SectionHeader
        eyebrow="EDGE TERMINAL"
        title="Edge Terminal P&L"
        subtitle={
          data?.since_date
            ? `Trades placed by the Edge Terminal scraper since ${data.since_date}. Computed at ${fmtTime(data.generated_at)}.`
            : "Cumulative P&L on Edge Terminal scraper trades."
        }
      />
      {stale ? (
        <div
          data-testid="edge-terminal-stale-badge"
          className="mb-2 inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-300"
        >
          Stale poller
          {typeof status?.age_seconds === "number"
            ? ` · ${Math.round(status.age_seconds)}s`
            : ""}
        </div>
      ) : null}
      {isLoading ? (
        <div className="text-xs text-muted-foreground">Loading…</div>
      ) : error ? (
        <div
          data-testid="edge-terminal-pnl-error"
          className="text-xs text-rose-400"
        >
          Failed to load.
        </div>
      ) : data?.error ? (
        <div
          data-testid="edge-terminal-pnl-error"
          className="text-xs text-rose-400"
        >
          {data.error}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Cumulative P&L
            </div>
            <div
              data-testid="edge-terminal-pnl-cumulative"
              className={`text-2xl font-bold ${pnlColor}`}
            >
              {fmtUSD(pnl)}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Hit rate
            </div>
            <div className="text-2xl font-bold text-foreground">
              {fmtPct(data?.win_rate)}
            </div>
            <div className="mt-1 text-[10px] text-muted-foreground">
              {data?.n_settled ?? 0} settled · {data?.n_trades ?? 0} placed
            </div>
          </div>
        </div>
      )}
    </GlassCard>
  );
}
