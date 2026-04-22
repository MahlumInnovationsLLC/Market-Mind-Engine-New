import { useMemo } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip } from "recharts";
import {
  getGetRecentTradesQueryKey,
  useGetRecentTrades,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function fmtMoney(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const f = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(Math.abs(n));
  return n < 0 ? `-${f}` : f;
}

/**
 * Compact realized-P&L sparkline driven by the recent-trades feed.
 *
 * Renders a 50-trade rolling cumulative P&L curve — same data as
 * TradeStatsPanel but visualized so operators on the Live tab can see
 * trend at a glance without scanning a numeric grid.
 */
export default function PnlSparkline() {
  const params = { limit: 50 };
  const { data, isLoading } = useGetRecentTrades(params, {
    query: {
      queryKey: getGetRecentTradesQueryKey(params),
      refetchInterval: 30_000,
    },
  });

  const series = useMemo(() => {
    const trades = data?.items ?? [];
    // Oldest -> newest so the line reads left to right.
    const ordered = [...trades]
      .filter((t) => !!t.executed_at)
      .sort(
        (a, b) =>
          new Date(a.executed_at!).getTime() -
          new Date(b.executed_at!).getTime(),
      );
    let cum = 0;
    return ordered.map((t, i) => {
      const pnl = typeof t.pnl === "number" ? t.pnl : 0;
      cum += pnl;
      return { i, t: t.executed_at, pnl, cum };
    });
  }, [data]);

  const last = series.length > 0 ? series[series.length - 1].cum : null;
  const isWin = last !== null && last > 0;
  const isLoss = last !== null && last < 0;
  const tone = isWin
    ? "text-success"
    : isLoss
      ? "text-destructive"
      : "text-foreground";
  const stroke = isWin
    ? "hsl(var(--success))"
    : isLoss
      ? "hsl(var(--destructive))"
      : "hsl(var(--primary))";

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-pnl-sparkline">
      <SectionHeader
        eyebrow="REALIZED"
        title="P&L · last 50 fills"
        subtitle="Rolling cumulative realized P&L."
        actions={
          <span
            className={`text-sm font-bold tabular-nums ${tone}`}
            data-testid="text-sparkline-total"
          >
            {fmtMoney(last)}
          </span>
        }
      />
      {isLoading ? (
        <div
          className="h-20 animate-pulse bg-muted/10"
          data-testid="skeleton-sparkline"
        />
      ) : series.length === 0 ? (
        <div className="border border-border bg-card/40 p-4 text-center text-[11px] uppercase tracking-widest text-muted-foreground">
          NO_FILLS_YET
        </div>
      ) : (
        <div className="h-20 w-full" data-testid="chart-sparkline">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={series} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="pnlSparkFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={stroke} stopOpacity={0.4} />
                  <stop offset="100%" stopColor={stroke} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Tooltip
                cursor={{ stroke: "hsl(var(--border))" }}
                contentStyle={{
                  background: "hsl(var(--popover))",
                  border: "1px solid hsl(var(--border))",
                  fontSize: 11,
                }}
                labelFormatter={() => ""}
                formatter={(v: number) => [fmtMoney(v), "Cumulative"]}
              />
              <Area
                type="monotone"
                dataKey="cum"
                stroke={stroke}
                strokeWidth={2}
                fill="url(#pnlSparkFill)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </GlassCard>
  );
}
