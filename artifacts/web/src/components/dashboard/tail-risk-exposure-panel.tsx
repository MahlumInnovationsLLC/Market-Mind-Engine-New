import { Fragment } from "react";
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
 * Bucket boundaries AND the row-level tail definition are owned by the
 * server.  The response includes:
 *   - `lo`/`hi`/`label`/`is_tail` per bucket — `is_tail=true` only when
 *     the whole bucket sits below `tail_concentration.threshold_price`
 *     (currently $0.20).  The `$0.10–$0.25` bucket straddles that
 *     line, so its UI badge stays off and its share of the tail
 *     headline comes from the dedicated row-level aggregate the server
 *     computes against `price < threshold` (NOT from summing the whole
 *     bucket — that would over-count $0.20–$0.25 trades).
 *   - `totals.ev_per_trade` per bucket and grand total — surfaced by
 *     the server so every consumer (web, mobile, alerts) shares one
 *     definition.
 */

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
          {/* Headline — concentration of OPEN max-loss exposure that
              sits in trades priced below the server-published
              threshold.  Tone classification is owned server-side so
              every surface (web, mobile, alerts) agrees on what
              "elevated" means, and the threshold itself is read from
              the response so a future tightening (e.g. $0.20 → $0.15)
              flows through without a client redeploy. */}
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

          {/* Bucket table.  No client-side bucketing — server owns the
              taxonomy and we just render what came back.  Each bucket
              row is followed by its per-vertical breakdown rows so the
              full bucket × vertical matrix is visible without a
              drill-down click. */}
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
                  <th className="text-right font-normal py-1">Realized P&L (30d)</th>
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
                      {/* Per-vertical cells for this bucket — full
                          matrix view.  Indented and slightly muted so
                          they read as a sub-row of the bucket above. */}
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

          {/* Per-vertical breakdown for the tail buckets only — keeps
              the audit's "lost −$5,607 in sports alone" signal visible
              as a one-line summary even though the full matrix is
              already in the table above. */}
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
