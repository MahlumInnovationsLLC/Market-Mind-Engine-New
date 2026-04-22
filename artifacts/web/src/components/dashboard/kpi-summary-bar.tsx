import { useQuery } from "@tanstack/react-query";
import {
  getGetDashboardOverviewQueryKey,
  getGetVerticalsOverviewQueryKey,
  useGetDashboardOverview,
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
}: {
  label: string;
  value: string;
  hint?: string;
  testId: string;
  valueClassName?: string;
}) {
  return (
    <GlassCard tone="solid" padding="sm" className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      <span
        className={`text-lg font-bold tabular-nums ${valueClassName ?? "text-foreground"}`}
        data-testid={testId}
      >
        {value}
      </span>
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

  const todayPnl =
    equityPoints.length > 0
      ? equityPoints[equityPoints.length - 1].daily_pnl
      : null;
  const periodPnl = equityPoints.reduce((s, p) => s + (p.daily_pnl ?? 0), 0);
  const periodTrades = equityPoints.reduce((s, p) => s + (p.trades ?? 0), 0);

  // Trade-weighted Sharpe across verticals — most representative single
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

  const winRate = overview?.win_rate ?? null;
  const totalPnl = overview?.realized_pnl ?? null;
  const openPositions = overview?.open_positions ?? null;

  const periodLabel = period.toUpperCase();

  return (
    <div
      className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6"
      data-testid="kpi-summary-bar"
    >
      <KpiTile
        label="P&L TODAY"
        value={fmtCurrency(todayPnl)}
        hint={equityQ.isLoading ? "loading…" : "since 00:00 UTC"}
        valueClassName={pnlClass(todayPnl)}
        testId="kpi-pnl-today"
      />
      <KpiTile
        label="WIN RATE"
        value={fmtPct(winRate, 1)}
        hint="all-time settled"
        testId="kpi-win-rate"
      />
      <KpiTile
        label={`P&L · ${periodLabel}`}
        value={fmtCurrency(periodPnl)}
        hint={`${fmtNumber(periodTrades)} trades`}
        valueClassName={pnlClass(periodPnl)}
        testId="kpi-period-pnl"
      />
      <KpiTile
        label="TOTAL P&L"
        value={fmtCurrency(totalPnl)}
        hint="cumulative realized"
        valueClassName={pnlClass(totalPnl)}
        testId="kpi-total-pnl"
      />
      <KpiTile
        label="OPEN POS"
        value={openPositions !== null ? String(openPositions) : "—"}
        hint={`${verticals.length} verticals`}
        valueClassName={openPositions && openPositions > 0 ? "text-primary" : undefined}
        testId="kpi-open-positions"
      />
      <KpiTile
        label="SHARPE (W)"
        value={sharpe !== null ? sharpe.toFixed(2) : "—"}
        hint="trade-weighted"
        valueClassName={pnlClass(sharpe)}
        testId="kpi-sharpe"
      />
    </div>
  );
}
