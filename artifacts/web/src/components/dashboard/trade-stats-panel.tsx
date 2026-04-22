import { useMemo } from "react";
import {
  getGetRecentTradesQueryKey,
  useGetRecentTrades,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import { usePeriod } from "./period-context";

function fmtUsd(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: digits,
  }).format(n);
}

function pnlClass(n: number | null | undefined): string {
  if (n === null || n === undefined) return "text-foreground";
  if (n < 0) return "text-loss";
  if (n > 0) return "text-win";
  return "text-foreground";
}

/**
 * Companion to the daily P&L bar chart. Aggregates the most recent
 * 200 trades to surface volume, win/loss/scratch counts, expectancy,
 * and avg win / avg loss for the operator at a glance.
 *
 * Sourced from /api/dashboard/recent-trades; bounded by the same
 * window the period strip selects.
 */
export default function TradeStatsPanel() {
  const params = { limit: 200 };
  const { period, days } = usePeriod();
  const { matches } = useVerticalFilter();
  const { data, isLoading } = useGetRecentTrades(params, {
    query: {
      queryKey: getGetRecentTradesQueryKey(params),
      refetchInterval: 60_000,
    },
  });

  const stats = useMemo(() => {
    const all = data?.items ?? [];
    const cutoffMs = Date.now() - days * 86_400_000;
    const trades = all.filter((t) => {
      if (!matches(t.vertical)) return false;
      if (!t.executed_at) return true;
      const ms = new Date(t.executed_at).getTime();
      return Number.isNaN(ms) ? true : ms >= cutoffMs;
    });

    let wins = 0;
    let losses = 0;
    let scratches = 0;
    let totalWin = 0;
    let totalLoss = 0;
    let totalPnl = 0;
    let volume = 0;

    for (const t of trades) {
      const pnl = t.pnl ?? null;
      const qty = Math.abs(Number(t.quantity ?? 0));
      const px = Number(t.price ?? 0);
      volume += qty * px;
      if (pnl === null || pnl === undefined) continue;
      totalPnl += pnl;
      if (pnl > 0) {
        wins += 1;
        totalWin += pnl;
      } else if (pnl < 0) {
        losses += 1;
        totalLoss += pnl;
      } else {
        scratches += 1;
      }
    }
    const settled = wins + losses + scratches;
    const winRate = wins + losses > 0 ? wins / (wins + losses) : null;
    const avgWin = wins > 0 ? totalWin / wins : null;
    const avgLoss = losses > 0 ? totalLoss / losses : null;
    const expectancy = settled > 0 ? totalPnl / settled : null;
    return {
      count: trades.length,
      settled,
      wins,
      losses,
      scratches,
      winRate,
      avgWin,
      avgLoss,
      expectancy,
      totalPnl,
      volume,
    };
  }, [data, days, matches]);

  return (
    <GlassCard tone="solid" padding="md" data-testid="trade-stats-panel">
      <SectionHeader
        eyebrow={`STATS · ${period.toUpperCase()}`}
        title="Trade stats"
        subtitle="Aggregated from the most recent 200 fills, clipped to the period."
      />
      {isLoading ? (
        <div className="grid grid-cols-2 gap-3" data-testid="skeleton-trade-stats">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="h-12 animate-pulse bg-muted/10" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <Stat label="TRADES" value={String(stats.count)} testId="stat-trades" />
          <Stat
            label="VOLUME"
            value={fmtUsd(stats.volume)}
            hint="qty × price"
            valueClass="text-primary"
            testId="stat-volume"
          />
          <Stat
            label="EXPECTANCY"
            value={fmtUsd(stats.expectancy, 2)}
            hint={`${stats.settled} settled`}
            valueClass={pnlClass(stats.expectancy)}
            testId="stat-expectancy"
          />
          <Stat
            label="W / L / SCR"
            value={`${stats.wins}·${stats.losses}·${stats.scratches}`}
            hint={
              stats.winRate !== null
                ? `${(stats.winRate * 100).toFixed(0)}% win`
                : undefined
            }
            testId="stat-wls"
          />
          <Stat
            label="AVG WIN"
            value={fmtUsd(stats.avgWin, 2)}
            valueClass={pnlClass(stats.avgWin)}
            testId="stat-avg-win"
          />
          <Stat
            label="AVG LOSS"
            value={fmtUsd(stats.avgLoss, 2)}
            valueClass={pnlClass(stats.avgLoss)}
            testId="stat-avg-loss"
          />
        </div>
      )}
    </GlassCard>
  );
}

function Stat({
  label,
  value,
  hint,
  valueClass,
  testId,
}: {
  label: string;
  value: string;
  hint?: string;
  valueClass?: string;
  testId: string;
}) {
  return (
    <div className="border border-border bg-card/40 p-2">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div
        className={`text-base font-bold tabular-nums ${valueClass ?? "text-foreground"}`}
        data-testid={testId}
      >
        {value}
      </div>
      {hint ? (
        <div className="text-[10px] text-muted-foreground/70">{hint}</div>
      ) : null}
    </div>
  );
}
