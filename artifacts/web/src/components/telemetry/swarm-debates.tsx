import { useCallback, useState } from "react";
import { Link } from "wouter";
import { useQueryClient } from "@tanstack/react-query";
import {
  useGetRecentDebates,
  getGetRecentDebatesQueryKey,
  useGetDebateEntrantMarkets,
  getGetDebateEntrantMarketsQueryKey,
  type DebateEntrantMarkets,
  type EntrantOutrightMarket,
} from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";
import DelayedSignalsBanner from "@/components/delayed-signals-banner";

function fmtPct(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtMoney(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `$${n.toFixed(3)}`;
}

function fmtLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

type FallbackDetail = {
  persona: string;
  fromProvider: string | null;
  fromModel: string | null;
  toProvider: string | null;
  toModel: string | null;
  reason: string | null;
};

function extractFallbacks(
  verdicts: unknown,
): FallbackDetail[] {
  if (!Array.isArray(verdicts)) return [];
  const out: FallbackDetail[] = [];
  for (const raw of verdicts) {
    if (!raw || typeof raw !== "object") continue;
    const v = raw as Record<string, unknown>;
    if (!v.fallback_used) continue;
    out.push({
      persona: typeof v.persona === "string" ? v.persona : "?",
      fromProvider: typeof v.primary_provider === "string" ? v.primary_provider : null,
      fromModel: typeof v.primary_model === "string" ? v.primary_model : null,
      toProvider: typeof v.provider === "string" ? v.provider : null,
      toModel: typeof v.model === "string" ? v.model : null,
      reason: typeof v.fallback_reason === "string" ? v.fallback_reason : null,
    });
  }
  return out;
}

type PlackettLuceSketch = {
  model?: string | null;
  entrant?: string | null;
  entrant_rating?: number | null;
  entrant_probability?: number | null;
  field_size?: number | null;
  ratings?: Record<string, number> | null;
  probabilities?: Record<string, number> | null;
};

type SketchRow = {
  entrant: string;
  rating: number | null;
  probability: number;
  isTraded: boolean;
  market: EntrantOutrightMarket | null;
};

function extractPlackettLuceSketch(
  gameContext: unknown,
): PlackettLuceSketch | null {
  if (!gameContext || typeof gameContext !== "object") return null;
  const ctx = gameContext as Record<string, unknown>;
  const raw = ctx["plackett_luce_sketch"];
  if (!raw || typeof raw !== "object") return null;
  const sketch = raw as Record<string, unknown>;
  const probsRaw = sketch["probabilities"];
  if (!probsRaw || typeof probsRaw !== "object") return null;
  const probs: Record<string, number> = {};
  for (const [k, v] of Object.entries(probsRaw as Record<string, unknown>)) {
    if (typeof v === "number" && Number.isFinite(v)) probs[k] = v;
  }
  if (Object.keys(probs).length === 0) return null;
  const ratingsRaw = sketch["ratings"];
  let ratings: Record<string, number> | null = null;
  if (ratingsRaw && typeof ratingsRaw === "object") {
    ratings = {};
    for (const [k, v] of Object.entries(ratingsRaw as Record<string, unknown>)) {
      if (typeof v === "number" && Number.isFinite(v)) ratings[k] = v;
    }
  }
  return {
    model: typeof sketch["model"] === "string" ? (sketch["model"] as string) : null,
    entrant: typeof sketch["entrant"] === "string" ? (sketch["entrant"] as string) : null,
    entrant_rating:
      typeof sketch["entrant_rating"] === "number"
        ? (sketch["entrant_rating"] as number)
        : null,
    entrant_probability:
      typeof sketch["entrant_probability"] === "number"
        ? (sketch["entrant_probability"] as number)
        : null,
    field_size:
      typeof sketch["field_size"] === "number" ? (sketch["field_size"] as number) : null,
    ratings,
    probabilities: probs,
  };
}

type SortKey = "probability" | "rating" | "entrant" | "mid";
type SortDir = "asc" | "desc";

function fmtCents(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${n}¢`;
}

function PlackettLuceTable({
  debateId,
  sketch,
}: {
  debateId: number;
  sketch: PlackettLuceSketch;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("probability");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // Per-entrant outright market resolution.  This component only mounts
  // for debates with a Plackett-Luce sketch (tournament-style fields),
  // so the endpoint should resolve as long as the title cache has the
  // sibling tickers.  Pro-only; sub-Pro callers (who never see
  // game_context, hence no sketch) won't reach this code path.
  const entrantMarketsQ = useGetDebateEntrantMarkets(debateId, {
    query: {
      staleTime: 30_000,
      queryKey: getGetDebateEntrantMarketsQueryKey(debateId),
    },
  });
  const entrantMarkets: DebateEntrantMarkets["entrants"] =
    entrantMarketsQ.data?.entrants ?? {};

  const probs = sketch.probabilities ?? {};
  const ratings = sketch.ratings ?? {};
  const tradedEntrant = sketch.entrant ?? null;

  const rows: SketchRow[] = Object.entries(probs).map(([entrant, probability]) => ({
    entrant,
    probability,
    rating: typeof ratings[entrant] === "number" ? ratings[entrant] : null,
    isTraded: tradedEntrant !== null && entrant === tradedEntrant,
    market:
      (entrantMarkets[entrant] as EntrantOutrightMarket | undefined) ?? null,
  }));

  const sorted = [...rows].sort((a, b) => {
    const dir = sortDir === "asc" ? 1 : -1;
    if (sortKey === "entrant") {
      return a.entrant.localeCompare(b.entrant) * dir;
    }
    if (sortKey === "rating") {
      const ar = a.rating ?? Number.NEGATIVE_INFINITY;
      const br = b.rating ?? Number.NEGATIVE_INFINITY;
      if (ar === br) return 0;
      return (ar < br ? -1 : 1) * dir;
    }
    if (sortKey === "mid") {
      const am = a.market?.mid_cents ?? Number.NEGATIVE_INFINITY;
      const bm = b.market?.mid_cents ?? Number.NEGATIVE_INFINITY;
      if (am === bm) return 0;
      return (am < bm ? -1 : 1) * dir;
    }
    if (a.probability === b.probability) return 0;
    return (a.probability < b.probability ? -1 : 1) * dir;
  });

  const probSum = rows.reduce((acc, r) => acc + r.probability, 0);
  const fieldSize = sketch.field_size ?? rows.length;

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // Default to descending for numeric columns, ascending for entrant.
      setSortDir(key === "entrant" ? "asc" : "desc");
    }
  };

  const sortIndicator = (key: SortKey) =>
    sortKey === key ? (sortDir === "desc" ? " ▾" : " ▴") : "";

  const hasRatings = rows.some((r) => r.rating !== null);

  return (
    <div
      className="mt-2 border border-border/60 bg-background/40"
      data-testid={`debate-pl-sketch-${debateId}`}
    >
      <div className="flex items-center justify-between border-b border-border/60 px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span className="font-bold text-foreground">FIELD_PROBABILITIES</span>
        <span>
          N={fieldSize} · Σ={probSum.toFixed(3)}
        </span>
      </div>
      <table className="w-full text-[10px]">
        <thead>
          <tr className="border-b border-border/40 text-muted-foreground uppercase tracking-wider">
            <th className="px-2 py-1 text-left font-normal">
              <button
                type="button"
                onClick={() => toggleSort("entrant")}
                className="hover:text-primary"
                data-testid={`debate-pl-sort-entrant-${debateId}`}
              >
                ENTRANT{sortIndicator("entrant")}
              </button>
            </th>
            <th className="px-2 py-1 text-right font-normal">
              <button
                type="button"
                onClick={() => toggleSort("rating")}
                className="hover:text-primary"
                data-testid={`debate-pl-sort-rating-${debateId}`}
              >
                RATING{sortIndicator("rating")}
              </button>
            </th>
            <th className="px-2 py-1 text-right font-normal">
              <button
                type="button"
                onClick={() => toggleSort("mid")}
                className="hover:text-primary"
                data-testid={`debate-pl-sort-mid-${debateId}`}
              >
                MID{sortIndicator("mid")}
              </button>
            </th>
            <th className="px-2 py-1 text-right font-normal">
              <button
                type="button"
                onClick={() => toggleSort("probability")}
                className="hover:text-primary"
                data-testid={`debate-pl-sort-probability-${debateId}`}
              >
                WIN_PROB{sortIndicator("probability")}
              </button>
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => {
            const market = r.market;
            const url = market?.kalshi_url ?? null;
            const entrantCell = (
              <div className="flex items-center gap-1">
                <span className="truncate" title={r.entrant}>
                  {r.entrant}
                </span>
                {r.isTraded ? (
                  <span
                    className="border border-primary/60 px-1 py-px text-[9px] uppercase tracking-widest text-primary"
                    data-testid={`debate-pl-traded-badge-${debateId}`}
                  >
                    TRADED
                  </span>
                ) : null}
              </div>
            );
            return (
              <tr
                key={r.entrant}
                data-testid={`debate-pl-row-${debateId}-${r.entrant}`}
                data-traded={r.isTraded ? "true" : "false"}
                data-market-id={market?.kalshi_market_id ?? ""}
                className={`border-b border-border/20 last:border-0 ${
                  r.isTraded ? "bg-primary/10 text-primary font-bold" : ""
                } ${url ? "hover:bg-muted/30" : ""}`}
              >
                <td className="px-2 py-1 text-left">
                  {url ? (
                    <a
                      href={url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="block hover:text-primary"
                      data-testid={`debate-pl-link-${debateId}-${r.entrant}`}
                      title={`Open ${market?.kalshi_market_id ?? r.entrant} on Kalshi`}
                    >
                      {entrantCell}
                    </a>
                  ) : (
                    entrantCell
                  )}
                </td>
                <td className="px-2 py-1 text-right tabular-nums text-muted-foreground">
                  {r.rating !== null ? r.rating.toFixed(0) : "—"}
                </td>
                <td
                  className="px-2 py-1 text-right tabular-nums"
                  data-testid={`debate-pl-mid-${debateId}-${r.entrant}`}
                >
                  {fmtCents(market?.mid_cents)}
                </td>
                <td className="px-2 py-1 text-right tabular-nums">
                  {(r.probability * 100).toFixed(1)}%
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {hasRatings ? null : (
        <div className="px-2 py-1 text-[10px] text-muted-foreground border-t border-border/40">
          Ratings unavailable for this field — probabilities only.
        </div>
      )}
    </div>
  );
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function SwarmDebates() {
  const params = { limit: 10 };
  const queryClient = useQueryClient();
  const { data, isLoading } = useGetRecentDebates(params, {
    query: {
      refetchInterval: 60_000,
      queryKey: getGetRecentDebatesQueryKey(params),
    },
  });

  const onLlmEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      // Refresh on debate completion / gatekeeper verdict events.
      if (t === "debate_complete" || t === "gatekeeper_verdict" || t === "moderator_consensus") {
        queryClient.invalidateQueries({ queryKey: getGetRecentDebatesQueryKey(params) });
      }
    },
    [queryClient],
  );
  const llm = useWebSocket({ path: "/api/ws/llm-logs", onMessage: onLlmEvent, bufferSize: 50 });

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-72 bg-card" />;
  }

  const items = data?.items ?? [];
  const live = llm.status === "open";

  return (
    <div className="border border-border bg-card flex flex-col">
      {data?.delayed ? (
        <DelayedSignalsBanner
          delayMinutes={data.delay_minutes}
          testId="banner-recent-debates-delayed"
        />
      ) : null}
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <div className="flex items-center gap-2">
          <span
            className={`w-1.5 h-1.5 rounded-full ${live ? "bg-success animate-pulse" : "bg-muted-foreground"}`}
          />
          <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
            SWARM_DEBATES
          </h2>
        </div>
        <span className="text-xs text-muted-foreground">CONSENSUS_VERDICTS</span>
      </div>
      <div className="overflow-auto divide-y divide-border/50">
        {items.length === 0 ? (
          <div className="p-4 text-center text-muted-foreground text-xs">NO_DEBATES_FOUND</div>
        ) : (
          items.map((d) => {
            const fallbacks = extractFallbacks(d.analyst_verdicts);
            const sketch = extractPlackettLuceSketch(d.game_context);
            const fallbackTitle = fallbacks.length
              ? fallbacks
                  .map(
                    (f) =>
                      `${f.persona}: ${f.fromProvider ?? "?"}/${f.fromModel ?? "?"} → ${f.toProvider ?? "?"}/${f.toModel ?? "?"}${f.reason ? ` (${f.reason})` : ""}`,
                  )
                  .join("\n")
              : "";
            const verdict = (d.recommended_action ?? "").toUpperCase();
            const verdictClass =
              verdict === "PASS"
                ? "text-muted-foreground border-muted-foreground/40"
                : verdict.includes("BUY") || verdict.includes("YES")
                  ? "text-win border-win/50"
                  : verdict.includes("SELL") || verdict.includes("NO")
                    ? "text-loss border-loss/50"
                    : "text-primary border-primary/50";
            return (
              <div
                key={d.id}
                data-focus-id={`debate-${d.id}`}
                className="p-3 hover:bg-muted/20 transition-colors text-xs scroll-mt-24 data-[focus-flash=true]:ring-2 data-[focus-flash=true]:ring-accent-cyan data-[focus-flash=true]:bg-accent-cyan/10"
              >
                <div className="flex items-start justify-between gap-2 mb-2">
                  <div className="min-w-0 flex-1">
                    <div className="text-foreground truncate" title={d.matchup ?? ""}>
                      {d.matchup ?? "—"}
                    </div>
                    <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
                      {d.league ?? "—"} · {d.market_type ?? "—"} · {fmtTime(d.created_at)}
                    </div>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {fallbacks.length > 0 && (
                      <span
                        className="text-[9px] uppercase border border-warning/60 text-warning px-1 py-0.5 tracking-widest"
                        data-testid={`debate-fallback-badge-${d.id}`}
                        title={fallbackTitle}
                      >
                        FALLBACK · {fallbacks.length}
                      </span>
                    )}
                    <div
                      className={`text-[10px] uppercase border px-2 py-0.5 ${verdictClass} ${d.approved ? "" : "opacity-70"}`}
                    >
                      {verdict || "—"}
                    </div>
                  </div>
                </div>
                {d.redacted ? (
                  <div
                    className="mb-2 border border-primary/40 bg-primary/5 px-2.5 py-2 flex items-center justify-between gap-2"
                    data-testid={`upsell-redacted-${d.id}`}
                  >
                    <div className="min-w-0">
                      <div className="text-[10px] uppercase tracking-wider text-primary font-bold">
                        PRO_ONLY
                      </div>
                      <div className="text-[11px] text-foreground/80 truncate">
                        Upgrade to Pro to see persona reasoning
                      </div>
                    </div>
                    <Link
                      href="/pricing"
                      className="shrink-0 text-[10px] uppercase tracking-widest font-semibold border border-primary/60 text-primary px-2 py-1 hover:bg-primary hover:text-primary-foreground transition-colors"
                      data-testid={`link-upgrade-${d.id}`}
                    >
                      UPGRADE →
                    </Link>
                  </div>
                ) : null}
                <div className="grid grid-cols-4 gap-2 text-[10px]">
                  <div>
                    <div className="text-muted-foreground">CONSENSUS</div>
                    <div className="text-primary font-bold">{fmtPct(d.consensus_probability, 1)}</div>
                  </div>
                  <div>
                    <div className="text-muted-foreground">BAND</div>
                    <div>
                      {fmtPct(d.confidence_band_low)}–{fmtPct(d.confidence_band_high)}
                    </div>
                  </div>
                  <div>
                    <div className="text-muted-foreground">AGREE</div>
                    <div>{fmtPct(d.agreement_rate)}</div>
                  </div>
                  <div className="text-right">
                    <div className="text-muted-foreground">COST · LAT</div>
                    <div>
                      {fmtMoney(d.cost_estimate)} · {fmtLatency(d.latency_ms)}
                    </div>
                  </div>
                </div>
                {sketch ? <PlackettLuceTable debateId={d.id} sketch={sketch} /> : null}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
