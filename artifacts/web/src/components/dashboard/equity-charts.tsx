import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { resolveApiUrl } from "@/lib/api";
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
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import { usePeriod } from "./period-context";
import { useEffectivePlacedBy } from "./source-filter-context";
import { TileStaleBadge, useTileStaleClass } from "./tile-stale-badge";

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

export function fmtShortDate(s: string): string {
  if (!s) return "—";
  // Parse the YYYY-MM-DD bucket key as a *local* calendar date.  The
  // backend (`get_public_equity_curve`) groups trades by
  // `date_trunc('day', executed_at)::date` in UTC and emits each
  // bucket as a bare `YYYY-MM-DD` string with no timezone.  Parsing
  // it via `new Date("YYYY-MM-DDT00:00:00Z")` then formatting with
  // `toLocaleDateString` made any user west of UTC see the *previous*
  // day (e.g. a 2026-02-20 bucket rendered as "Feb 19" in Pacific
  // time), which made it look like a real-day trade had been
  // back-dated.  Splitting the components and constructing a local
  // Date keeps the rendered label aligned with the backend bucket.
  const parts = s.split("-");
  if (parts.length !== 3) return s;
  const [y, m, d] = parts.map((p) => Number.parseInt(p, 10));
  if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) {
    return s;
  }
  const dt = new Date(y, m - 1, d);
  if (Number.isNaN(dt.getTime())) return s;
  return dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const CYAN = "hsl(var(--primary))";
const WIN = "hsl(var(--win, 142 71% 45%))";
const LOSS = "hsl(var(--loss, 0 72% 55%))";

export default function EquityCharts() {
  const { period, days } = usePeriod();
  // Thread the operator's Manual / System / Both selection through
  // the equity-curve fetch so the Cumulative + Daily P&L charts move
  // in lock-step with the rest of the dashboard.  `placedBy` is part
  // of the React-Query key so flipping the toggle invalidates cleanly
  // instead of returning a stale cached series.  `all` produces no
  // server-side filter, identical to the marketing-landing behaviour.
  // Source-aware placed_by (task #846): falls through to Manual /
  // System / Both unless the input-mode toggle is forcing one source.
  const placedBy = useEffectivePlacedBy();
  const staleClass = useTileStaleClass();
  // Mirror the operator's vertical-strip selection so the equity chart
  // narrows whenever they click SPORTS / CRYPTO / etc.  An empty
  // selection means "show all", same as omitting the param entirely —
  // identical to the marketing landing-page behaviour.  We use
  // `effectiveVerticals` (not the bare `selected`) so admin-managed
  // hidden verticals are also suppressed server-side for non-admins
  // when no explicit chip selection is active — otherwise the headline
  // curve would silently leak hidden-vertical P&L into a non-admin's
  // view while every panel below the fold correctly excluded it.
  const { effectiveVerticals } = useVerticalFilter();
  const verticalsCsv = effectiveVerticals.slice().sort().join(",");
  const { data, isLoading, isError } = useQuery<EquityCurve>({
    queryKey: ["public-equity-curve", days, placedBy, verticalsCsv],
    queryFn: async () => {
      // Goes through `resolveApiUrl` so the Mac-Studio cutover knob
      // (`VITE_API_URL`) reroutes the public equity curve to the
      // configured backend just like every other API call.
      const params = new URLSearchParams({
        days: String(days),
        placed_by: placedBy,
      });
      if (verticalsCsv) params.set("vertical", verticalsCsv);
      const r = await fetch(
        resolveApiUrl(
          `${import.meta.env.BASE_URL}api/public/equity-curve?${params.toString()}`,
        ),
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
      className={"space-y-2" + (staleClass ? ` ${staleClass}` : "")}
      data-testid="equity-charts"
    >
    <TileStaleBadge testId="equity-charts-stale-badge" />
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
      <GlassCard tone="solid" padding="md" className="xl:col-span-2">
        <SectionHeader
          eyebrow={`PNL · ${periodLabel}`}
          title="Cumulative P&L"
          subtitle="Daily realized PnL, summed forward across the selected window."
        />
        <div className="h-64" data-testid="chart-cumulative-pnl">
          {isLoading ? (
            <div
              className="h-full animate-pulse bg-muted/10"
              data-testid="skeleton-cumulative-pnl"
              aria-busy="true"
            />
          ) : isError || points.length === 0 ? (
            <div className="flex h-full items-center justify-center text-[11px] uppercase tracking-widest text-muted-foreground">
              {isError ? "CURVE_UNAVAILABLE" : "NO_SETTLED_DATA"}
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
          {isLoading ? (
            <div
              className="h-full animate-pulse bg-muted/10"
              data-testid="skeleton-daily-pnl"
              aria-busy="true"
            />
          ) : isError || points.length === 0 ? (
            <div className="flex h-full items-center justify-center text-[11px] uppercase tracking-widest text-muted-foreground">
              {isError ? "CURVE_UNAVAILABLE" : "NO_SETTLED_DATA"}
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
    </div>
  );
}
