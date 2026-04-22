import {
  getGetDashboardOverviewQueryKey,
  useGetDashboardOverview,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";

/**
 * 6-tile KPI strip. All values are sourced **directly** from
 * `/api/dashboard/overview` — no derived formulas. The dashboard's
 * period strip below this bar drives the equity charts and trade
 * stats; overview KPIs are intentionally period-invariant lifetime
 * counters per the API contract.
 */

function fmtCurrency(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtCount(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US").format(n);
}

function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = new Date(iso).getTime();
  if (Number.isNaN(ms)) return "—";
  const diff = Math.max(0, Date.now() - ms);
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
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
  const { data, isLoading } = useGetDashboardOverview({
    query: {
      queryKey: getGetDashboardOverviewQueryKey(),
      refetchInterval: 60_000,
    },
  });

  const realizedPnl = data?.realized_pnl ?? null;
  const winRate = data?.win_rate ?? null;
  const openPositions = data?.open_positions ?? null;
  const counts = data?.counts;
  const tradesCount = counts?.trades ?? null;
  const cyclesCount = counts?.trading_cycles ?? null;
  const debatesCount = counts?.swarm_debates ?? null;
  const latestCycleAt = data?.latest_cycle_at ?? null;
  const latestTradeAt = data?.latest_trade_at ?? null;

  return (
    <div
      className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6"
      data-testid="kpi-summary-bar"
    >
      <KpiTile
        loading={isLoading}
        label="REALIZED P&L"
        value={fmtCurrency(realizedPnl)}
        hint={
          latestTradeAt ? `last fill ${fmtRelative(latestTradeAt)}` : undefined
        }
        valueClassName={pnlClass(realizedPnl)}
        testId="kpi-realized-pnl"
      />
      <KpiTile
        loading={isLoading}
        label="WIN RATE"
        value={fmtPct(winRate, 1)}
        hint="all-time settled"
        testId="kpi-win-rate"
      />
      <KpiTile
        loading={isLoading}
        label="OPEN POSITIONS"
        value={openPositions !== null ? String(openPositions) : "—"}
        hint={
          counts?.positions !== null && counts?.positions !== undefined
            ? `${fmtCount(counts.positions)} lifetime`
            : undefined
        }
        valueClassName={
          openPositions && openPositions > 0 ? "text-primary" : undefined
        }
        testId="kpi-open-positions"
      />
      <KpiTile
        loading={isLoading}
        label="TRADES"
        value={fmtCount(tradesCount)}
        hint="lifetime fills"
        valueClassName="text-primary"
        testId="kpi-trades"
      />
      <KpiTile
        loading={isLoading}
        label="CYCLES"
        value={fmtCount(cyclesCount)}
        hint={
          debatesCount !== null && debatesCount !== undefined
            ? `${fmtCount(debatesCount)} debates`
            : undefined
        }
        testId="kpi-cycles"
      />
      <KpiTile
        loading={isLoading}
        label="LAST CYCLE"
        value={fmtRelative(latestCycleAt)}
        hint={latestCycleAt ? "swarm activity" : "awaiting first run"}
        testId="kpi-last-cycle"
      />
    </div>
  );
}
