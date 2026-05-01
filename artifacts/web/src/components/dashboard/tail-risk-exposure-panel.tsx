import { Fragment } from "react";
import { useQuery } from "@tanstack/react-query";

import { resolveApiUrl } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import { usePeriod } from "./period-context";
import { usePlacedBy } from "./placed-by-context";

// Tail-risk exposure tile — read-only matrix from
// /api/public/tail-risk-exposure. Buckets, the row-level tail definition
// (price < tail_concentration.threshold_price), and ev_per_trade are
// owned by the server. See replit.md ("Tail-risk exposure tile").

type BucketRow = {
  vertical: string;
  open_count: number;
  open_cost_usd: number;
  open_max_loss_usd: number;
  realized_count: number;
  realized_wins: number;
  realized_pnl_usd: number;
  ev_per_trade: number | null;
};

type BucketTotals = {
  open_count: number;
  open_cost_usd: number;
  open_max_loss_usd: number;
  realized_count: number;
  realized_wins: number;
  realized_pnl_usd: number;
  ev_per_trade: number | null;
};

type Bucket = {
  label: string;
  lo: number;
  hi: number;
  is_tail: boolean;
  rows: BucketRow[];
  totals: BucketTotals;
};

type TailRiskExposure = {
  days: number;
  buckets: Bucket[];
  totals: BucketTotals;
  tail_concentration: {
    tail_max_loss_usd: number;
    total_max_loss_usd: number;
    pct: number | null;
    tone: "red" | "amber" | "green" | "empty";
    threshold_price: number;
  };
  source?: string;
  placed_by: string;
  verticals: string[];
};

function fmtUsd(n: number, fractionDigits = 0): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: 0,
  }).format(n);
}

function fmtPct(n: number | null, digits = 1): string {
  if (n === null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtEv(n: number | null): string {
  if (n === null || Number.isNaN(n)) return "—";
  // EV/trade is a small dollar number per trade — show 2 dp so a $0.50
  // edge doesn't round to "$1" and a −$0.30 drag doesn't round to "$0".
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
    minimumFractionDigits: 2,
    signDisplay: "exceptZero",
  }).format(n);
}

function fmtPriceThreshold(p: number): string {
  // Render `0.20` as `$0.20`, `0.2` as `$0.20`, `0.05` as `$0.05`.
  return `$${p.toFixed(2)}`;
}

function pnlClass(n: number): string {
  if (n < 0) return "text-loss";
  if (n > 0) return "text-win";
  return "text-foreground";
}

function evClass(n: number | null): string {
  if (n === null) return "text-muted-foreground";
  return pnlClass(n);
}

const TONE_HEADLINE_CLASS: Record<TailRiskExposure["tail_concentration"]["tone"], string> = {
  // Red is the audit-driven "this is what burned us last quarter"
  // colour — match the existing `text-loss` token so the tile keys off
  // the same palette as P&L Today, the equity chart's loss area, etc.
  red: "text-loss",
  amber: "text-warning",
  green: "text-win",
  empty: "text-muted-foreground",
};

const TONE_LABEL: Record<TailRiskExposure["tail_concentration"]["tone"], string> = {
  red: "ELEVATED",
  amber: "WATCH",
  green: "CONTAINED",
  empty: "NO EXPOSURE",
};

// Default threshold rendered in the headline copy when the server hasn't
// returned one yet (e.g. mid-loading first paint) — must match the
// server-side `_TAIL_PRICE_THRESHOLD` constant in `app/dashboard.py`.
const DEFAULT_TAIL_THRESHOLD = 0.2;

export default function TailRiskExposurePanel() {
  // Match the threading pattern KpiSummaryBar / EquityCharts already
  // use: provenance + verticals are part of the cache key so toggles
  // refetch instead of replaying stale data.
  const { placedBy } = usePlacedBy();
  const { effectiveVerticals } = useVerticalFilter();
  // Honour the dashboard's PeriodStrip (1d / 7d / 30d / YTD) just like
  // KpiSummaryBar and EquityCharts.  `days` is part of the cache key so
  // toggling 7d → 30d refetches immediately rather than serving stale.
  const { period, days } = usePeriod();
  const periodLabel = period.toUpperCase();
  const verticalsCsv = effectiveVerticals.slice().sort().join(",");

  const q = useQuery<TailRiskExposure>({
    queryKey: ["public-tail-risk-exposure", placedBy, verticalsCsv, days],
    queryFn: async () => {
      const params = new URLSearchParams({
        days: String(days),
        placed_by: placedBy,
      });
      if (verticalsCsv) params.set("vertical", verticalsCsv);
      const r = await fetch(
        resolveApiUrl(
          `${import.meta.env.BASE_URL}api/public/tail-risk-exposure?${params.toString()}`,
        ),
      );
      if (!r.ok) throw new Error(`tail-risk-exposure ${r.status}`);
      return r.json();
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const data = q.data;
  const buckets = data?.buckets ?? [];
  const concentration = data?.tail_concentration;
  const thresholdLabel = fmtPriceThreshold(
    concentration?.threshold_price ?? DEFAULT_TAIL_THRESHOLD,
  );

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-tail-risk-exposure"
    >
      <SectionHeader
        eyebrow={`RISK · ${periodLabel}`}
        title="Tail-risk exposure"
        subtitle="Open positions and realized P&L bucketed by entry price across the selected window. Forensic audit: 0.28% of YTD trades drove −$9.6K of losses — this tile keeps that risk visible."
      />

      {q.isLoading && (
        <div
          className="text-xs text-muted-foreground py-6 text-center"
          data-testid="tail-risk-loading"
        >
          Loading exposure…
        </div>
      )}

      {q.isError && (
        <div
          className="text-xs text-destructive py-6 text-center"
          data-testid="tail-risk-error"
        >
          Failed to load: {String((q.error as Error)?.message ?? q.error)}
        </div>
      )}

      {data && concentration && (
        <>
          {/* Headline: tone + threshold are server-owned. */}
          <div
            className="mb-4 flex flex-wrap items-baseline gap-x-3 gap-y-1"
            data-testid="tail-risk-headline"
          >
            <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
              TAIL CONCENTRATION
            </span>
            <span
              className={`text-2xl font-bold tabular-nums ${TONE_HEADLINE_CLASS[concentration.tone]}`}
              data-testid="tail-risk-headline-value"
              data-tone={concentration.tone}
            >
              {fmtPct(concentration.pct)}
            </span>
            <span
              className={`text-[10px] font-semibold uppercase tracking-widest ${TONE_HEADLINE_CLASS[concentration.tone]}`}
              data-testid="tail-risk-headline-tone"
            >
              {TONE_LABEL[concentration.tone]}
            </span>
            <span
              className="text-[10px] text-muted-foreground/80"
              data-testid="tail-risk-headline-detail"
            >
              {fmtUsd(concentration.tail_max_loss_usd)} of {fmtUsd(concentration.total_max_loss_usd)} open max-loss
              sits in trades priced &lt;{thresholdLabel}
            </span>
          </div>

          {/* Bucket table — server-owned taxonomy; rows include
              per-vertical breakdown for the full bucket × vertical matrix. */}
          <div className="overflow-x-auto">
            <table
              className="w-full text-xs"
              data-testid="tail-risk-table"
            >
              <thead>
                <tr className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  <th className="text-left font-normal py-1 pr-2">Price bucket</th>
                  <th className="text-right font-normal py-1 pr-2">Open #</th>
                  <th className="text-right font-normal py-1 pr-2">Cost basis</th>
                  <th className="text-right font-normal py-1 pr-2">Max loss</th>
                  <th className="text-right font-normal py-1 pr-2">Realized #</th>
                  <th className="text-right font-normal py-1 pr-2">Win rate</th>
                  <th className="text-right font-normal py-1 pr-2">EV/trade</th>
                  <th className="text-right font-normal py-1">{`Realized P&L (${periodLabel})`}</th>
                </tr>
              </thead>
              <tbody>
                {buckets.map((b, idx) => {
                  // Server-owned tail flag: `is_tail=true` ONLY when
                  // the whole bucket sits below the threshold.  The
                  // straddling `$0.10–$0.25` bucket is intentionally
                  // false so it doesn't get flagged as tail in the UI
                  // even though some of its rows feed the headline.
                  const isTail = b.is_tail;
                  const totals = b.totals;
                  const winRate =
                    totals.realized_count > 0
                      ? totals.realized_wins / totals.realized_count
                      : null;
                  // Sort cells by max-loss desc so the worst offender
                  // is the first nested row — operators scan top-down.
                  const cells = b.rows
                    .slice()
                    .sort((a, c) => c.open_max_loss_usd - a.open_max_loss_usd);
                  return (
                    <Fragment key={b.label}>
                      <tr
                        data-testid={`tail-risk-bucket-${idx}`}
                        data-bucket-label={b.label}
                        data-tail={isTail ? "true" : "false"}
                        className={`border-t border-border/40 ${isTail ? "bg-loss/5" : ""}`}
                      >
                        <td className="py-1.5 pr-2 font-medium text-foreground">
                          {b.label}
                          {isTail && (
                            <span className="ml-2 text-[9px] uppercase tracking-widest text-loss">
                              tail
                            </span>
                          )}
                        </td>
                        <td className="py-1.5 pr-2 text-right tabular-nums">
                          {totals.open_count}
                        </td>
                        <td className="py-1.5 pr-2 text-right tabular-nums">
                          {fmtUsd(totals.open_cost_usd)}
                        </td>
                        <td
                          className={`py-1.5 pr-2 text-right tabular-nums ${isTail && totals.open_max_loss_usd > 0 ? "text-loss font-semibold" : ""}`}
                        >
                          {fmtUsd(totals.open_max_loss_usd)}
                        </td>
                        <td className="py-1.5 pr-2 text-right tabular-nums">
                          {totals.realized_count}
                        </td>
                        <td className="py-1.5 pr-2 text-right tabular-nums">
                          {fmtPct(winRate, 0)}
                        </td>
                        <td
                          className={`py-1.5 pr-2 text-right tabular-nums ${evClass(totals.ev_per_trade)}`}
                          data-testid={`tail-risk-bucket-${idx}-ev`}
                        >
                          {fmtEv(totals.ev_per_trade)}
                        </td>
                        <td
                          className={`py-1.5 text-right tabular-nums ${pnlClass(totals.realized_pnl_usd)}`}
                        >
                          {fmtUsd(totals.realized_pnl_usd)}
                        </td>
                      </tr>
                      {/* Per-vertical sub-rows for this bucket. */}
                      {cells.map((cell) => {
                        const cellWinRate =
                          cell.realized_count > 0
                            ? cell.realized_wins / cell.realized_count
                            : null;
                        return (
                          <tr
                            key={`${b.label}-${cell.vertical}`}
                            className="text-muted-foreground/90"
                            data-testid={`tail-risk-cell-${idx}-${cell.vertical}`}
                          >
                            <td className="py-1 pr-2 pl-4 text-[11px]">
                              ↳ {cell.vertical}
                            </td>
                            <td className="py-1 pr-2 text-right tabular-nums text-[11px]">
                              {cell.open_count}
                            </td>
                            <td className="py-1 pr-2 text-right tabular-nums text-[11px]">
                              {fmtUsd(cell.open_cost_usd)}
                            </td>
                            <td className="py-1 pr-2 text-right tabular-nums text-[11px]">
                              {fmtUsd(cell.open_max_loss_usd)}
                            </td>
                            <td className="py-1 pr-2 text-right tabular-nums text-[11px]">
                              {cell.realized_count}
                            </td>
                            <td className="py-1 pr-2 text-right tabular-nums text-[11px]">
                              {fmtPct(cellWinRate, 0)}
                            </td>
                            <td
                              className={`py-1 pr-2 text-right tabular-nums text-[11px] ${evClass(cell.ev_per_trade)}`}
                            >
                              {fmtEv(cell.ev_per_trade)}
                            </td>
                            <td
                              className={`py-1 text-right tabular-nums text-[11px] ${pnlClass(cell.realized_pnl_usd)}`}
                            >
                              {fmtUsd(cell.realized_pnl_usd)}
                            </td>
                          </tr>
                        );
                      })}
                    </Fragment>
                  );
                })}
                {/* Grand totals row. */}
                <tr
                  className="border-t-2 border-border bg-muted/10 font-semibold"
                  data-testid="tail-risk-totals-row"
                >
                  <td className="py-1.5 pr-2 text-[10px] uppercase tracking-widest text-muted-foreground">
                    Total
                  </td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">
                    {data.totals.open_count}
                  </td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">
                    {fmtUsd(data.totals.open_cost_usd)}
                  </td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">
                    {fmtUsd(data.totals.open_max_loss_usd)}
                  </td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">
                    {data.totals.realized_count}
                  </td>
                  <td className="py-1.5 pr-2 text-right tabular-nums">
                    {fmtPct(
                      data.totals.realized_count > 0
                        ? data.totals.realized_wins / data.totals.realized_count
                        : null,
                      0,
                    )}
                  </td>
                  <td
                    className={`py-1.5 pr-2 text-right tabular-nums ${evClass(data.totals.ev_per_trade)}`}
                    data-testid="tail-risk-totals-ev"
                  >
                    {fmtEv(data.totals.ev_per_trade)}
                  </td>
                  <td
                    className={`py-1.5 text-right tabular-nums ${pnlClass(data.totals.realized_pnl_usd)}`}
                  >
                    {fmtUsd(data.totals.realized_pnl_usd)}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          {/* One-line per-vertical summary for tail buckets. */}
          {(() => {
            const tailRows = buckets
              .filter((b) => b.is_tail)
              .flatMap((b) => b.rows);
            if (tailRows.length === 0) return null;
            const byVertical = tailRows.reduce<
              Record<string, { open: number; maxLoss: number; pnl: number }>
            >((acc, r) => {
              const k = r.vertical;
              if (!acc[k]) acc[k] = { open: 0, maxLoss: 0, pnl: 0 };
              acc[k].open += r.open_count;
              acc[k].maxLoss += r.open_max_loss_usd;
              acc[k].pnl += r.realized_pnl_usd;
              return acc;
            }, {});
            const entries = Object.entries(byVertical).sort(
              (a, b) => b[1].maxLoss - a[1].maxLoss,
            );
            return (
              <div
                className="mt-3 text-[10px] text-muted-foreground/80"
                data-testid="tail-risk-vertical-breakdown"
              >
                <span className="uppercase tracking-widest">
                  Tail buckets by vertical:{" "}
                </span>
                {entries.map(([vertical, agg], i) => (
                  <span
                    key={vertical}
                    data-testid={`tail-risk-vertical-${vertical}`}
                  >
                    <span className="font-semibold text-foreground">
                      {vertical}
                    </span>{" "}
                    {agg.open}× / {fmtUsd(agg.maxLoss)} max-loss / {periodLabel}{" "}
                    <span className={pnlClass(agg.pnl)}>
                      {fmtUsd(agg.pnl)}
                    </span>
                    {i < entries.length - 1 ? " · " : ""}
                  </span>
                ))}
              </div>
            );
          })()}
        </>
      )}
    </GlassCard>
  );
}
