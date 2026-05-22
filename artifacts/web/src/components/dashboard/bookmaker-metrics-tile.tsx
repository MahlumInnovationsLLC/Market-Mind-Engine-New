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

/** Inline "?" affordance with a two-sentence plain-language explanation. */
function InfoTip({ help, testID }: { help: string; testID?: string }) {
  return (
    <button
      type="button"
      data-testid={testID}
      aria-label={help}
      title={help}
      className="ml-1 inline-flex h-4 w-4 items-center justify-center rounded-full border border-border text-[9px] leading-none text-muted-foreground hover:bg-muted"
    >
      ?
    </button>
  );
}

const HELP_PF =
  "Sum of winning P&L divided by sum of losing P&L on settled trades only. Open positions are excluded, so the number reflects fully-resolved bets in this window.";
const HELP_UNIT =
  "Net P&L normalised to a flat $100 reference stake (1u = $100). Our live sizing is fractional Kelly, so this restated number is what an industry-standard flat-stake bettor would have earned.";
const HELP_HIT =
  "Share of settled trades in this window that closed in profit. The denominator is shown as n=… below the headline number.";
const HELP_TOPCONF =
  "The highest calibrated model probability across today's pending, gate-cleared candidates. It is the most confident pick the swarm is currently offering for execution.";
const HELP_HIEDGE =
  "The largest EV edge percentage across today's pending, gate-cleared candidates. Same candidate pool used by the Top Edges tile above.";
const HELP_PICKS =
  "Count of today's pending, gate-cleared candidates broken out by vertical. These are the bets that would fire if all gates approve.";

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
          <div data-testid="bm-profit-factor">
            <div className="text-[10px] uppercase text-muted-foreground">
              Profit Factor
              <InfoTip help={HELP_PF} testID="bm-help-profit-factor" />
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtPF(data?.profit_factor)}
            </div>
          </div>
          <div data-testid="bm-unit-pnl">
            <div className="text-[10px] uppercase text-muted-foreground">
              Equiv. Unit P&amp;L
              <InfoTip help={HELP_UNIT} testID="bm-help-unit-pnl" />
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtUnits(data?.equivalent_unit_pnl)}
            </div>
            <div className="text-[9px] text-muted-foreground">
              1u = ${data?.unit_dollars ?? 100}
            </div>
          </div>
          <div data-testid="bm-hit-rate">
            <div className="text-[10px] uppercase text-muted-foreground">
              Hit Rate
              <InfoTip help={HELP_HIT} testID="bm-help-hit-rate" />
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtPctProb(data?.win_rate)}
            </div>
            <div className="text-[9px] text-muted-foreground">
              n = {data?.n_settled ?? 0}
            </div>
          </div>
          <div data-testid="bm-top-confidence">
            <div className="text-[10px] uppercase text-muted-foreground">
              Top Confidence (today)
              <InfoTip help={HELP_TOPCONF} testID="bm-help-top-confidence" />
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtPctProb(data?.top_confidence_today)}
            </div>
          </div>
          <div data-testid="bm-highest-edge">
            <div className="text-[10px] uppercase text-muted-foreground">
              Highest Edge (today)
              <InfoTip help={HELP_HIEDGE} testID="bm-help-highest-edge" />
            </div>
            <div className="text-base font-semibold tabular-nums">
              {fmtPctRaw(data?.highest_edge_today)}
            </div>
          </div>
          <div data-testid="bm-picks-today">
            <div className="text-[10px] uppercase text-muted-foreground">
              Picks Today
              <InfoTip help={HELP_PICKS} testID="bm-help-picks-today" />
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
