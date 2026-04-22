import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { usePeriod } from "./period-context";

type EquityPoint = {
  date: string;
  daily_pnl: number;
  cumulative_pnl: number;
  trades: number;
};
type EquityCurve = { days: number; points: EquityPoint[] };

function fmtCurrency(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: digits,
  }).format(n);
}

function fmtShortDate(s: string): string {
  if (!s) return "—";
  const d = new Date(`${s}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return s;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const CYAN = "hsl(var(--primary))";
const WIN = "hsl(var(--win, 142 71% 45%))";
const LOSS = "hsl(var(--loss, 0 72% 55%))";

export default function EquityCharts() {
  const { period, days } = usePeriod();
  const { data, isLoading, isError } = useQuery<EquityCurve>({
    queryKey: ["public-equity-curve", days],
    queryFn: async () => {
      const r = await fetch(
        `${import.meta.env.BASE_URL}api/public/equity-curve?days=${days}`,
      );
      if (!r.ok) throw new Error(`equity-curve ${r.status}`);
      return r.json();
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const points = useMemo(() => data?.points ?? [], [data]);
  const periodLabel = period.toUpperCase();

  return (
    <div
      className="grid grid-cols-1 gap-4 xl:grid-cols-3"
      data-testid="equity-charts"
    >
      <GlassCard tone="solid" padding="md" className="xl:col-span-2">
        <SectionHeader
          eyebrow={`PNL · ${periodLabel}`}
          title="Cumulative P&L"
          subtitle="Daily realized PnL, summed forward across the selected window."
        />
        <div className="h-64" data-testid="chart-cumulative-pnl">
          {isLoading || isError || points.length === 0 ? (
            <div className="flex h-full items-center justify-center text-[11px] uppercase tracking-widest text-muted-foreground">
              {isLoading
                ? "LOADING…"
                : isError
                  ? "CURVE_UNAVAILABLE"
                  : "NO_SETTLED_DATA"}
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={points}
                margin={{ top: 8, right: 12, bottom: 8, left: -16 }}
              >
                <defs>
                  <linearGradient id="cum-pnl" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={CYAN} stopOpacity={0.45} />
                    <stop offset="100%" stopColor={CYAN} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="2 4" />
                <XAxis
                  dataKey="date"
                  tickFormatter={fmtShortDate}
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  stroke="hsl(var(--border))"
                  minTickGap={32}
                />
                <YAxis
                  tickFormatter={(v: number) => fmtCurrency(v)}
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  stroke="hsl(var(--border))"
                  width={64}
                />
                <Tooltip
                  contentStyle={{
                    background: "hsl(var(--card))",
                    border: "1px solid hsl(var(--border))",
                    fontSize: 11,
                  }}
                  formatter={(v: number) => fmtCurrency(v)}
                  labelFormatter={(v: string) => fmtShortDate(v)}
                />
                <ReferenceLine
                  y={0}
                  stroke="hsl(var(--muted-foreground))"
                  strokeDasharray="3 3"
                />
                <Area
                  type="monotone"
                  dataKey="cumulative_pnl"
                  name="Cumulative"
                  stroke={CYAN}
                  strokeWidth={2}
                  fill="url(#cum-pnl)"
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </GlassCard>

      <GlassCard tone="solid" padding="md">
        <SectionHeader
          eyebrow={`PNL · ${periodLabel}`}
          title="Daily P&L"
          subtitle="Per-day realized PnL across the window."
        />
        <div className="h-64" data-testid="chart-daily-pnl">
          {isLoading || isError || points.length === 0 ? (
            <div className="flex h-full items-center justify-center text-[11px] uppercase tracking-widest text-muted-foreground">
              {isLoading
                ? "LOADING…"
                : isError
                  ? "CURVE_UNAVAILABLE"
                  : "NO_SETTLED_DATA"}
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={points}
                margin={{ top: 8, right: 12, bottom: 8, left: -16 }}
              >
                <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="2 4" />
                <XAxis
                  dataKey="date"
                  tickFormatter={fmtShortDate}
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  stroke="hsl(var(--border))"
                  minTickGap={24}
                />
                <YAxis
                  tickFormatter={(v: number) => fmtCurrency(v)}
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  stroke="hsl(var(--border))"
                  width={64}
                />
                <Tooltip
                  contentStyle={{
                    background: "hsl(var(--card))",
                    border: "1px solid hsl(var(--border))",
                    fontSize: 11,
                  }}
                  formatter={(v: number) => fmtCurrency(v)}
                  labelFormatter={(v: string) => fmtShortDate(v)}
                />
                <ReferenceLine
                  y={0}
                  stroke="hsl(var(--muted-foreground))"
                  strokeDasharray="3 3"
                />
                <Bar dataKey="daily_pnl" isAnimationActive={false}>
                  {points.map((p, i) => (
                    <Cell
                      key={i}
                      fill={
                        (p.daily_pnl ?? 0) < 0
                          ? LOSS
                          : (p.daily_pnl ?? 0) > 0
                            ? WIN
                            : "hsl(var(--muted-foreground))"
                      }
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      </GlassCard>
    </div>
  );
}
