import { useEffect, useMemo, useState } from "react";
import {
  getGetDashboardEdgeTerminalSignalsQueryKey,
  useGetDashboardEdgeTerminalSignals,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function fmtAmericanOdds(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  return v > 0 ? `+${Math.round(v)}` : `${Math.round(v)}`;
}

function fmtEvPct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
}

function fmtPickType(s: string | null | undefined): string {
  if (!s) return "—";
  return s.replace(/_/g, " ").toUpperCase();
}

function kalshiMarketUrl(
  ticker: string | null | undefined,
  marketId: string | null | undefined,
): string | null {
  const raw = (ticker ?? marketId ?? "").trim();
  if (!raw) return null;
  const parts = raw.split("-");
  const series = (parts[0] ?? "").toLowerCase();
  if (!series) return null;
  if (parts.length >= 2 && parts[1]) {
    return `https://kalshi.com/markets/${series}/${parts[1].toLowerCase()}`;
  }
  return `https://kalshi.com/markets/${series}`;
}

function safeHttpUrl(s: string | null | undefined): string | null {
  if (!s || typeof s !== "string") return null;
  try {
    const u = new URL(s);
    if (u.protocol === "http:" || u.protocol === "https:") return u.toString();
    return null;
  } catch {
    return null;
  }
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleTimeString();
  } catch {
    return s;
  }
}

function dispositionTone(d: string | null | undefined): string {
  const v = (d || "").toLowerCase();
  switch (v) {
    case "placed":
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    case "skipped_already_placed":
      // Deliberate "honored your existing position" — surface in the
      // same emerald family as a placed trade so the operator can see
      // at a glance that the executor agreed with their manual fill.
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-200";
    case "skipped_unresolved":
    case "skipped_gate":
    case "skipped_zero_kelly":
    case "skipped_slippage":
      return "border-amber-500/40 bg-amber-500/10 text-amber-300";
    case "errored":
    case "failed":
      return "border-rose-500/40 bg-rose-500/10 text-rose-300";
    case "pending":
      return "border-sky-500/40 bg-sky-500/10 text-sky-300";
    default:
      if (v.startsWith("skipped"))
        return "border-amber-500/40 bg-amber-500/10 text-amber-300";
      return "border-border bg-muted/30 text-muted-foreground";
  }
}

// Operator-friendly label rendered in the STATUS column.  Backend
// emits the structured ``skipped_*`` taxonomy; we collapse them here
// so the table reads "ALREADY PLACED" / "NO MARKET" rather than the
// raw enum.
export function dispositionLabel(d: string | null | undefined): string {
  const v = (d || "").toLowerCase();
  switch (v) {
    case "placed":
      return "PLACED";
    case "pending":
      return "PENDING";
    case "skipped_already_placed":
      return "ALREADY PLACED";
    case "skipped_unresolved":
      return "NO MARKET";
    case "skipped_zero_kelly":
      return "ZERO KELLY";
    case "skipped_slippage":
      return "SLIPPAGE";
    case "skipped_gate":
      return "GATE";
    case "skipped":
      return "SKIPPED";
    case "errored":
    case "failed":
      return "FAILED";
    default:
      return (d || "—").toUpperCase();
  }
}

type StatusFilter = "all" | "placed" | "pending" | "skipped" | "errored";

function matchesStatus(
  disposition: string | null | undefined,
  status: StatusFilter,
): boolean {
  if (status === "all") return true;
  const d = (disposition || "").toLowerCase();
  if (status === "skipped") return d.startsWith("skipped");
  return d === status;
}

const SELECT_CLASS =
  "rounded border border-border bg-background px-2 py-1 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent-cyan";

// localStorage key for persisting the operator's filter selections.
// Bumping this key invalidates any old persisted shape if we ever
// change the schema.
const FILTERS_STORAGE_KEY = "axiom.edge-terminal-recent-signals-filters.v1";

const STATUS_VALUES: readonly StatusFilter[] = [
  "all",
  "placed",
  "pending",
  "skipped",
  "errored",
];

type PersistedFilters = {
  status: StatusFilter;
  vertical: string;
  pickType: string;
  search: string;
};

const DEFAULT_FILTERS: PersistedFilters = {
  status: "all",
  vertical: "all",
  pickType: "all",
  search: "",
};

function loadStoredFilters(): PersistedFilters {
  if (typeof window === "undefined") return DEFAULT_FILTERS;
  try {
    const raw = window.localStorage.getItem(FILTERS_STORAGE_KEY);
    if (!raw) return DEFAULT_FILTERS;
    const parsed = JSON.parse(raw) as Partial<PersistedFilters> | null;
    if (!parsed || typeof parsed !== "object") return DEFAULT_FILTERS;
    const status =
      typeof parsed.status === "string" &&
      (STATUS_VALUES as readonly string[]).includes(parsed.status)
        ? (parsed.status as StatusFilter)
        : DEFAULT_FILTERS.status;
    const vertical =
      typeof parsed.vertical === "string"
        ? parsed.vertical
        : DEFAULT_FILTERS.vertical;
    const pickType =
      typeof parsed.pickType === "string"
        ? parsed.pickType
        : DEFAULT_FILTERS.pickType;
    const search =
      typeof parsed.search === "string" ? parsed.search : DEFAULT_FILTERS.search;
    return { status, vertical, pickType, search };
  } catch {
    return DEFAULT_FILTERS;
  }
}

export default function EdgeTerminalRecentSignalsTile() {
  const key = getGetDashboardEdgeTerminalSignalsQueryKey({ limit: 25 });
  const { data, isLoading, error } = useGetDashboardEdgeTerminalSignals(
    { limit: 25 },
    {
      query: { queryKey: key, refetchInterval: 30_000, staleTime: 25_000 },
    },
  );

  const items = data?.items ?? [];

  // Filters are hydrated synchronously from localStorage so an operator
  // who always looks at e.g. "placed NBA spreads" doesn't have to
  // re-apply the same selections on every refresh (task #853).
  const initialFilters = useMemo(() => loadStoredFilters(), []);
  const [status, setStatus] = useState<StatusFilter>(initialFilters.status);
  const [vertical, setVertical] = useState<string>(initialFilters.vertical);
  const [pickType, setPickType] = useState<string>(initialFilters.pickType);
  const [search, setSearch] = useState<string>(initialFilters.search);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        FILTERS_STORAGE_KEY,
        JSON.stringify({ status, vertical, pickType, search }),
      );
    } catch {
      // Quota exceeded / private mode — silently ignore; the
      // selections still work for the current session.
    }
  }, [status, vertical, pickType, search]);

  const filtersDirty =
    status !== DEFAULT_FILTERS.status ||
    vertical !== DEFAULT_FILTERS.vertical ||
    pickType !== DEFAULT_FILTERS.pickType ||
    search !== DEFAULT_FILTERS.search;

  const resetFilters = () => {
    setStatus(DEFAULT_FILTERS.status);
    setVertical(DEFAULT_FILTERS.vertical);
    setPickType(DEFAULT_FILTERS.pickType);
    setSearch(DEFAULT_FILTERS.search);
  };

  const verticalOptions = useMemo(() => {
    const set = new Set<string>();
    for (const s of items) {
      if (s.vertical) set.add(s.vertical);
    }
    return Array.from(set).sort();
  }, [items]);

  const pickTypeOptions = useMemo(() => {
    const set = new Set<string>();
    for (const s of items) {
      if (s.pick_type) set.add(s.pick_type);
    }
    return Array.from(set).sort();
  }, [items]);

  const filteredItems = useMemo(() => {
    const q = search.trim().toLowerCase();
    return items.filter((s) => {
      if (!matchesStatus(s.disposition, status)) return false;
      if (vertical !== "all" && (s.vertical ?? "") !== vertical) return false;
      if (pickType !== "all" && (s.pick_type ?? "") !== pickType) return false;
      if (q) {
        const hay = [s.matchup, s.selection, s.ticker]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [items, status, vertical, pickType, search]);

  const hasItems = items.length > 0;
  const allFiltered = hasItems && filteredItems.length === 0;

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-edge-terminal-recent-signals"
    >
      <SectionHeader
        eyebrow="EDGE TERMINAL"
        title="Recent signals"
        subtitle="Latest scraped picks with pick type, average odds and source EV%."
      />
      {isLoading ? (
        <div className="text-xs text-muted-foreground">Loading…</div>
      ) : error ? (
        <div
          data-testid="edge-terminal-recent-signals-error"
          className="text-xs text-rose-400"
        >
          Failed to load.
        </div>
      ) : data?.error ? (
        <div
          data-testid="edge-terminal-recent-signals-error"
          className="text-xs text-rose-400"
        >
          {data.error}
        </div>
      ) : !hasItems ? (
        <div className="text-xs text-muted-foreground">No signals yet.</div>
      ) : (
        <>
          <div
            data-testid="edge-terminal-recent-signals-filters"
            className="mb-3 flex flex-wrap items-center gap-2"
          >
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search matchup, selection, ticker"
              aria-label="Search signals"
              data-testid="edge-terminal-recent-signals-search"
              className={`${SELECT_CLASS} min-w-[200px] flex-1`}
            />
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value as StatusFilter)}
              aria-label="Filter by status"
              data-testid="edge-terminal-recent-signals-status-filter"
              className={SELECT_CLASS}
            >
              <option value="all">All statuses</option>
              <option value="placed">Placed</option>
              <option value="pending">Pending</option>
              <option value="skipped">Skipped</option>
              <option value="errored">Errored</option>
            </select>
            <select
              value={vertical}
              onChange={(e) => setVertical(e.target.value)}
              aria-label="Filter by vertical"
              data-testid="edge-terminal-recent-signals-vertical-filter"
              className={SELECT_CLASS}
            >
              <option value="all">All verticals</option>
              {verticalOptions.map((v) => (
                <option key={v} value={v}>
                  {v.toUpperCase()}
                </option>
              ))}
            </select>
            <select
              value={pickType}
              onChange={(e) => setPickType(e.target.value)}
              aria-label="Filter by pick type"
              data-testid="edge-terminal-recent-signals-pick-type-filter"
              className={SELECT_CLASS}
            >
              <option value="all">All pick types</option>
              {pickTypeOptions.map((p) => (
                <option key={p} value={p}>
                  {fmtPickType(p)}
                </option>
              ))}
            </select>
            {filtersDirty ? (
              <button
                type="button"
                onClick={resetFilters}
                data-testid="edge-terminal-recent-signals-reset-filters"
                className="rounded border border-border bg-muted/30 px-2 py-1 text-xs text-muted-foreground hover:text-foreground hover:border-accent-cyan/60"
              >
                Reset filters
              </button>
            ) : null}
          </div>
          {allFiltered ? (
            <div
              data-testid="edge-terminal-recent-signals-empty-filtered"
              className="text-xs text-muted-foreground"
            >
              No signals match the current filters.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-left text-xs">
                <thead>
                  <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
                    <th className="py-1.5 pr-3">When</th>
                    <th className="py-1.5 pr-3">Matchup</th>
                    <th className="py-1.5 pr-3">Selection</th>
                    <th className="py-1.5 pr-3">Pick</th>
                    <th className="py-1.5 pr-3 text-right">Avg odds</th>
                    <th className="py-1.5 pr-3 text-right">EV%</th>
                    <th className="py-1.5 pr-3">Market</th>
                    <th className="py-1.5 pr-3">Status</th>
                    <th className="py-1.5 pr-3 text-right">Src</th>
                  </tr>
                </thead>
                <tbody data-testid="edge-terminal-recent-signals-tbody">
                  {filteredItems.map((s) => {
                    const ev = typeof s.ev_pct === "number" ? s.ev_pct : null;
                    const evColor =
                      ev === null
                        ? "text-muted-foreground"
                        : ev >= 0
                          ? "text-emerald-400"
                          : "text-rose-400";
                    return (
                      <tr
                        key={s.id}
                        data-testid={`edge-terminal-recent-signal-row-${s.id}`}
                        className="border-b border-border/30 last:border-0"
                      >
                        <td className="py-1.5 pr-3 text-muted-foreground">
                          {fmtTime(s.scraped_at)}
                        </td>
                        <td className="py-1.5 pr-3 text-foreground">
                          {s.matchup ?? "—"}
                          {s.vertical ? (
                            <span className="ml-1 text-[10px] uppercase text-muted-foreground">
                              {s.vertical}
                            </span>
                          ) : null}
                        </td>
                        <td className="py-1.5 pr-3 text-foreground">
                          {s.selection ?? "—"}
                        </td>
                        <td className="py-1.5 pr-3">
                          <span
                            data-testid={`edge-terminal-pick-type-${s.id}`}
                            className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-foreground"
                          >
                            {fmtPickType(s.pick_type)}
                          </span>
                        </td>
                        <td
                          data-testid={`edge-terminal-avg-odds-${s.id}`}
                          className="py-1.5 pr-3 text-right tabular-nums text-foreground"
                        >
                          {fmtAmericanOdds(s.avg_odds)}
                        </td>
                        <td
                          data-testid={`edge-terminal-ev-pct-${s.id}`}
                          className={`py-1.5 pr-3 text-right tabular-nums ${evColor}`}
                        >
                          {fmtEvPct(s.ev_pct)}
                        </td>
                        <td className="py-1.5 pr-3 text-muted-foreground">
                          {(() => {
                            const href = kalshiMarketUrl(
                              s.ticker,
                              s.kalshi_market_id,
                            );
                            const label = s.ticker ?? "—";
                            return href ? (
                              <a
                                data-testid={`edge-terminal-ticker-link-${s.id}`}
                                href={href}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-accent-cyan hover:underline"
                              >
                                {label}
                              </a>
                            ) : (
                              <span>{label}</span>
                            );
                          })()}
                          {s.kalshi_market_id ? (
                            <span
                              data-testid={`edge-terminal-market-id-${s.id}`}
                              title={s.kalshi_market_id}
                              className="ml-1 cursor-help text-[10px] text-muted-foreground/60"
                            >
                              ⓘ
                            </span>
                          ) : null}
                        </td>
                        <td className="py-1.5 pr-3">
                          <span
                            className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${dispositionTone(s.disposition)}`}
                          >
                            {dispositionLabel(s.disposition)}
                          </span>
                        </td>
                        <td className="py-1.5 pr-3 text-right">
                          {(() => {
                            const href = safeHttpUrl(s.source_screenshot_url);
                            return href ? (
                              <a
                                data-testid={`edge-terminal-screenshot-${s.id}`}
                                href={href}
                                target="_blank"
                                rel="noreferrer noopener"
                                className="text-accent-cyan hover:underline"
                                aria-label="Open source screenshot"
                              >
                                🔗
                              </a>
                            ) : (
                              <span className="text-muted-foreground">—</span>
                            );
                          })()}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </GlassCard>
  );
}
