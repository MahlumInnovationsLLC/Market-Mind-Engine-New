import { useQuery } from "@tanstack/react-query";
import {
  getGetDashboardOverviewQueryKey,
  getGetRecentTradesQueryKey,
  getGetVerticalsOverviewQueryKey,
  useGetDashboardOverview,
  useGetRecentTrades,
  useGetVerticalsOverview,
} from "@workspace/api-client-react";
import { resolveApiUrl } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import { usePeriod } from "./period-context";
import { useEffectivePlacedBy } from "./source-filter-context";
import { TileStaleBadge, useTileStaleClass } from "./tile-stale-badge";

/**
 * 6-tile KPI strip from §6.1 of FRONTEND_UI_REPORT:
 *   P&L Today · Win Rate · ROI · Total Volume · Open Positions · Sharpe
 *
 * `/api/dashboard/overview` only exposes win_rate, open_positions,
 * realized_pnl, latest_*. The other tiles are derived from the
 * dashboard's existing public endpoints (no new backend contract):
 *   - P&L Today  ← /api/public/equity-curve last point.daily_pnl
 *   - ROI        ← (period P&L) / first non-zero |cumulative_pnl| baseline
 *                  in the same equity curve window
 *   - Volume     ← Σ |qty|·price across /api/dashboard/recent-trades
 *                  clipped to the selected period
 *   - Sharpe     ← trade-weighted Sharpe across /api/dashboard/verticals
 */

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
  // Trade-provenance filter (T004).  Plumbed through every trade-
  // derived tile (P&L today, ROI, volume, win-rate, Sharpe) AND the
  // equity-curve query the P&L-today / ROI tiles derive from — the
  // equity-curve API now accepts an optional `placed_by` param that
  // defaults to "all" on the marketing landing surface, so threading
  // it from the authed dashboard doesn't break the anonymous one.
  // Open-positions stays unscoped — it describes the live book
  // (currently-held contracts) which is provenance-agnostic.
  // Threads the source-aware effective `placed_by` so the strip
  // narrows automatically to Edge Terminal or Pipeline trades when the
  // input-mode toggle is flipped (task #846).  Falls back to the
  // operator's Manual / System / Both selection when both sources are
  // visible.
  const placedBy = useEffectivePlacedBy();
  const staleClass = useTileStaleClass();
  // Mirror the operator's vertical-strip selection so every equity-
  // derived tile (P&L Today, ROI, Volume, Sharpe) narrows in lock-step
  // with the equity charts and the per-vertical panels lower on the
  // page.  We use `effectiveVerticals` (not the bare `selected`) so
  // admin-managed hidden verticals are also suppressed server-side for
  // non-admins when no explicit chip selection is active — matching
  // how `matches()` already filters the per-vertical breakdown.
  // Win-rate and Open-positions tiles intentionally stay unscoped:
  // their endpoints return a global all-time / live-book scalar without
  // a per-vertical breakdown, so narrowing them here would require a
  // separate API change and is out of scope for this wiring.
  const {
    effectiveVerticals,
    matches: matchesVertical,
    filterActive: verticalFilterActive,
  } = useVerticalFilter();
  const verticalsCsv = effectiveVerticals.slice().sort().join(",");

  const overviewParams = { placed_by: placedBy };
  const overviewQ = useGetDashboardOverview(overviewParams, {
    query: {
      queryKey: getGetDashboardOverviewQueryKey(overviewParams),
      refetchInterval: 60_000,
    },
  });
  const verticalsParams = { days, placed_by: placedBy };
  const verticalsQ = useGetVerticalsOverview(verticalsParams, {
    query: {
      queryKey: getGetVerticalsOverviewQueryKey(verticalsParams),
      refetchInterval: 60_000,
    },
  });
  const tradesParams = { limit: 200, placed_by: placedBy };
  const tradesQ = useGetRecentTrades(tradesParams, {
    query: {
      queryKey: getGetRecentTradesQueryKey(tradesParams),
      refetchInterval: 60_000,
    },
  });
  const equityQ = useQuery<EquityCurve>({
    // `placedBy` AND the verticals CSV are part of the key so the
    // cache splits per-segment and per-vertical-selection, forcing a
    // refetch when the operator toggles the Manual/System/Both
    // toggle or clicks a vertical chip.
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

  const overview = overviewQ.data;
  const verticals = verticalsQ.data?.verticals ?? [];
  const equityPoints = equityQ.data?.points ?? [];
  const trades = tradesQ.data?.items ?? [];

  const periodLabel = period.toUpperCase();

  // P&L Today — last bucket of the equity curve.
  const todayPnl =
    equityPoints.length > 0
      ? equityPoints[equityPoints.length - 1].daily_pnl
      : null;

  // Win rate — overview field, period-invariant by API contract.
  const winRate = overview?.win_rate ?? null;

  // Open positions — overview field, period-invariant by API contract.
  const openPositions = overview?.open_positions ?? null;

  // ROI — period P&L / first non-zero |cumulative_pnl| baseline in
  // the window. Falls back to "—" when no settled equity is available.
  let baseline = 0;
  for (const p of equityPoints) {
    if (Math.abs(p.cumulative_pnl) > 0) {
      baseline = Math.abs(p.cumulative_pnl);
      break;
    }
  }
  const periodPnl = equityPoints.reduce((s, p) => s + (p.daily_pnl ?? 0), 0);
  const roi = baseline > 0 ? periodPnl / baseline : null;

  // Total Volume — Σ |qty|·price across recent fills clipped to the
  // selected window.  When a vertical filter is active we additionally
  // drop any fill whose `vertical` is not in the operator's selection,
  // so the tile narrows in lock-step with the equity chart.  The
  // recent-trades endpoint already returns `vertical` per row, so this
  // is a pure client-side filter — no extra API surface needed.
  const cutoffMs = Date.now() - days * 86_400_000;
  let volume = 0;
  let volumeFills = 0;
  for (const t of trades) {
    if (t.executed_at) {
      const ms = new Date(t.executed_at).getTime();
      if (!Number.isNaN(ms) && ms < cutoffMs) continue;
    }
    if (verticalFilterActive && !matchesVertical(t.vertical ?? null)) continue;
    volume += Math.abs(Number(t.quantity ?? 0)) * Number(t.price ?? 0);
    volumeFills += 1;
  }

  // Sharpe — trade-weighted across verticals, matching the period.
  // The same vertical filter applies: when the operator narrows to one
  // or more verticals we weight only those rows so the trade-weighted
  // Sharpe reflects the same slice as the rest of the headline strip.
  const sharpeRows = verticalFilterActive
    ? verticals.filter((v) => matchesVertical(v.vertical))
    : verticals;
  const sharpeNumer = sharpeRows.reduce(
    (s, v) =>
      s +
      (v.sharpe !== null && v.sharpe !== undefined ? v.sharpe * v.trades : 0),
    0,
  );
  const sharpeDenom = sharpeRows.reduce(
    (s, v) =>
      s + (v.sharpe !== null && v.sharpe !== undefined ? v.trades : 0),
    0,
  );
  const sharpe = sharpeDenom > 0 ? sharpeNumer / sharpeDenom : null;

  return (
    <div
      className={
        "space-y-2" + (staleClass ? ` ${staleClass}` : "")
      }
      data-testid="kpi-summary-bar"
    >
    <TileStaleBadge testId="kpi-summary-stale-badge" />
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <KpiTile
        loading={equityQ.isLoading}
        label="P&L TODAY"
        value={fmtCurrency(todayPnl)}
        hint="last equity bucket"
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
        loading={equityQ.isLoading}
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
        hint={`${fmtNumber(volumeFills)} fills`}
        valueClassName="text-primary"
        testId="kpi-volume"
      />
      <KpiTile
        loading={overviewQ.isLoading}
        label="OPEN POSITIONS"
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
    </div>
  );
}
