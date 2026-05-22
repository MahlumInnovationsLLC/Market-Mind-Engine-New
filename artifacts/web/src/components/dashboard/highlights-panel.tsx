import { useMemo } from "react";
import {
  getGetRecentDebatesQueryKey,
  getGetRecentTradesQueryKey,
  useGetRecentDebates,
  useGetRecentTrades,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { useEffectivePlacedBy } from "./source-filter-context";
import { TileStaleBadge, useTileStaleClass } from "./tile-stale-badge";

function fmtCurrency(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

function fmtPct(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function HighlightsPanel() {
  // Trade-provenance filter (T004 / task #849).  "Best win" /
  // "biggest miss" are pure P&L so they belong in the toggle's blast
  // radius; the upstream debate feed is now also source-aware — Edge
  // Terminal bypasses the swarm so an ``edge_terminal`` selection
  // returns an empty TOP PICKS list, matching the operator's "only
  // candidates Edge Terminal surfaced" expectation.
  const placedBy = useEffectivePlacedBy();
  const staleClass = useTileStaleClass();
  const tradeParams = { limit: 100, placed_by: placedBy };
  const debateParams = { limit: 25, placed_by: placedBy };
  const tradesQ = useGetRecentTrades(tradeParams, {
    query: {
      queryKey: getGetRecentTradesQueryKey(tradeParams),
      refetchInterval: 60_000,
    },
  });
  const debatesQ = useGetRecentDebates(debateParams, {
    query: {
      queryKey: getGetRecentDebatesQueryKey(debateParams),
      refetchInterval: 60_000,
    },
  });

  const trades = tradesQ.data?.items ?? [];
  const debates = debatesQ.data?.items ?? [];

  const { topPicks, bestWin, biggestMiss } = useMemo(() => {
    const picks = debates
      .filter((d) => d.approved && (d.recommended_action ?? "").toUpperCase() !== "PASS")
      .sort(
        (a, b) =>
          (b.consensus_probability ?? 0) - (a.consensus_probability ?? 0),
      )
      .slice(0, 3);

    const settled = trades.filter(
      (t) => t.pnl !== null && t.pnl !== undefined,
    );
    let win: typeof trades[number] | null = null;
    let miss: typeof trades[number] | null = null;
    for (const t of settled) {
      const p = t.pnl as number;
      if (!win || p > (win.pnl as number)) win = t;
      if (!miss || p < (miss.pnl as number)) miss = t;
    }
    return { topPicks: picks, bestWin: win, biggestMiss: miss };
  }, [trades, debates]);

  const isLoading = tradesQ.isLoading || debatesQ.isLoading;

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="highlights-panel"
      className={staleClass}
    >
      <TileStaleBadge testId="highlights-panel-stale-badge" />
      <SectionHeader
        eyebrow="OPS"
        title="Highlights"
        subtitle="Top approved picks and the day's outliers."
      />
      {isLoading ? (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          <div className="h-28 animate-pulse bg-muted/10" />
          <div className="h-28 animate-pulse bg-muted/10" />
          <div className="h-28 animate-pulse bg-muted/10" />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          <div
            className="border border-border bg-card/40 p-3"
            data-testid="highlight-top-picks"
          >
            <div className="mb-2 text-[10px] uppercase tracking-widest text-accent-cyan/80">
              TOP PICKS
            </div>
            {topPicks.length === 0 ? (
              <div className="text-xs text-muted-foreground">
                NO_APPROVED_PICKS
              </div>
            ) : (
              <ul className="space-y-2 text-xs">
                {topPicks.map((d) => {
                  const action = (d.recommended_action ?? "").toUpperCase();
                  const actionClass = action.includes("BUY") || action.includes("YES")
                    ? "text-win"
                    : action.includes("SELL") || action.includes("NO")
                      ? "text-loss"
                      : "text-primary";
                  return (
                    <li
                      key={d.id}
                      className="flex items-start justify-between gap-2"
                    >
                      <div className="min-w-0">
                        <div className="truncate" title={d.matchup ?? ""}>
                          {d.matchup ?? "—"}
                        </div>
                        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                          {d.league ?? "—"} · {d.market_type ?? "—"} · {fmtTime(d.created_at)}
                        </div>
                      </div>
                      <div className="shrink-0 text-right">
                        <div className={`text-[10px] uppercase ${actionClass}`}>
                          {action || "—"}
                        </div>
                        <div className="text-xs font-bold tabular-nums text-primary">
                          {fmtPct(d.consensus_probability)}
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <div
            className="border border-border bg-card/40 p-3"
            data-testid="highlight-best-win"
          >
            <div className="mb-2 text-[10px] uppercase tracking-widest text-win">
              BEST WIN
            </div>
            {bestWin && (bestWin.pnl ?? 0) > 0 ? (
              <>
                <div
                  className="text-2xl font-bold tabular-nums text-win"
                  data-testid="text-best-win-pnl"
                >
                  +{fmtCurrency(bestWin.pnl)}
                </div>
                <div
                  className="mt-1 truncate text-xs text-foreground"
                  title={bestWin.market_id ?? ""}
                >
                  {bestWin.market_title ?? bestWin.market_id ?? "—"}
                </div>
                <div className="mt-1 text-[10px] uppercase tracking-widest text-muted-foreground">
                  {bestWin.vertical ?? "—"} · {fmtTime(bestWin.executed_at)}
                </div>
              </>
            ) : (
              <div className="text-xs text-muted-foreground">NO_WINS_YET</div>
            )}
          </div>

          <div
            className="border border-border bg-card/40 p-3"
            data-testid="highlight-biggest-miss"
          >
            <div className="mb-2 text-[10px] uppercase tracking-widest text-loss">
              BIGGEST MISS
            </div>
            {biggestMiss && (biggestMiss.pnl ?? 0) < 0 ? (
              <>
                <div
                  className="text-2xl font-bold tabular-nums text-loss"
                  data-testid="text-biggest-miss-pnl"
                >
                  {fmtCurrency(biggestMiss.pnl)}
                </div>
                <div
                  className="mt-1 truncate text-xs text-foreground"
                  title={biggestMiss.market_id ?? ""}
                >
                  {biggestMiss.market_title ?? biggestMiss.market_id ?? "—"}
                </div>
                <div className="mt-1 text-[10px] uppercase tracking-widest text-muted-foreground">
                  {biggestMiss.vertical ?? "—"} · {fmtTime(biggestMiss.executed_at)}
                </div>
              </>
            ) : (
              <div className="text-xs text-muted-foreground">NO_LOSSES_YET</div>
            )}
          </div>
        </div>
      )}
    </GlassCard>
  );
}
