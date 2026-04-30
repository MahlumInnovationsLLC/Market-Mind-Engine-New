import { useCallback, useMemo, useState } from "react";
import { useLocation } from "wouter";
import { useQueryClient } from "@tanstack/react-query";
import { useGetRecentTrades, getGetRecentTradesQueryKey } from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";
import {
  usePipelineSocket,
  usePipelineSubscription,
} from "@/components/realtime/websocket-provider";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import { usePlacedBy } from "@/components/dashboard/placed-by-context";
import DelayedSignalsBanner from "@/components/delayed-signals-banner";
import {
  KNOWN_VENUES,
  normalizeVenue,
  venueDisplayLabel,
  type KnownVenue,
} from "@/lib/venues";

function formatMoney(num: number | null | undefined): string {
  if (num === null || num === undefined) return "—";
  const formatted = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Math.abs(num));
  return num < 0 ? `-${formatted}` : formatted;
}

type VenueFilter = "all" | KnownVenue | "other";
type SortKey = "time" | "venue";
type SortDir = "asc" | "desc";

const VENUE_BADGE_CLASS: Record<KnownVenue | "other" | "unknown", string> = {
  kalshi: "border-primary/60 bg-primary/10 text-primary",
  polymarket: "border-warning/60 bg-warning/10 text-warning",
  other: "border-border bg-card/40 text-muted-foreground",
  unknown: "border-border bg-card/40 text-muted-foreground/60",
};

function VenueBadge({ exchange }: { exchange: string | null | undefined }) {
  const norm = normalizeVenue(exchange);
  const cls = VENUE_BADGE_CLASS[norm ?? "unknown"];
  return (
    <span
      className={`inline-flex items-center border px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${cls}`}
      data-testid="trade-venue-badge"
      data-venue={norm ?? "unknown"}
    >
      {venueDisplayLabel(exchange)}
    </span>
  );
}

export default function RecentTrades() {
  // Trade-provenance filter (T004).  Lives in URL via PlacedByProvider
  // so reload + share preserves the operator's chosen segment; the
  // server-side WHERE keeps the LIMIT 25 slice meaningful.
  const { placedBy } = usePlacedBy();
  const params = { limit: 25, placed_by: placedBy };
  const queryClient = useQueryClient();
  const [, setLocation] = useLocation();
  const { data, isLoading } = useGetRecentTrades(params, {
    query: { refetchInterval: 60_000, queryKey: getGetRecentTradesQueryKey(params) },
  });

  const onPipelineEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      if (
        t === "candidate_complete" ||
        t === "cycle_complete" ||
        t === "trade_settled"
      ) {
        queryClient.invalidateQueries({ queryKey: getGetRecentTradesQueryKey(params) });
      }
    },
    [queryClient, params],
  );
  const onSettlementEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      if (t === "trade_settled" || t === "market_settle_complete") {
        queryClient.invalidateQueries({ queryKey: getGetRecentTradesQueryKey(params) });
      }
    },
    [queryClient, params],
  );
  usePipelineSubscription(onPipelineEvent);
  const pipeline = usePipelineSocket();
  useWebSocket({ path: "/api/ws/settlement", onMessage: onSettlementEvent, bufferSize: 50 });
  const { matches } = useVerticalFilter();

  const [venueFilter, setVenueFilter] = useState<VenueFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("time");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // Surface only venues that actually appear in the current page so the
  // pill row stays honest (no orphaned "Polymarket" pill on a Kalshi-
  // only feed).
  const allTrades = data?.items || [];
  const venuesPresent = useMemo(() => {
    const set = new Set<KnownVenue | "other">();
    for (const t of allTrades) {
      const n = normalizeVenue(t.exchange);
      if (n) set.add(n);
    }
    return set;
  }, [allTrades]);

  const verticalFiltered = allTrades.filter((t) => matches(t.vertical));
  const venueFiltered = verticalFiltered.filter((t) => {
    if (venueFilter === "all") return true;
    return normalizeVenue(t.exchange) === venueFilter;
  });

  const trades = useMemo(() => {
    if (sortKey === "time") {
      // Default order from the API is already executed_at DESC; only
      // reverse when the operator picks ascending.
      return sortDir === "desc" ? venueFiltered : [...venueFiltered].reverse();
    }
    // sort by venue: stable sort within the venue group by executed_at
    // (already in descending order from the API), then group by venue.
    const collator = new Intl.Collator("en", { sensitivity: "base" });
    const arr = [...venueFiltered];
    arr.sort((a, b) => {
      const va = (a.exchange ?? "").toString();
      const vb = (b.exchange ?? "").toString();
      const cmp = collator.compare(va, vb);
      return sortDir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [venueFiltered, sortKey, sortDir]);

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-96 bg-card" />;
  }

  const live = pipeline.status === "open";

  // Pills always include "All"; per-venue pills appear when the venue
  // is present in the current feed OR currently selected (so the
  // operator can deselect their own filter even if the venue has
  // since dropped off the page).
  const pillVenues: VenueFilter[] = ["all"];
  for (const v of KNOWN_VENUES) {
    if (venuesPresent.has(v) || venueFilter === v) pillVenues.push(v);
  }
  if (venuesPresent.has("other") || venueFilter === "other") pillVenues.push("other");

  const toggleSort = (key: SortKey) => {
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir(key === "time" ? "desc" : "asc");
    } else {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    }
  };

  return (
    <div className="border border-border bg-card overflow-hidden flex flex-col h-[500px]">
      {data?.delayed ? (
        <DelayedSignalsBanner
          delayMinutes={data.delay_minutes}
          testId="banner-recent-trades-delayed"
        />
      ) : null}
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <div className="flex items-center gap-2">
          <span
            className={`w-1.5 h-1.5 rounded-full ${live ? "bg-success animate-pulse" : "bg-muted-foreground"}`}
          />
          <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">RECENT_FILLS</h2>
        </div>
        <span className="text-xs text-muted-foreground">LAST 25</span>
      </div>

      <div
        className="border-b border-border bg-muted/10 px-3 py-1.5 flex flex-wrap items-center gap-2 shrink-0"
        data-testid="recent-trades-venue-filter"
      >
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground mr-1">
          VENUE
        </span>
        {pillVenues.map((pill) => {
          const active = venueFilter === pill;
          const label =
            pill === "all" ? "All" : pill === "other" ? "Other" : venueDisplayLabel(pill);
          return (
            <button
              key={pill}
              type="button"
              onClick={() => setVenueFilter(pill)}
              aria-pressed={active}
              data-testid={`venue-filter-${pill}`}
              className={`border px-2 py-0.5 text-[10px] uppercase tracking-widest transition-colors ${
                active
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border bg-card/30 text-muted-foreground hover:border-primary hover:text-primary"
              }`}
            >
              {label}
            </button>
          );
        })}
      </div>

      <div className="overflow-auto grow">
        <table className="w-full text-xs text-left">
          <thead className="sticky top-0 bg-card border-b border-border text-muted-foreground uppercase text-[10px]">
            <tr>
              <th className="p-2 font-normal">
                <SortHeader
                  label="TIME"
                  active={sortKey === "time"}
                  dir={sortDir}
                  onClick={() => toggleSort("time")}
                  testId="sort-time"
                />
              </th>
              <th className="p-2 font-normal">MARKET</th>
              <th className="p-2 font-normal">
                <SortHeader
                  label="VENUE"
                  active={sortKey === "venue"}
                  dir={sortDir}
                  onClick={() => toggleSort("venue")}
                  testId="sort-venue"
                />
              </th>
              <th className="p-2 font-normal">SIDE/ACT</th>
              <th className="p-2 font-normal text-right">QTY</th>
              <th className="p-2 font-normal text-right">PRICE</th>
              <th className="p-2 font-normal text-right">PNL</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {trades.length === 0 ? (
              <tr>
                <td
                  colSpan={7}
                  className="p-4 text-center text-muted-foreground"
                  data-testid="recent-trades-empty"
                >
                  NO_TRADES_FOUND
                </td>
              </tr>
            ) : trades.map((trade) => {
              const isLoss = trade.pnl !== null && trade.pnl !== undefined && trade.pnl < 0;
              const isWin = trade.pnl !== null && trade.pnl !== undefined && trade.pnl > 0;
              const pnlClass = isLoss ? "text-loss" : isWin ? "text-win" : "text-muted-foreground";

              const isBuy = trade.action?.toLowerCase() === 'buy';
              const actionClass = isBuy ? "text-primary" : "text-loss";

              return (
                <tr
                  key={trade.id}
                  data-focus-id={`trade-${trade.id}`}
                  data-testid={`trade-row-${trade.id}`}
                  onClick={() => setLocation(`/trade/${trade.id}`)}
                  className="cursor-pointer hover:bg-muted/30 transition-colors scroll-mt-24 data-[focus-flash=true]:outline data-[focus-flash=true]:outline-2 data-[focus-flash=true]:outline-accent-cyan data-[focus-flash=true]:bg-accent-cyan/10"
                >
                  <td className="p-2 whitespace-nowrap text-muted-foreground">
                    {trade.executed_at ? new Date(trade.executed_at).toLocaleTimeString([], {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'}) : "—"}
                  </td>
                  <td className="p-2 max-w-[200px] truncate" title={trade.market_id || ""}>
                    {trade.market_title || trade.market_id || "—"}
                  </td>
                  <td className="p-2 whitespace-nowrap">
                    <VenueBadge exchange={trade.exchange} />
                  </td>
                  <td className="p-2 whitespace-nowrap">
                    <span className="uppercase">{trade.side || "—"}</span> / <span className={`${actionClass} uppercase`}>{trade.action || "—"}</span>
                  </td>
                  <td className="p-2 text-right">{trade.quantity ?? "—"}</td>
                  <td className="p-2 text-right">{formatMoney(trade.price)}</td>
                  <td className={`p-2 text-right font-bold ${pnlClass}`}>
                    {formatMoney(trade.pnl)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

interface SortHeaderProps {
  label: string;
  active: boolean;
  dir: SortDir;
  onClick: () => void;
  testId?: string;
}

function SortHeader({ label, active, dir, onClick, testId }: SortHeaderProps) {
  const arrow = !active ? "↕" : dir === "asc" ? "↑" : "↓";
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testId}
      aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
      className={`inline-flex items-center gap-1 uppercase tracking-wider text-[10px] ${
        active ? "text-primary" : "text-muted-foreground hover:text-primary"
      }`}
    >
      {label}
      <span aria-hidden className="opacity-70">{arrow}</span>
    </button>
  );
}
