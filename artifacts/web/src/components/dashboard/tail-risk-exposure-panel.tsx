import { useQuery } from "@tanstack/react-query";

import { resolveApiUrl } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import { usePlacedBy } from "./placed-by-context";

/**
 * Tail-risk exposure tile (task #744).
 *
 * Live read-only matrix surfaced from `/api/public/tail-risk-exposure`.
 * Forensic audit of YTD 2026 SYSTEM trades found that 0.28% of trades
 * (15 of 5,347) drove −$9,604 of cumulative losses on a −$5,878 net
 * book.  The two patterns: deep-OTM tail bets at `t.price < $0.10`
 * (one Feb-20 NBA NO at $0.04 / qty 4,394 lost −$4,218 by itself) and
 * jumbo bets at `t.quantity ≥ 1,000`.  This tile makes the live
 * concentration of those bets visible right under the KPI strip so the
 * operator sees today's tail-risk exposure before the next blow-up.
 *
 * Threads the same operator filters (`placedBy` + `effectiveVerticals`)
 * as KpiSummaryBar / EquityCharts so the tile narrows in lock-step
 * when the operator toggles Manual/System/Both or picks a vertical.
 *
 * Bucket boundaries are owned by the server — the response includes
 * `lo`/`hi`/`label` per bucket; this component does NOT re-bucket so
 * any future taxonomy change (or per-vertical override) flows through
 * without a client redeploy.
 */

type BucketRow = {
  vertical: string;
  open_count: number;
  open_cost_usd: number;
  open_max_loss_usd: number;
  realized_count: number;
  realized_wins: number;
  realized_pnl_usd: number;
};

type Bucket = {
  label: string;
  lo: number;
  hi: number;
  rows: BucketRow[];
  totals: {
    open_count: number;
    open_cost_usd: number;
    open_max_loss_usd: number;
    realized_count: number;
    realized_wins: number;
    realized_pnl_usd: number;
  };
};

type TailRiskExposure = {
  days: number;
  buckets: Bucket[];
  totals: Bucket["totals"];
  tail_concentration: {
    tail_max_loss_usd: number;
    total_max_loss_usd: number;
    pct: number | null;
    tone: "red" | "amber" | "green" | "empty";
  };
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

function pnlClass(n: number): string {
  if (n < 0) return "text-loss";
  if (n > 0) return "text-win";
  return "text-foreground";
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

export default function TailRiskExposurePanel() {
  // Match the threading pattern KpiSummaryBar / EquityCharts already
  // use: provenance + verticals are part of the cache key so toggles
  // refetch instead of replaying stale data.
  const { placedBy } = usePlacedBy();
  const { effectiveVerticals } = useVerticalFilter();
  const verticalsCsv = effectiveVerticals.slice().sort().join(",");

  const q = useQuery<TailRiskExposure>({
    queryKey: ["public-tail-risk-exposure", placedBy, verticalsCsv],
    queryFn: async () => {
      const params = new URLSearchParams({
        days: "30",
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

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-tail-risk-exposure"
    >
      <SectionHeader
        eyebrow="RISK"
        title="Tail-risk exposure"
        subtitle="Open positions and 30-day realized P&L bucketed by entry price. Forensic audit: 0.28% of YTD trades drove −$9.6K of losses — this tile keeps that risk visible."
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
          {/* Headline — concentration of OPEN max-loss exposure in the
              two leftmost (sub-$0.25) buckets.  Tone classification is
              owned server-side so every surface (web, mobile, alerts)
              agrees on what "elevated" means. */}
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
            <span className="text-[10px] text-muted-foreground/80">
              {fmtUsd(concentration.tail_max_loss_usd)} of {fmtUsd(concentration.total_max_loss_usd)} open max-loss
              sits in price &lt;$0.25 buckets
            </span>
          </div>

          {/* Bucket table.  No client-side bucketing — server owns the
              taxonomy and we just render what came back. */}
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
                  <th className="text-right font-normal py-1">Realized P&L (30d)</th>
                </tr>
              </thead>
              <tbody>
                {buckets.map((b, idx) => {
                  const isTail = b.lo < 0.20;
                  const totals = b.totals;
                  const winRate =
                    totals.realized_count > 0
                      ? totals.realized_wins / totals.realized_count
                      : null;
                  return (
                    <tr
                      key={b.label}
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
                        className={`py-1.5 text-right tabular-nums ${pnlClass(totals.realized_pnl_usd)}`}
                      >
                        {fmtUsd(totals.realized_pnl_usd)}
                      </td>
                    </tr>
                  );
                })}
                {/* Grand totals — match the bucket-row layout so the eye
                    can scan straight down the columns. */}
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
                    className={`py-1.5 text-right tabular-nums ${pnlClass(data.totals.realized_pnl_usd)}`}
                  >
                    {fmtUsd(data.totals.realized_pnl_usd)}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          {/* Per-vertical breakdown for the tail buckets only — keeps
              the audit's "lost −$5,607 in sports alone" signal visible
              without bloating the main table.  Aggregates the
              per-(bucket × vertical) cells for the two leftmost buckets
              into a single per-vertical strip, sorted by max-loss
              descending so the worst offender is left-most. */}
          {(() => {
            const tailRows = buckets
              .filter((b) => b.lo < 0.20)
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
                    {agg.open}× / {fmtUsd(agg.maxLoss)} max-loss / 30d{" "}
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
