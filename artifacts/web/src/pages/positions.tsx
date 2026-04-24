import { useMemo, useState } from "react";
import { useLocation } from "wouter";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@clerk/react";
import {
  getGetRecentTradesQueryKey,
  useGetRecentTrades,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { apiFetch } from "@/lib/api";

type KalshiPosition = {
  market_ticker: string;
  market_title: string | null;
  bet_type: "YES" | "NO" | "FLAT";
  position: number;
  market_exposure_cents: number;
  realized_pnl_cents: number;
  fees_paid_cents: number;
  // Live mark-to-market: dollar value of the open position at current
  // mid/last market price (null when flat or no live quote available).
  current_value_cents: number | null;
  // Cost basis per contract, in cents 0..100 (avg entry on the chosen side).
  entry_price_cents: number | null;
  // Live YES-leg per-contract price in cents (mark for the YES side).
  last_price_cents: number | null;
};

type KalshiPositionsResponse = {
  items: KalshiPosition[];
  count: number;
};

type SortKey =
  | "market"
  | "side"
  | "qty"
  | "entry"
  | "mark"
  | "value"
  | "pnl"
  | "edge"
  | "age";

type SortDir = "asc" | "desc";

function fmtUsd(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || Number.isNaN(cents)) return "—";
  const usd = cents / 100;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(usd);
}

function fmtMoney(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const f = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(Math.abs(n));
  return n < 0 ? `-${f}` : f;
}

function pnlClass(c: number | null | undefined): string {
  if (c === null || c === undefined) return "text-foreground";
  if (c < 0) return "text-loss";
  if (c > 0) return "text-win";
  return "text-foreground";
}

function fmtAge(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86_400)}d`;
}

// Commodity-binary ticker parser — mirrors
// `app/commodities/tickers.py::parse_commodity_ticker` so the
// positions table can show symbol + timeframe + threshold per row
// when the row belongs to the `commodities` vertical.  Kept to a
// best-effort regex match: anything we can't parse just renders as
// the raw ticker (no breakdown badge).
const COMMODITY_PREFIX_TO_SYMBOL: Record<string, string> = {
  OIL: "WTI", WTI: "WTI", GOLD: "GOLD", NATGAS: "NG", NG: "NG",
  CORN: "CORN", SOY: "SOY", SOYB: "SOY", WHEAT: "WHEAT",
  COPPER: "COPPER", SILVER: "SILVER", COFFEE: "COFFEE", SUGAR: "SUGAR",
};
const COMMODITY_TF_SUFFIXES: Array<[string, string]> = [
  ["1H", "1h"], ["4H", "4h"], ["1D", "1d"], ["H", "1h"], ["D", "1d"],
];
const COMMODITY_BODY_RE =
  /^(?<sym>[A-Z]+(?:1H|4H|1D|H|D)?)[-_](?<when>[A-Z0-9]+)[-_](?<pfx>[A-Z])(?<k>\d+(?:\.\d+)?)$/;

function parseCommodityTicker(
  ticker: string,
): { symbol: string; timeframe: string; threshold: number; direction: "above" | "below" } | null {
  if (!ticker) return null;
  const body = ticker.replace(/^kx/i, "").toUpperCase();
  const m = COMMODITY_BODY_RE.exec(body);
  if (!m || !m.groups) return null;
  const symRaw = m.groups.sym;
  let symbol: string | null = null;
  let timeframe = "1d";
  for (const [suf, tf] of COMMODITY_TF_SUFFIXES) {
    if (symRaw.length > suf.length && symRaw.endsWith(suf)) {
      const base = symRaw.slice(0, -suf.length);
      if (COMMODITY_PREFIX_TO_SYMBOL[base]) {
        symbol = COMMODITY_PREFIX_TO_SYMBOL[base];
        timeframe = tf;
        break;
      }
    }
  }
  if (!symbol) {
    if (COMMODITY_PREFIX_TO_SYMBOL[symRaw]) {
      symbol = COMMODITY_PREFIX_TO_SYMBOL[symRaw];
    } else {
      const sortedPrefixes = Object.keys(COMMODITY_PREFIX_TO_SYMBOL).sort(
        (a, b) => b.length - a.length,
      );
      for (const p of sortedPrefixes) {
        if (symRaw.startsWith(p)) {
          symbol = COMMODITY_PREFIX_TO_SYMBOL[p];
          break;
        }
      }
    }
  }
  if (!symbol) return null;
  const threshold = Number.parseFloat(m.groups.k);
  if (!Number.isFinite(threshold) || threshold <= 0) return null;
  const direction: "above" | "below" = m.groups.pfx.toUpperCase() === "B" ? "below" : "above";
  return { symbol, timeframe, threshold, direction };
}

type Row = {
  market: string;
  marketTitle: string | null;
  betType: "YES" | "NO" | "FLAT";
  side: string | null;
  qty: number;
  entry: number | null;
  mark: number | null;
  pnlCents: number;
  // Live mark-to-market dollar value of the open position, in cents.
  // null when the position is flat or no live quote is available.
  currentValueCents: number | null;
  // Unrealized P&L in cents = current_value - cost_basis.  null when
  // either side is missing.
  unrealizedPnlCents: number | null;
  edge: number | null;
  ageSeconds: number | null;
  vertical: string | null;
  exchange: string | null;
  status: string;
  tradeRef: string | null;
};

export default function PositionsPage() {
  const [, setLocation] = useLocation();
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const positionsQ = useQuery<KalshiPositionsResponse>({
    queryKey: ["positions", "kalshi", "all"],
    queryFn: () =>
      apiFetch<KalshiPositionsResponse>("/api/kalshi/positions?limit=200", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const tradesParams = { limit: 200 };
  const tradesQ = useGetRecentTrades(tradesParams, {
    query: {
      queryKey: getGetRecentTradesQueryKey(tradesParams),
      refetchInterval: 60_000,
      staleTime: 30_000,
    },
  });

  const [verticalFilter, setVerticalFilter] = useState<string | null>(null);
  const [exchangeFilter, setExchangeFilter] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<"open" | "all">("open");
  const [sortKey, setSortKey] = useState<SortKey>("pnl");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const rows: Row[] = useMemo(() => {
    const positions = positionsQ.data?.items ?? [];
    const trades = tradesQ.data?.items ?? [];
    // Index trades by market_id for enrichment.
    const byMarket = new Map<string, typeof trades>();
    for (const t of trades) {
      const k = t.market_id ?? "";
      if (!k) continue;
      const arr = byMarket.get(k);
      if (arr) arr.push(t);
      else byMarket.set(k, [t]);
    }
    const now = Date.now();
    return positions.map((p) => {
      const matched = byMarket.get(p.market_ticker) ?? [];
      const sorted = matched
        .filter((t) => !!t.executed_at)
        .sort(
          (a, b) =>
            new Date(b.executed_at!).getTime() -
            new Date(a.executed_at!).getTime(),
        );
      const latest = sorted[0] ?? null;
      // All enrichment fields (side, vertical, entry, age) derive from
      // the **latest** matching fill — we don't have positional avg-cost
      // basis on /api/kalshi/positions, so the latest fill is the
      // closest signal we can surface without a new endpoint.
      // Prefer the API-derived cost basis (avg entry on the chosen side,
      // computed from market_exposure_cents / |position|) over the latest
      // fill price — the latter only reflects the most recent partial fill
      // and can disagree with the averaged cost basis after multiple fills.
      const entry =
        p.entry_price_cents ??
        (latest && typeof latest.price === "number" ? latest.price : null);
      const ageSeconds = latest?.executed_at
        ? Math.max(0, (now - new Date(latest.executed_at).getTime()) / 1000)
        : null;
      // Mark = per-contract live price for the position's chosen side.
      // For a YES position that's the YES mid; for NO it's (100 - YES mid).
      // The backend ships YES-leg `last_price_cents`; flip for NO so the
      // Mark column always shows the live price of the side actually held.
      let mark: number | null = null;
      if (p.last_price_cents !== null && p.last_price_cents !== undefined) {
        mark = p.position < 0 ? 100 - p.last_price_cents : p.last_price_cents;
      }
      const currentValueCents = p.current_value_cents ?? null;
      const unrealizedPnlCents =
        currentValueCents !== null && p.market_exposure_cents !== null
          ? currentValueCents - p.market_exposure_cents
          : null;
      return {
        market: p.market_ticker,
        marketTitle: p.market_title ?? null,
        betType: p.bet_type ?? "FLAT",
        side: latest?.side ?? null,
        qty: p.position,
        entry,
        mark,
        pnlCents: p.realized_pnl_cents,
        currentValueCents,
        unrealizedPnlCents,
        edge: null, // Edge not surfaced on /api/kalshi/positions; per-trade only.
        ageSeconds,
        vertical: latest?.vertical ?? null,
        exchange: latest?.exchange ?? "kalshi",
        status: p.position === 0 ? "closed" : "open",
        tradeRef: latest?.trade_id ?? (latest?.id ? String(latest.id) : null),
      };
    });
  }, [positionsQ.data, tradesQ.data]);

  const verticals = useMemo(() => {
    const s = new Set<string>();
    for (const r of rows) if (r.vertical) s.add(r.vertical);
    return Array.from(s).sort();
  }, [rows]);
  const exchanges = useMemo(() => {
    const s = new Set<string>();
    for (const r of rows) if (r.exchange) s.add(r.exchange);
    return Array.from(s).sort();
  }, [rows]);

  const filtered = useMemo(() => {
    return rows.filter((r) => {
      if (statusFilter === "open" && r.status !== "open") return false;
      if (verticalFilter && r.vertical !== verticalFilter) return false;
      if (exchangeFilter && r.exchange !== exchangeFilter) return false;
      return true;
    });
  }, [rows, statusFilter, verticalFilter, exchangeFilter]);

  const sorted = useMemo(() => {
    const dir = sortDir === "asc" ? 1 : -1;
    const key = sortKey;
    const get = (r: Row): number | string => {
      switch (key) {
        case "market":
          return r.market;
        case "side":
          return r.side ?? "";
        case "qty":
          return r.qty;
        case "entry":
          return r.entry ?? -Infinity;
        case "mark":
          return r.mark ?? -Infinity;
        case "value":
          return r.currentValueCents ?? -Infinity;
        case "pnl":
          return r.pnlCents;
        case "edge":
          return r.edge ?? -Infinity;
        case "age":
          return r.ageSeconds ?? -Infinity;
      }
    };
    return [...filtered].sort((a, b) => {
      const av = get(a);
      const bv = get(b);
      if (typeof av === "number" && typeof bv === "number") {
        return (av - bv) * dir;
      }
      return String(av).localeCompare(String(bv)) * dir;
    });
  }, [filtered, sortKey, sortDir]);

  function toggleSort(k: SortKey) {
    if (k === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(k);
      setSortDir(k === "market" || k === "side" ? "asc" : "desc");
    }
  }

  const isLoading = positionsQ.isLoading || tradesQ.isLoading;
  const isError = positionsQ.isError;

  return (
    <div className="space-y-5 font-mono" data-testid="page-positions">
      <header className="flex flex-col gap-2 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
            PORTFOLIO
          </div>
          <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
            Positions
          </h1>
          <p className="mt-1 text-xs text-muted-foreground">
            Open Kalshi book, sortable. Click any row to open the trade
            detail with model-prob vs market-price chart.
          </p>
        </div>
        <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
          {sorted.length} of {rows.length} shown
        </div>
      </header>

      <GlassCard tone="solid" padding="sm" data-testid="card-positions-filters">
        <div className="flex flex-wrap items-center gap-4">
          <ChipGroup
            label="Status"
            value={statusFilter}
            options={[
              { value: "open", label: "Open" },
              { value: "all", label: "All" },
            ]}
            onChange={(v) => setStatusFilter((v as "open" | "all") ?? "open")}
            testId="chips-status"
          />
          <ChipGroup
            label="Vertical"
            value={verticalFilter}
            options={[
              { value: null, label: "Any" },
              ...verticals.map((v) => ({ value: v, label: v.toUpperCase() })),
            ]}
            onChange={(v) => setVerticalFilter(v as string | null)}
            testId="chips-vertical"
          />
          <ChipGroup
            label="Exchange"
            value={exchangeFilter}
            options={[
              { value: null, label: "Any" },
              ...exchanges.map((v) => ({ value: v, label: v.toUpperCase() })),
            ]}
            onChange={(v) => setExchangeFilter(v as string | null)}
            testId="chips-exchange"
          />
        </div>
      </GlassCard>

      <GlassCard tone="solid" padding="md" data-testid="card-positions-table">
        <SectionHeader
          eyebrow="BOOK"
          title="Open positions"
          subtitle="Live Kalshi book enriched with the most recent fill per market."
        />
        {isLoading ? (
          <div className="space-y-2" data-testid="skeleton-positions">
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className="h-8 animate-pulse bg-muted/10" />
            ))}
          </div>
        ) : isError ? (
          <div className="border border-loss/40 bg-loss/5 p-3 text-[11px] uppercase tracking-widest text-loss">
            KALSHI_FEED_UNAVAILABLE
          </div>
        ) : sorted.length === 0 ? (
          <div className="border border-border bg-card/40 p-6 text-center text-xs uppercase tracking-widest text-muted-foreground">
            NO_POSITIONS_MATCH_FILTERS
          </div>
        ) : (
          <div className="overflow-auto">
            <table className="w-full text-xs">
              <thead className="border-b border-border text-[10px] uppercase tracking-widest text-muted-foreground">
                <tr>
                  <Th label="Market" k="market" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} align="left" />
                  <Th label="Side" k="side" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} align="left" />
                  <Th label="Qty" k="qty" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} align="right" />
                  <Th label="Entry" k="entry" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} align="right" />
                  <Th label="Mark" k="mark" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} align="right" title="Live per-contract mid for the side held (YES mid for YES; 100 - YES mid for NO)" />
                  <Th label="Value" k="value" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} align="right" title="Live mark-to-market value of the open position" />
                  <Th label="P&L" k="pnl" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} align="right" />
                  <Th label="Edge" k="edge" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} align="right" disabled title="Edge is per-trade — open a row to view" />
                  <Th label="Age" k="age" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} align="right" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border/50">
                {sorted.map((r) => {
                  const clickable = !!r.tradeRef;
                  const onActivate = () => {
                    if (r.tradeRef) {
                      setLocation(`/trade/${encodeURIComponent(r.tradeRef)}`);
                    }
                  };
                  return (
                    <tr
                      key={r.market}
                      data-testid={`row-position-${r.market}`}
                      className={`hover:bg-muted/20 ${clickable ? "cursor-pointer" : ""}`}
                      onClick={clickable ? onActivate : undefined}
                      onKeyDown={
                        clickable
                          ? (e) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                onActivate();
                              }
                            }
                          : undefined
                      }
                      tabIndex={clickable ? 0 : undefined}
                      role={clickable ? "link" : undefined}
                    >
                      <td
                        className="py-2 pr-2 max-w-[320px]"
                        title={r.market}
                      >
                        <div className="flex items-baseline gap-2">
                          <div className="truncate text-[12px] text-foreground">
                            {r.marketTitle ?? r.market}
                          </div>
                          {r.vertical && (
                            <span className="shrink-0 text-[10px] uppercase text-muted-foreground">
                              {r.vertical}
                            </span>
                          )}
                        </div>
                        {r.marketTitle && (
                          <div
                            className="truncate font-mono text-[10px] text-muted-foreground"
                            data-testid={`pos-row-ticker-${r.market}`}
                          >
                            {r.market}
                          </div>
                        )}
                        {r.vertical === "commodities" && (() => {
                          const parsed = parseCommodityTicker(r.market);
                          if (!parsed) return null;
                          return (
                            <div
                              className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground"
                              data-testid={`row-commodity-breakdown-${r.market}`}
                            >
                              <span className="border border-primary/40 bg-primary/5 px-1.5 text-primary">
                                {parsed.symbol}
                              </span>
                              <span className="border border-border px-1.5">
                                {parsed.timeframe}
                              </span>
                              <span className="border border-border px-1.5 normal-case">
                                {parsed.direction === "above" ? ">" : "<"}{" "}
                                {parsed.threshold}
                              </span>
                            </div>
                          );
                        })()}
                      </td>
                      <td className="py-2">
                        <span
                          className={`border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-widest ${
                            r.betType === "YES"
                              ? "border-win/40 bg-win/5 text-win"
                              : r.betType === "NO"
                              ? "border-loss/40 bg-loss/5 text-loss"
                              : "border-border text-muted-foreground"
                          }`}
                          data-testid={`pos-row-bet-${r.market}`}
                        >
                          {r.betType}
                        </span>
                      </td>
                      <td className="py-2 text-right tabular-nums">{Math.abs(r.qty)}</td>
                      <td className="py-2 text-right tabular-nums">
                        {fmtMoney(r.entry)}
                      </td>
                      <td
                        className="py-2 text-right tabular-nums text-muted-foreground"
                        data-testid={`pos-row-mark-${r.market}`}
                      >
                        {r.mark === null ? "—" : `${r.mark.toFixed(0)}¢`}
                      </td>
                      <td
                        className="py-2 text-right tabular-nums"
                        data-testid={`pos-row-value-${r.market}`}
                      >
                        {r.currentValueCents === null ? (
                          <span className="text-muted-foreground">—</span>
                        ) : (
                          <div>
                            <div className="text-foreground">
                              {fmtUsd(r.currentValueCents)}
                            </div>
                            {r.unrealizedPnlCents !== null && (
                              <div
                                className={`text-[10px] ${pnlClass(r.unrealizedPnlCents)}`}
                                data-testid={`pos-row-unrealized-${r.market}`}
                              >
                                {r.unrealizedPnlCents >= 0 ? "+" : ""}
                                {fmtUsd(r.unrealizedPnlCents)}
                              </div>
                            )}
                          </div>
                        )}
                      </td>
                      <td
                        className={`py-2 text-right tabular-nums font-bold ${pnlClass(r.pnlCents)}`}
                      >
                        {fmtUsd(r.pnlCents)}
                      </td>
                      <td className="py-2 text-right tabular-nums text-muted-foreground">
                        {r.edge === null ? "—" : `${(r.edge * 100).toFixed(1)}%`}
                      </td>
                      <td className="py-2 text-right tabular-nums text-muted-foreground">
                        {fmtAge(r.ageSeconds)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </GlassCard>
    </div>
  );
}

function Th({
  label,
  k,
  sortKey,
  sortDir,
  onClick,
  align,
  disabled = false,
  title,
}: {
  label: string;
  k: SortKey;
  sortKey: SortKey;
  sortDir: SortDir;
  onClick: (k: SortKey) => void;
  align: "left" | "right";
  disabled?: boolean;
  title?: string;
}) {
  const active = sortKey === k && !disabled;
  const arrow = active ? (sortDir === "asc" ? "▲" : "▼") : "";
  return (
    <th
      className={`py-1 font-normal select-none ${
        align === "right" ? "text-right" : "text-left"
      } ${
        disabled
          ? "cursor-default text-muted-foreground/60"
          : `cursor-pointer ${active ? "text-primary" : "hover:text-foreground"}`
      }`}
      onClick={disabled ? undefined : () => onClick(k)}
      title={title}
      data-testid={`th-${k}`}
    >
      {label} {arrow}
    </th>
  );
}

function ChipGroup<T extends string | null>({
  label,
  value,
  options,
  onChange,
  testId,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  testId: string;
}) {
  return (
    <div className="flex items-center gap-2" data-testid={testId}>
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </span>
      <div className="flex flex-wrap gap-1">
        {options.map((opt) => {
          const active = opt.value === value;
          return (
            <button
              key={opt.label}
              type="button"
              onClick={() => onChange(opt.value)}
              className={`border px-2 py-0.5 text-[10px] uppercase tracking-widest transition-colors ${
                active
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:border-primary hover:text-primary"
              }`}
              data-testid={`chip-${testId}-${opt.label.toLowerCase()}`}
              aria-pressed={active}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
