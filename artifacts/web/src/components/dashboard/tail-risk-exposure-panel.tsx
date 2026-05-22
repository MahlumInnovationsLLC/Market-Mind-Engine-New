import { Fragment } from "react";
import { useLocation } from "wouter";
import { useQuery } from "@tanstack/react-query";

import { resolveApiUrl } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import { usePeriod } from "./period-context";
import { useEffectivePlacedBy } from "./source-filter-context";
import { TileStaleBadge, useTileStaleClass } from "./tile-stale-badge";

// See replit.md "Tail-risk exposure tile".

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
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
    minimumFractionDigits: 2,
    signDisplay: "exceptZero",
  }).format(n);
}

function fmtPriceThreshold(p: number): string {
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

const DEFAULT_TAIL_THRESHOLD = 0.2;

export default function TailRiskExposurePanel() {
  const placedBy = useEffectivePlacedBy();
  const tileStaleClass = useTileStaleClass();
  const { effectiveVerticals } = useVerticalFilter();
  const { period, days } = usePeriod();
  const [, setLocation] = useLocation();
  const periodLabel = period.toUpperCase();
  const verticalsCsv = effectiveVerticals.slice().sort().join(",");

  // Open the positions page pre-filtered to the price bucket the
  // operator clicked. We thread the same days/placed_by/vertical
  // filters that drove this tile so the resulting list lines up
  // with what they were just looking at, and request the
  // max-loss sort so the worst contributors land at the top.
  const openBucket = (b: Bucket) => {
    const params = new URLSearchParams();
    params.set("bucket_lo", String(b.lo));
    params.set("bucket_hi", String(b.hi));
    params.set("bucket_label", b.label);
    params.set("days", String(days));
    params.set("placed_by", placedBy);
    if (verticalsCsv) params.set("vertical", verticalsCsv);
    params.set("sort", "max_loss");
    setLocation(`/positions?${params.toString()}`);
  };

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
      className={tileStaleClass}
    >
      <TileStaleBadge testId="tail-risk-stale-badge" />
      <SectionHeader
        eyebrow={`RISK · ${periodLabel}`}
        title="Tail-risk exposure"
        subtitle="Open positions and realized P&L bucketed by entry price across the selected window."
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
                  const isTail = b.is_tail;
                  const totals = b.totals;
                  const winRate =
                    totals.realized_count > 0
                      ? totals.realized_wins / totals.realized_count
                      : null;
                  const cells = b.rows
                    .slice()
                    .sort((a, c) => c.open_max_loss_usd - a.open_max_loss_usd);
                  // Top 5 contributors by max-loss for the hover peek; we
                  // surface them in a `title` so operators can scan the
                  // worst names without leaving the dashboard.
                  const topContribs = cells.slice(0, 5);
                  const peekText =
                    totals.open_count === 0 && totals.realized_count === 0
                      ? `${b.label}: no exposure`
                      : `Top contributors in ${b.label} (sorted by max-loss):\n` +
                        topContribs
                          .map(
                            (c) =>
                              `• ${c.vertical} — ${fmtUsd(c.open_max_loss_usd)} max-loss · ${c.open_count} open`,
                          )
                          .join("\n") +
                        `\n\nClick to open the matching trades.`;
                  const isInteractive =
                    totals.open_count > 0 || totals.realized_count > 0;
                  return (
                    <Fragment key={b.label}>
                      <tr
                        data-testid={`tail-risk-bucket-${idx}`}
                        data-bucket-label={b.label}
                        data-tail={isTail ? "true" : "false"}
                        title={peekText}
                        role={isInteractive ? "button" : undefined}
                        tabIndex={isInteractive ? 0 : undefined}
                        aria-label={
                          isInteractive
                            ? `Open trades in price bucket ${b.label}`
                            : undefined
                        }
                        onClick={isInteractive ? () => openBucket(b) : undefined}
                        onKeyDown={
                          isInteractive
                            ? (e) => {
                                if (e.key === "Enter" || e.key === " ") {
                                  e.preventDefault();
                                  openBucket(b);
                                }
                              }
                            : undefined
                        }
                        className={`border-t border-border/40 ${isTail ? "bg-loss/5" : ""} ${
                          isInteractive
                            ? "cursor-pointer hover:bg-primary/10 focus:outline focus:outline-2 focus:outline-primary"
                            : ""
                        }`}
                      >
                        <td className="py-1.5 pr-2 font-medium text-foreground">
                          {b.label}
                          {isTail && (
                            <span className="ml-2 text-[9px] uppercase tracking-widest text-loss">
                              tail
                            </span>
                          )}
                          {isInteractive && (
                            <span
                              className="ml-2 text-[9px] uppercase tracking-widest text-primary/70"
                              data-testid={`tail-risk-bucket-${idx}-drill`}
                            >
                              drill ↗
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
