import { useQuery } from "@tanstack/react-query";
import {
  getGetDashboardOverviewQueryKey,
  getGetRecentTradesQueryKey,
  getGetVerticalsOverviewQueryKey,
  useGetDashboardOverview,
  useGetRecentTrades,
  useGetVerticalsOverview,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { usePeriod } from "./period-context";

type EquityPoint = {
  date: string;
  daily_pnl: number;
  cumulative_pnl: number;
  trades: number;
};
type EquityCurve = { days: number; points: EquityPoint[] };

function fmtCurrency(n: number | null | undefined, fractionDigits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: 0,
  }).format(n);
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtNumber(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n);
}

function pnlClass(n: number | null | undefined): string {
  if (n === null || n === undefined) return "text-foreground";
  if (n < 0) return "text-loss";
  if (n > 0) return "text-win";
  return "text-foreground";
}

function KpiTile({
  label,
  value,
  hint,
  testId,
  valueClassName,
  loading,
}: {
  label: string;
  value: string;
  hint?: string;
  testId: string;
  valueClassName?: string;
  loading?: boolean;
}) {
  return (
    <GlassCard tone="solid" padding="sm" className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      {loading ? (
        <span
          className="block h-6 w-20 animate-pulse bg-muted/20"
          data-testid={`${testId}-skeleton`}
          aria-busy="true"
        />
      ) : (
        <span
          className={`text-lg font-bold tabular-nums ${valueClassName ?? "text-foreground"}`}
          data-testid={testId}
        >
          {value}
        </span>
      )}
      {hint ? (
        <span className="text-[10px] text-muted-foreground/70">{hint}</span>
      ) : null}
    </GlassCard>
  );
}

export default function KpiSummaryBar() {
  const { period, days } = usePeriod();
  const overviewQ = useGetDashboardOverview({
    query: {
      queryKey: getGetDashboardOverviewQueryKey(),
      refetchInterval: 60_000,
    },
  });
  const verticalsParams = { days };
  const verticalsQ = useGetVerticalsOverview(verticalsParams, {
    query: {
      queryKey: getGetVerticalsOverviewQueryKey(verticalsParams),
      refetchInterval: 60_000,
    },
  });
  const tradesParams = { limit: 200 };
  const tradesQ = useGetRecentTrades(tradesParams, {
    query: {
      queryKey: getGetRecentTradesQueryKey(tradesParams),
      refetchInterval: 60_000,
    },
  });
  const equityQ = useQuery<EquityCurve>({
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

  const overview = overviewQ.data;
  const verticals = verticalsQ.data?.verticals ?? [];
  const equityPoints = equityQ.data?.points ?? [];
  const trades = tradesQ.data?.items ?? [];

  const loading =
    overviewQ.isLoading ||
    verticalsQ.isLoading ||
    equityQ.isLoading ||
    tradesQ.isLoading;

  const todayPnl =
    equityPoints.length > 0
      ? equityPoints[equityPoints.length - 1].daily_pnl
      : null;

  // Trade-weighted Sharpe across verticals — most representative
  // operator-level number that the verticals endpoint actually returns.
  const sharpeNumer = verticals.reduce(
    (s, v) =>
      s + (v.sharpe !== null && v.sharpe !== undefined ? v.sharpe * v.trades : 0),
    0,
  );
  const sharpeDenom = verticals.reduce(
    (s, v) =>
      s + (v.sharpe !== null && v.sharpe !== undefined ? v.trades : 0),
    0,
  );
  const sharpe = sharpeDenom > 0 ? sharpeNumer / sharpeDenom : null;

  // Volume = sum(|qty| * price) across recent fills clipped to the
  // selected window. The dashboard endpoint doesn't expose a volume
  // figure directly so this is the closest faithful aggregate.
  const cutoffMs = Date.now() - days * 86_400_000;
  let volume = 0;
  for (const t of trades) {
    if (t.executed_at) {
      const ms = new Date(t.executed_at).getTime();
      if (!Number.isNaN(ms) && ms < cutoffMs) continue;
    }
    volume += Math.abs(Number(t.quantity ?? 0)) * Number(t.price ?? 0);
  }

  // ROI = period realized PnL / opening cumulative bankroll baseline.
  // We use the first non-zero |cumulative_pnl| point in the window as
  // a denominator proxy when no bankroll endpoint is available; this
  // mirrors how the marketing landing computes its "growth %".
  let baseline = 0;
  for (const p of equityPoints) {
    if (Math.abs(p.cumulative_pnl) > 0) {
      baseline = Math.abs(p.cumulative_pnl);
      break;
    }
  }
  const periodPnl = equityPoints.reduce((s, p) => s + (p.daily_pnl ?? 0), 0);
  const roi = baseline > 0 ? periodPnl / baseline : null;

  const winRate = overview?.win_rate ?? null;
  const openPositions = overview?.open_positions ?? null;

  const periodLabel = period.toUpperCase();

  return (
    <div
      className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6"
      data-testid="kpi-summary-bar"
    >
      <KpiTile
        loading={loading && todayPnl === null}
        label="P&L TODAY"
        value={fmtCurrency(todayPnl)}
        hint="since 00:00 UTC"
        valueClassName={pnlClass(todayPnl)}
        testId="kpi-pnl-today"
      />
      <KpiTile
        loading={overviewQ.isLoading}
        label="WIN RATE"
        value={fmtPct(winRate, 1)}
        hint="all-time settled"
        testId="kpi-win-rate"
      />
      <KpiTile
        loading={loading && roi === null}
        label={`ROI · ${periodLabel}`}
        value={roi !== null ? fmtPct(roi, 1) : "—"}
        hint="period P&L vs baseline"
        valueClassName={pnlClass(roi)}
        testId="kpi-roi"
      />
      <KpiTile
        loading={tradesQ.isLoading}
        label={`VOLUME · ${periodLabel}`}
        value={fmtCurrency(volume)}
        hint={`${fmtNumber(trades.length)} fills`}
        valueClassName="text-primary"
        testId="kpi-volume"
      />
      <KpiTile
        loading={overviewQ.isLoading}
        label="OPEN POS"
        value={openPositions !== null ? String(openPositions) : "—"}
        hint={`${verticals.length} verticals`}
        valueClassName={
          openPositions && openPositions > 0 ? "text-primary" : undefined
        }
        testId="kpi-open-positions"
      />
      <KpiTile
        loading={verticalsQ.isLoading}
        label="SHARPE (W)"
        value={sharpe !== null ? sharpe.toFixed(2) : "—"}
        hint="trade-weighted"
        valueClassName={pnlClass(sharpe)}
        testId="kpi-sharpe"
      />
    </div>
  );
}
