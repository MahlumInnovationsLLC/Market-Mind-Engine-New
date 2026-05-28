import { useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { useWindowVirtualizer } from "@tanstack/react-virtual";
import {
  getGetDashboardEdgeTerminalPnlQueryKey,
  getGetRecentTradesQueryKey,
  useGetDashboardEdgeTerminalPnl,
  useGetRecentTrades,
} from "@workspace/api-client-react";
import { resolveApiUrl } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

type EquityPoint = {
  date: string;
  daily_pnl: number;
  cumulative_pnl: number;
  trades: number;
};
type EquityCurve = { days: number; points: EquityPoint[] };

function fmtUSD(n: number | null | undefined, signed = true): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  const sign = signed ? (v >= 0 ? "+" : "-") : v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toFixed(2)}`;
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return `${(Number(n) * 100).toFixed(digits)}%`;
}

function fmtInt(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return new Intl.NumberFormat("en-US").format(Number(n));
}

function pnlColor(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(Number(n)))
    return "text-foreground";
  const v = Number(n);
  if (v > 0) return "text-emerald-400";
  if (v < 0) return "text-rose-400";
  return "text-foreground";
}

function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  } catch {
    return s;
  }
}

function fmtDateTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

export default function TrendsPage() {
  // ── Edge Terminal P&L summary (today / 7d / by vertical) ─────────
  const pnlKey = getGetDashboardEdgeTerminalPnlQueryKey();
  const pnlQ = useGetDashboardEdgeTerminalPnl({
    query: {
      queryKey: pnlKey,
      refetchInterval: 30_000,
      staleTime: 25_000,
    },
  });

  // ── 7-day daily P&L curve, scoped to edge_terminal trades ────────
  // Reuses /api/public/equity-curve which already honours
  // `placed_by=edge_terminal` (same path KpiSummaryBar uses), so we
  // get a per-day P&L breakdown without a new backend contract.
  const equityQ = useQuery<EquityCurve>({
    queryKey: ["trends-edge-equity", 7, "edge_terminal"],
    queryFn: async () => {
      const params = new URLSearchParams({
        days: "7",
        placed_by: "edge_terminal",
      });
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

  // ── Recent Edge Terminal trades (last 100, clipped to 7d) ────────
  const tradesParams = { limit: 100, placed_by: "edge_terminal" as const };
  const tradesQ = useGetRecentTrades(tradesParams, {
    query: {
      queryKey: getGetRecentTradesQueryKey(tradesParams),
      refetchInterval: 60_000,
    },
  });

  const summary = pnlQ.data;
  const equityPoints = equityQ.data?.points ?? [];
  const cutoff7dMs = Date.now() - 7 * 86_400_000;
  const trades = useMemo(() => {
    const items = tradesQ.data?.items ?? [];
    return items.filter((t) => {
      if (!t.executed_at) return true;
      const ms = new Date(t.executed_at).getTime();
      return Number.isNaN(ms) ? true : ms >= cutoff7dMs;
    });
  }, [tradesQ.data, cutoff7dMs]);

  // Virtualize the trades table against the window scroll so only the rows
  // near the viewport are mounted (the list can carry up to ~100 fills).
  const tradesTableRef = useRef<HTMLDivElement>(null);
  const tradesVirtualizer = useWindowVirtualizer({
    count: trades.length,
    estimateSize: () => 34,
    overscan: 10,
    scrollMargin: tradesTableRef.current?.offsetTop ?? 0,
  });
  const tradeVirtualRows = tradesVirtualizer.getVirtualItems();
  const tradesPaddingTop =
    tradeVirtualRows.length > 0
      ? Math.max(
          0,
          tradeVirtualRows[0].start - tradesVirtualizer.options.scrollMargin,
        )
      : 0;
  const tradesPaddingBottom =
    tradeVirtualRows.length > 0
      ? Math.max(
          0,
          tradesVirtualizer.getTotalSize() -
            (tradeVirtualRows[tradeVirtualRows.length - 1].end -
              tradesVirtualizer.options.scrollMargin),
        )
      : 0;

  // ── Aggregations from the 7-day trade list ───────────────────────
  const aggregates = useMemo(() => {
    let bestDay: { date: string; pnl: number } | null = null;
    let worstDay: { date: string; pnl: number } | null = null;
    let n_settled_7d = 0;
    let n_wins_7d = 0;
    for (const p of equityPoints) {
      if (
        bestDay === null ||
        (p.daily_pnl ?? 0) > (bestDay.pnl ?? -Infinity)
      ) {
        bestDay = { date: p.date, pnl: p.daily_pnl ?? 0 };
      }
      if (
        worstDay === null ||
        (p.daily_pnl ?? 0) < (worstDay.pnl ?? Infinity)
      ) {
        worstDay = { date: p.date, pnl: p.daily_pnl ?? 0 };
      }
    }
    const bySide: Record<string, { n: number; pnl: number; settled: number }> =
      {};
    const byVertical: Record<
      string,
      { n: number; pnl: number; settled: number; wins: number; vol: number }
    > = {};
    let totalVolume = 0;
    for (const t of trades) {
      const px = Number(t.price ?? 0);
      const qty = Math.abs(Number(t.quantity ?? 0));
      const vol = qty * px;
      totalVolume += vol;
      const pnl = t.pnl === null || t.pnl === undefined ? null : Number(t.pnl);
      const settled = pnl !== null;
      if (settled) {
        n_settled_7d += 1;
        if (pnl! > 0) n_wins_7d += 1;
      }
      const sideKey = (t.side ?? "?").toString().toUpperCase();
      const sb = (bySide[sideKey] ||= { n: 0, pnl: 0, settled: 0 });
      sb.n += 1;
      sb.pnl += pnl ?? 0;
      sb.settled += settled ? 1 : 0;
      const vKey = (t.vertical ?? "—").toString();
      const vb = (byVertical[vKey] ||= {
        n: 0,
        pnl: 0,
        settled: 0,
        wins: 0,
        vol: 0,
      });
      vb.n += 1;
      vb.pnl += pnl ?? 0;
      vb.vol += vol;
      if (settled) {
        vb.settled += 1;
        if (pnl! > 0) vb.wins += 1;
      }
    }
    const winRate7d = n_settled_7d > 0 ? n_wins_7d / n_settled_7d : null;
    return {
      bestDay,
      worstDay,
      totalVolume,
      bySide,
      byVertical,
      winRate7d,
      n_settled_7d,
      n_wins_7d,
    };
  }, [trades, equityPoints]);

  // ── Sparkline scale ─────────────────────────────────────────────
  const sparkBounds = useMemo(() => {
    if (equityPoints.length === 0) return { min: 0, max: 0 };
    const vals = equityPoints.map((p) => p.cumulative_pnl ?? 0);
    return { min: Math.min(...vals, 0), max: Math.max(...vals, 0) };
  }, [equityPoints]);

  return (
    <div
      className="space-y-4 px-4 py-4 lg:px-6"
      data-testid="page-trends"
    >
      <SectionHeader
        eyebrow="EDGE TERMINAL"
        title="Trends — last 7 days"
        subtitle="Every trade you've placed through the Edge Terminal scraper, broken out by day, vertical, and side. Refreshes every 30–60s."
      />

      {/* ── Headline 7-day metrics ─────────────────────────────── */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <HeadlineTile
          label="P&L · 7D"
          value={fmtUSD(summary?.realised_pnl_7d)}
          tone={pnlColor(summary?.realised_pnl_7d)}
          hint={`${fmtInt(summary?.trades_7d)} trades placed`}
          loading={pnlQ.isLoading}
          testId="trends-pnl-7d"
        />
        <HeadlineTile
          label="WIN RATE · 7D"
          value={fmtPct(aggregates.winRate7d)}
          hint={`${aggregates.n_wins_7d}/${aggregates.n_settled_7d} settled`}
          loading={tradesQ.isLoading}
          testId="trends-winrate-7d"
        />
        <HeadlineTile
          label="VOLUME · 7D"
          value={fmtUSD(aggregates.totalVolume, false)}
          hint={`${trades.length} fills`}
          tone="text-primary"
          loading={tradesQ.isLoading}
          testId="trends-volume-7d"
        />
        <HeadlineTile
          label="OPEN EXPOSURE"
          value={fmtUSD(summary?.open_notional, false)}
          hint={`${fmtInt(summary?.open_count)} positions live`}
          loading={pnlQ.isLoading}
          testId="trends-open-exposure"
        />
      </div>

      {/* ── Daily P&L sparkline ────────────────────────────────── */}
      <GlassCard tone="solid" padding="md">
        <SectionHeader
          eyebrow="DAILY"
          title="P&L by day"
          subtitle="Cumulative + per-day P&L for Edge Terminal trades over the last 7 days."
        />
        {equityQ.isLoading && equityPoints.length === 0 ? (
          <div className="text-xs text-muted-foreground">Loading…</div>
        ) : equityPoints.length === 0 ? (
          <div className="text-xs text-muted-foreground">
            No Edge Terminal trades have settled in the last 7 days yet.
          </div>
        ) : (
          <div className="space-y-3">
            <Sparkline
              points={equityPoints}
              min={sparkBounds.min}
              max={sparkBounds.max}
            />
            <div className="overflow-x-auto">
              <table className="w-full min-w-[480px] text-left text-xs">
                <thead>
                  <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
                    <th className="py-1.5 pr-3">Day</th>
                    <th className="py-1.5 pr-3 text-right">Trades</th>
                    <th className="py-1.5 pr-3 text-right">Daily P&L</th>
                    <th className="py-1.5 pr-3 text-right">Cumulative</th>
                  </tr>
                </thead>
                <tbody>
                  {equityPoints.map((p) => (
                    <tr
                      key={p.date}
                      data-testid={`trends-daily-row-${p.date}`}
                      className="border-b border-border/30 last:border-0"
                    >
                      <td className="py-1.5 pr-3 text-foreground">
                        {fmtDate(p.date)}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                        {fmtInt(p.trades)}
                      </td>
                      <td
                        className={`py-1.5 pr-3 text-right tabular-nums ${pnlColor(p.daily_pnl)}`}
                      >
                        {fmtUSD(p.daily_pnl)}
                      </td>
                      <td
                        className={`py-1.5 pr-3 text-right tabular-nums ${pnlColor(p.cumulative_pnl)}`}
                      >
                        {fmtUSD(p.cumulative_pnl)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap gap-4 border-t border-border/40 pt-2 text-[11px] text-muted-foreground">
              <span>
                Best day:{" "}
                <span
                  className={pnlColor(aggregates.bestDay?.pnl)}
                  data-testid="trends-best-day"
                >
                  {aggregates.bestDay
                    ? `${fmtDate(aggregates.bestDay.date)} · ${fmtUSD(aggregates.bestDay.pnl)}`
                    : "—"}
                </span>
              </span>
              <span>
                Worst day:{" "}
                <span
                  className={pnlColor(aggregates.worstDay?.pnl)}
                  data-testid="trends-worst-day"
                >
                  {aggregates.worstDay
                    ? `${fmtDate(aggregates.worstDay.date)} · ${fmtUSD(aggregates.worstDay.pnl)}`
                    : "—"}
                </span>
              </span>
            </div>
          </div>
        )}
      </GlassCard>

      {/* ── By vertical + by side ──────────────────────────────── */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <GlassCard tone="solid" padding="md">
          <SectionHeader
            eyebrow="SLICE"
            title="By vertical · 7d"
            subtitle="P&L and win rate of Edge Terminal trades grouped by sport vertical."
          />
          {Object.keys(aggregates.byVertical).length === 0 ? (
            <div className="text-xs text-muted-foreground">
              No Edge Terminal trades in the last 7 days yet.
            </div>
          ) : (
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
                  <th className="py-1.5 pr-3">Vertical</th>
                  <th className="py-1.5 pr-3 text-right">N</th>
                  <th className="py-1.5 pr-3 text-right">Hit</th>
                  <th className="py-1.5 pr-3 text-right">Volume</th>
                  <th className="py-1.5 pr-3 text-right">P&L</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(aggregates.byVertical)
                  .sort((a, b) => b[1].pnl - a[1].pnl)
                  .map(([v, row]) => (
                    <tr
                      key={v}
                      data-testid={`trends-vertical-row-${v}`}
                      className="border-b border-border/30 last:border-0"
                    >
                      <td className="py-1.5 pr-3 font-semibold uppercase tracking-wider text-foreground">
                        {v}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                        {row.n}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                        {row.settled > 0
                          ? `${((row.wins / row.settled) * 100).toFixed(0)}%`
                          : "—"}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                        {fmtUSD(row.vol, false)}
                      </td>
                      <td
                        className={`py-1.5 pr-3 text-right tabular-nums ${pnlColor(row.pnl)}`}
                      >
                        {fmtUSD(row.pnl)}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </GlassCard>

        <GlassCard tone="solid" padding="md">
          <SectionHeader
            eyebrow="SLICE"
            title="By side · 7d"
            subtitle="YES vs NO contract performance over the last 7 days."
          />
          {Object.keys(aggregates.bySide).length === 0 ? (
            <div className="text-xs text-muted-foreground">No fills yet.</div>
          ) : (
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
                  <th className="py-1.5 pr-3">Side</th>
                  <th className="py-1.5 pr-3 text-right">Trades</th>
                  <th className="py-1.5 pr-3 text-right">Settled</th>
                  <th className="py-1.5 pr-3 text-right">P&L</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(aggregates.bySide)
                  .sort((a, b) => b[1].pnl - a[1].pnl)
                  .map(([s, row]) => (
                    <tr
                      key={s}
                      data-testid={`trends-side-row-${s}`}
                      className="border-b border-border/30 last:border-0"
                    >
                      <td className="py-1.5 pr-3 font-mono text-foreground">
                        {s}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                        {row.n}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                        {row.settled}
                      </td>
                      <td
                        className={`py-1.5 pr-3 text-right tabular-nums ${pnlColor(row.pnl)}`}
                      >
                        {fmtUSD(row.pnl)}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </GlassCard>
      </div>

      {/* ── Full trade log, last 7 days ─────────────────────────── */}
      <GlassCard tone="solid" padding="md">
        <SectionHeader
          eyebrow="LOG"
          title="Trades — last 7 days"
          subtitle={`Every Edge Terminal fill in the last 7 days. ${trades.length} rows.`}
        />
        {tradesQ.isLoading && trades.length === 0 ? (
          <div className="text-xs text-muted-foreground">Loading…</div>
        ) : trades.length === 0 ? (
          <div className="text-xs text-muted-foreground">
            No Edge Terminal trades in the last 7 days.
          </div>
        ) : (
          <div className="overflow-x-auto" ref={tradesTableRef}>
            <table className="w-full min-w-[720px] text-left text-xs">
              <thead>
                <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
                  <th className="py-1.5 pr-3">When</th>
                  <th className="py-1.5 pr-3">Vertical</th>
                  <th className="py-1.5 pr-3">Ticker</th>
                  <th className="py-1.5 pr-3">Side</th>
                  <th className="py-1.5 pr-3 text-right">Qty</th>
                  <th className="py-1.5 pr-3 text-right">Price</th>
                  <th className="py-1.5 pr-3 text-right">Bet</th>
                  <th className="py-1.5 pr-3 text-right">P&L</th>
                </tr>
              </thead>
              <tbody>
                {tradesPaddingTop > 0 && (
                  <tr aria-hidden="true">
                    <td colSpan={8} style={{ height: tradesPaddingTop }} />
                  </tr>
                )}
                {tradeVirtualRows.map((virtualRow) => {
                  const t = trades[virtualRow.index];
                  const qty = Number(t.quantity ?? 0);
                  const px = Number(t.price ?? 0);
                  const bet = Math.abs(qty) * px;
                  return (
                    <tr
                      key={`${t.id ?? t.market_id}-${t.executed_at}`}
                      data-index={virtualRow.index}
                      ref={tradesVirtualizer.measureElement}
                      data-testid={`trends-trade-row-${t.id ?? "x"}`}
                      className="border-b border-border/30 last:border-0"
                    >
                      <td className="py-1.5 pr-3 text-muted-foreground">
                        {fmtDateTime(t.executed_at)}
                      </td>
                      <td className="py-1.5 pr-3 uppercase text-foreground">
                        {t.vertical ?? "—"}
                      </td>
                      <td className="py-1.5 pr-3 font-mono text-[11px] text-accent-cyan">
                        {t.market_id ?? "—"}
                      </td>
                      <td className="py-1.5 pr-3 font-mono text-foreground">
                        {(t.side ?? "?").toUpperCase()}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {fmtInt(qty)}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {px.toFixed(2)}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-emerald-300">
                        {fmtUSD(bet, false)}
                      </td>
                      <td
                        className={`py-1.5 pr-3 text-right tabular-nums ${pnlColor(t.pnl)}`}
                      >
                        {t.pnl === null || t.pnl === undefined
                          ? "open"
                          : fmtUSD(t.pnl)}
                      </td>
                    </tr>
                  );
                })}
                {tradesPaddingBottom > 0 && (
                  <tr aria-hidden="true">
                    <td colSpan={8} style={{ height: tradesPaddingBottom }} />
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>
    </div>
  );
}

function HeadlineTile({
  label,
  value,
  hint,
  tone,
  loading,
  testId,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
  loading?: boolean;
  testId: string;
}) {
  return (
    <GlassCard tone="solid" padding="sm" className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      {loading ? (
        <span
          className="block h-7 w-24 animate-pulse bg-muted/20"
          aria-busy="true"
        />
      ) : (
        <span
          className={`text-2xl font-bold tabular-nums ${tone ?? "text-foreground"}`}
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

function Sparkline({
  points,
  min,
  max,
}: {
  points: EquityPoint[];
  min: number;
  max: number;
}) {
  if (points.length < 2) return null;
  const W = 600;
  const H = 80;
  const range = Math.max(1e-6, max - min);
  const dx = W / (points.length - 1);
  const path = points
    .map((p, i) => {
      const x = i * dx;
      const y = H - ((p.cumulative_pnl - min) / range) * H;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const zeroY = H - ((0 - min) / range) * H;
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      className="h-20 w-full"
      data-testid="trends-equity-spark"
    >
      <line
        x1="0"
        x2={W}
        y1={zeroY}
        y2={zeroY}
        className="stroke-border"
        strokeDasharray="2 3"
        strokeWidth="1"
      />
      <path d={path} fill="none" className="stroke-accent-cyan" strokeWidth="2" />
    </svg>
  );
}
