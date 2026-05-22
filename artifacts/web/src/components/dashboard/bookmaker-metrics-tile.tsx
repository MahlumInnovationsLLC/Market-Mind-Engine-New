import { useState } from "react";
import {
  getGetDashboardBookmakerMetricsQueryKey,
  useGetDashboardBookmakerMetrics,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

type Window = "7d" | "30d" | "ytd";
const WINDOWS: Window[] = ["7d", "30d", "ytd"];

function fmtPF(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toFixed(2);
}
function fmtUnits(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}u`;
}
function fmtPctProb(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(Number(n) * 100).toFixed(digits)}%`;
}
function fmtPctRaw(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${Number(n).toFixed(digits)}%`;
}
function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try { return new Date(s).toLocaleTimeString(); } catch { return s; }
}

export default function BookmakerMetricsTile() {
  const [window, setWindow] = useState<Window>("7d");
  const queryKey = getGetDashboardBookmakerMetricsQueryKey({ window });
  const { data, isLoading, error } = useGetDashboardBookmakerMetrics(
    { window },
    { query: { queryKey, refetchInterval: 30_000, staleTime: 25_000 } },
  );

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-bookmaker-metrics">
      <SectionHeader
        eyebrow="BOOKMAKER"
        title="Bookmaker Metrics"
        subtitle={
          data?.generated_at
            ? `Profit factor · equivalent-unit P&L · hit rate. Computed at ${fmtTime(data.generated_at)}.`
            : "Profit factor · equivalent-unit P&L · hit rate."
        }
      />
      <div className="mt-2 flex gap-1" role="tablist" aria-label="Window">
        {WINDOWS.map((w) => (
          <button
            key={w}
            type="button"
            role="tab"
            aria-selected={w === window}
            data-testid={`bm-window-${w}`}
            className={
              "rounded border px-2 py-0.5 text-[10px] uppercase " +
              (w === window
                ? "border-accent-cyan bg-muted text-foreground"
                : "border-border text-muted-foreground hover:bg-muted")
            }
            onClick={() => setWindow(w)}
          >
            {w.toUpperCase()}
          </button>
        ))}
      </div>
      {error ? (
        <div className="mt-3 text-xs text-destructive" role="alert">
          Failed to load Bookmaker Metrics.
        </div>
      ) : null}
      {data?.error ? (
        <div className="mt-3 text-xs text-destructive" role="alert">
          {data.error}
        </div>
      ) : null}
      {isLoading && !data ? (
        <div className="mt-3 h-24 w-full animate-pulse rounded bg-muted/50" />
      ) : (
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
          <div
            data-testid="bm-profit-factor"
            title="Sum of winning P&L divided by sum of losing P&L on settled trades only. Open positions excluded."
          >
            <div className="text-[10px] uppercase text-muted-foreground">
              Profit Factor
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtPF(data?.profit_factor)}
            </div>
          </div>
          <div
            data-testid="bm-unit-pnl"
            title={`Equivalent-unit P&L (1u = $${data?.unit_dollars ?? 100}). Live sizing is fractional Kelly; this normalises P&L to a flat reference stake for industry comparability.`}
          >
            <div className="text-[10px] uppercase text-muted-foreground">
              Equiv. Unit P&amp;L
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtUnits(data?.equivalent_unit_pnl)}
            </div>
            <div className="text-[9px] text-muted-foreground">
              1u = ${data?.unit_dollars ?? 100}
            </div>
          </div>
          <div
            data-testid="bm-hit-rate"
            title="Hit rate over settled trades in this window."
          >
            <div className="text-[10px] uppercase text-muted-foreground">
              Hit Rate
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtPctProb(data?.win_rate)}
            </div>
            <div className="text-[9px] text-muted-foreground">
              n = {data?.n_settled ?? 0}
            </div>
          </div>
          <div
            data-testid="bm-top-confidence"
            title="Maximum calibrated model probability across today's pending candidates."
          >
            <div className="text-[10px] uppercase text-muted-foreground">
              Top Confidence (today)
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtPctProb(data?.top_confidence_today)}
            </div>
          </div>
          <div
            data-testid="bm-highest-edge"
            title="Max |edge| across today's pending candidates."
          >
            <div className="text-[10px] uppercase text-muted-foreground">
              Highest Edge (today)
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtPctRaw(data?.highest_edge_today)}
            </div>
          </div>
          <div data-testid="bm-picks-today">
            <div className="text-[10px] uppercase text-muted-foreground">
              Picks Today
            </div>
            <div className="text-base font-semibold tabular-nums">
              {data?.picks_today?.total ?? 0}
            </div>
            <div className="text-[9px] text-muted-foreground">
              {(data?.picks_today?.by_vertical ?? [])
                .map((b) => `${b.vertical}:${b.n}`)
                .join(" ") || "—"}
            </div>
          </div>
        </div>
      )}
    </GlassCard>
  );
}
