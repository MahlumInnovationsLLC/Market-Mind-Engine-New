import {
  getGetDashboardEdgeTerminalPnlQueryKey,
  useGetDashboardEdgeTerminalPnl,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { TileStaleBadge, useTileStaleClass } from "./tile-stale-badge";
import { useEdgeTerminalSourceStale } from "./source-filter-context";

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
function fmtUnits(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}u`;
}

export default function EdgeTerminalPnlTile() {
  const pnlKey = getGetDashboardEdgeTerminalPnlQueryKey();
  const { data, isLoading, error } = useGetDashboardEdgeTerminalPnl({
    query: { queryKey: pnlKey, refetchInterval: 30_000, staleTime: 25_000 },
  });
  // Migrated to the shared poller hook so every dashboard tile shows
  // the identical stale indicator instead of this tile keeping its
  // bespoke "Stale poller" badge (task #846 code review).
  const { stale, ageSeconds } = useEdgeTerminalSourceStale();
  const staleClass = useTileStaleClass();
  const pnl = data?.cumulative_pnl ?? null;
  const byVertical = data?.by_vertical ?? [];
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
      className={staleClass}
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
      <TileStaleBadge testId="edge-terminal-stale-badge" />
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
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-3">
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
              <div
                data-testid="edge-terminal-pnl-units"
                className={`mt-0.5 text-[11px] tabular-nums ${pnlColor}`}
              >
                {fmtUnits(data?.equivalent_units)}
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
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                Equivalent units
              </div>
              <div
                className={`text-2xl font-bold ${pnlColor}`}
                data-testid="edge-terminal-pnl-units-headline"
              >
                {fmtUnits(data?.equivalent_units)}
              </div>
              <div className="mt-1 text-[10px] text-muted-foreground">
                1u = $100 flat
              </div>
            </div>
          </div>
          {byVertical.length > 0 ? (
            <div
              className="border-t border-border pt-2"
              data-testid="edge-terminal-pnl-by-vertical"
            >
              <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
                By vertical
              </div>
              <div className="grid grid-cols-1 gap-1 text-[11px]">
                {byVertical.map((row, i) => {
                  const label = row.vertical || "—";
                  const rowPnl = Number(row.pnl ?? 0);
                  const rowColor =
                    rowPnl > 0
                      ? "text-emerald-400"
                      : rowPnl < 0
                        ? "text-rose-400"
                        : "text-foreground";
                  return (
                    <div
                      key={`${label}-${i}`}
                      className="flex items-center justify-between gap-3 border border-border/40 bg-card/40 px-2 py-1"
                      data-testid={`edge-terminal-pnl-vertical-${label}`}
                    >
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="truncate font-semibold uppercase tracking-wider text-foreground">
                          {label}
                        </span>
                        <span className="text-[10px] text-muted-foreground">
                          n={row.n}
                          {typeof row.n_settled === "number"
                            ? ` · ${row.n_settled} settled`
                            : ""}
                        </span>
                      </div>
                      <div className="flex items-center gap-3 tabular-nums">
                        <span className={rowColor}>{fmtUSD(rowPnl)}</span>
                        <span
                          className={`text-[10px] ${rowColor}`}
                          data-testid={`edge-terminal-pnl-vertical-${label}-units`}
                        >
                          {fmtUnits(row.equivalent_units ?? rowPnl / 100)}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </GlassCard>
  );
}
