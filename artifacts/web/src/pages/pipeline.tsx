import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@clerk/react";
import { getMarketTitle } from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";
import {
  usePipelineSocket,
  usePipelineSubscription,
} from "@/components/realtime/websocket-provider";
import WsStatusIndicator from "@/components/ws-status-indicator";
import { apiFetch } from "@/lib/api";
import SwarmTranscript, { useSwarmTranscript } from "@/components/swarm-transcript";
import PipelineFlowchart from "@/components/pipeline/pipeline-flowchart";
import ConsoleTail from "@/components/pipeline/console-tail";
import TradeTrainTicker from "@/components/pipeline/trade-train-ticker";
import BackfillPanel from "@/components/pipeline/backfill-panel";
import RecentRetrains from "@/components/telemetry/recent-retrains";
import { extractVenuesFromEvent, venueDisplayLabel } from "@/lib/venues";

/**
 * Pipeline-page-local rolling buffer of pipeline-WS frames. The
 * underlying socket is the shared one mounted by `<WebSocketProvider>`
 * in the app shell — we deliberately do NOT open a second `useWebSocket`
 * here. This hook layers a 500-frame ring buffer + clear() on top of
 * `usePipelineSubscription` so the page can hydrate its cycles list
 * the same way it used to.
 */
function usePipelineEventBuffer(bufferSize = 500): {
  events: Record<string, unknown>[];
  clear: () => void;
} {
  const [events, setEvents] = useState<Record<string, unknown>[]>([]);
  usePipelineSubscription((event) => {
    setEvents((prev) => {
      const next = prev.length >= bufferSize ? prev.slice(1) : prev.slice();
      next.push(event);
      return next;
    });
  });
  const clear = useCallback(() => setEvents([]), []);
  return { events, clear };
}

type TitleMap = Record<string, string | null>;

const MarketTitleContext = createContext<{
  titles: TitleMap;
  request: (ticker: string) => void;
}>({ titles: {}, request: () => {} });

function useMarketTitleResolver() {
  const [titles, setTitles] = useState<TitleMap>({});
  const inflight = useRef<Set<string>>(new Set());
  const queue = useRef<Set<string>>(new Set());
  const flushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = () => {
    flushTimer.current = null;
    const batch = Array.from(queue.current);
    queue.current.clear();
    for (const ticker of batch) {
      if (inflight.current.has(ticker)) continue;
      inflight.current.add(ticker);
      getMarketTitle(ticker)
        .then((res) => {
          setTitles((prev) =>
            prev[ticker] === (res?.title ?? null)
              ? prev
              : { ...prev, [ticker]: res?.title ?? null },
          );
        })
        .catch(() => {
          setTitles((prev) =>
            ticker in prev ? prev : { ...prev, [ticker]: null },
          );
        })
        .finally(() => {
          inflight.current.delete(ticker);
        });
    }
  };

  const request = (ticker: string) => {
    if (!ticker) return;
    if (ticker in titles) return;
    if (inflight.current.has(ticker) || queue.current.has(ticker)) return;
    queue.current.add(ticker);
    if (flushTimer.current === null) {
      flushTimer.current = setTimeout(flush, 80);
    }
  };

  useEffect(() => {
    return () => {
      if (flushTimer.current !== null) clearTimeout(flushTimer.current);
    };
  }, []);

  return { titles, request };
}

function MarketLabel({
  ticker,
  className,
  fallback,
}: {
  ticker: string | null | undefined;
  className?: string;
  fallback?: string;
}) {
  const { titles, request } = useContext(MarketTitleContext);
  useEffect(() => {
    if (ticker) request(ticker);
  }, [ticker, request]);
  if (!ticker) {
    return <span className={className}>{fallback ?? "—"}</span>;
  }
  const resolved = titles[ticker];
  return (
    <span className={className} title={ticker}>
      {resolved || ticker}
    </span>
  );
}

interface CycleGroup {
  cycle_id: string | null;
  vertical: string | null;
  started_at: string | null;
  completed_at: string | null;
  status: "running" | "complete" | "halted";
  events: Record<string, unknown>[];
  candidates: Map<string, Record<string, unknown>[]>;
  trades_placed: number;
  picks_evaluated: number;
  errors: string | null;
}

function pickStr(o: Record<string, unknown>, k: string): string | null {
  const v = o[k];
  return typeof v === "string" ? v : null;
}
function pickNum(o: Record<string, unknown>, k: string): number | null {
  const v = o[k];
  return typeof v === "number" ? v : null;
}

function groupByCycle(events: Record<string, unknown>[]): CycleGroup[] {
  const map = new Map<string, CycleGroup>();
  const order: string[] = [];
  for (const ev of events) {
    const cid = pickStr(ev, "cycle_id") ?? "unknown";
    let g = map.get(cid);
    if (!g) {
      g = {
        cycle_id: cid === "unknown" ? null : cid,
        vertical: pickStr(ev, "vertical"),
        started_at: null,
        completed_at: null,
        status: "running",
        events: [],
        candidates: new Map(),
        trades_placed: 0,
        picks_evaluated: 0,
        errors: null,
      };
      map.set(cid, g);
      order.push(cid);
    }
    g.events.push(ev);
    const t = pickStr(ev, "type");
    if (t === "cycle_start") {
      g.started_at = pickStr(ev, "started_at") ?? g.started_at;
      g.vertical = pickStr(ev, "vertical") ?? g.vertical;
    } else if (t === "cycle_complete") {
      g.completed_at = pickStr(ev, "completed_at") ?? g.completed_at;
      g.status = "complete";
      g.trades_placed = pickNum(ev, "trades_placed") ?? g.trades_placed;
      g.picks_evaluated = pickNum(ev, "picks_evaluated") ?? g.picks_evaluated;
      g.errors = pickStr(ev, "errors");
    } else if (t === "cycle_halted") {
      g.status = "halted";
      g.errors = pickStr(ev, "reason") ?? pickStr(ev, "error");
    } else {
      const candId =
        pickStr(ev, "candidate_id") ?? pickStr(ev, "market_id") ?? null;
      if (candId) {
        const arr = g.candidates.get(candId) ?? [];
        arr.push(ev);
        g.candidates.set(candId, arr);
      }
    }
  }
  return order.map((cid) => map.get(cid)!).reverse();
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function eventTone(t: string | null): string {
  if (!t) return "text-muted-foreground";
  if (t.includes("error") || t === "cycle_halted") return "text-destructive";
  if (t === "cycle_complete") return "text-success";
  if (t === "cycle_start") return "text-primary";
  if (t === "candidate_complete") return "text-warning";
  if (t === "gate") return "text-muted-foreground";
  return "text-foreground";
}

function GateRow({ ev }: { ev: Record<string, unknown> }) {
  const passed = ev["passed"] === true;
  const gate = pickStr(ev, "gate") ?? pickStr(ev, "name") ?? "gate";
  const reason = pickStr(ev, "reason") ?? "";
  // Per-vertical-cap (and other composite gates) attach a `sub_check`
  // field inside `details` that names which internal predicate fired
  // — e.g. `commodities_correlation` for the commodities cross-symbol
  // exposure projection.  Surface it inline so operators can see the
  // root cause without having to drill into the JSON payload.
  const details =
    ev["details"] && typeof ev["details"] === "object"
      ? (ev["details"] as Record<string, unknown>)
      : null;
  const subCheck = details ? pickStr(details, "sub_check") : null;
  const intendedSymbol = details ? pickStr(details, "intended_symbol") : null;
  return (
    <div className="flex items-center gap-2 text-[10px] uppercase">
      <span
        className={`w-1.5 h-1.5 rounded-full ${passed ? "bg-success" : "bg-destructive"}`}
      />
      <span className="text-muted-foreground tracking-wider">{gate}</span>
      {subCheck && (
        <span
          className={`tracking-wider ${passed ? "text-muted-foreground/70" : "text-destructive/80"}`}
          data-testid="gate-sub-check"
          title={`sub-check: ${subCheck}${intendedSymbol ? ` · symbol=${intendedSymbol}` : ""}`}
        >
          › {subCheck}
          {intendedSymbol ? ` (${intendedSymbol})` : ""}
        </span>
      )}
      {!passed && reason && (
        <span className="text-destructive truncate" title={reason}>
          · {reason}
        </span>
      )}
    </div>
  );
}

function fmtPct(v: number | null | undefined): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

// Pull the routed venues out of the candidate's terminal event. The
// orchestrator emits both a single `placement` (legacy single-venue) and
// `placements` / `venues` arrays once fan-out is in play (see
// `cycle.py` candidate_complete payload). The shared helper handles
// all three shapes.
function extractCandidateVenues(events: Record<string, unknown>[]): string[] {
  const last = events[events.length - 1];
  if (!last || pickStr(last, "type") !== "candidate_complete") return [];
  if (pickStr(last, "outcome") !== "placed") return [];
  return extractVenuesFromEvent(last);
}

function venueClass(raw: string): string {
  if (raw === "kalshi") return "border-primary/60 bg-primary/10 text-primary";
  if (raw === "polymarket") return "border-warning/60 bg-warning/10 text-warning";
  return "border-border bg-card/40 text-muted-foreground";
}

function VenueChips({ venues }: { venues: string[] }) {
  if (venues.length === 0) return null;
  // Single-venue placements get a single chip; fan-out shows the
  // primary venue + a "+N venue" overflow chip so the row stays tidy
  // when the orchestrator routes to both Kalshi and Polymarket.
  const primary = venues[0]!;
  const extra = venues.length - 1;
  return (
    <span
      className="inline-flex items-center gap-1"
      data-testid="candidate-venues"
      data-venue-count={venues.length}
    >
      <span
        className={`inline-flex items-center border px-1.5 py-px text-[9px] uppercase tracking-wider ${venueClass(primary)}`}
        data-testid="candidate-venue-primary"
      >
        {venueDisplayLabel(primary)}
      </span>
      {extra > 0 && (
        <span
          className="inline-flex items-center border border-border bg-card/40 px-1.5 py-px text-[9px] uppercase tracking-wider text-muted-foreground"
          data-testid="candidate-venue-overflow"
          title={`Also routed to: ${venues.slice(1).map(venueDisplayLabel).join(", ")}`}
        >
          +{extra} venue{extra === 1 ? "" : "s"}
        </span>
      )}
    </span>
  );
}

function CandidateBlock({
  candidateId,
  events,
}: {
  candidateId: string;
  events: Record<string, unknown>[];
}) {
  const last = events[events.length - 1];
  const final =
    pickStr(last, "type") === "candidate_complete"
      ? pickStr(last, "outcome") ?? pickStr(last, "status") ?? "complete"
      : "in-progress";
  const market = pickStr(events[0] ?? {}, "market_id") ?? candidateId;
  const venues = extractCandidateVenues(events);
  // The candidate_start event carries both probabilities so the dashboard
  // can show the live model number alongside the cached swarm consensus.
  const startEv =
    events.find((e) => pickStr(e, "type") === "candidate_start") ?? events[0] ?? {};
  const cachedProb = pickNum(startEv, "swarm_consensus");
  const liveProb = pickNum(startEv, "live_model_probability");
  const liveModel =
    pickStr(startEv, "live_model_name") ??
    pickStr(startEv, "live_model_version");
  const liveMissing = liveProb === null;
  // Spread/total tickers carry a margin_adjustment payload that records
  // the binary baseline, the regressor's own probability, the expected
  // margin/total, and the blend weight.  When present, surface the
  // split so operators can see when the regressor is moving the call.
  const adjRaw = startEv["margin_adjustment"];
  const adj =
    adjRaw && typeof adjRaw === "object" ? (adjRaw as Record<string, unknown>) : null;
  const baselineProb =
    pickNum(startEv, "baseline_probability") ?? (adj ? pickNum(adj, "baseline_prob") : null);
  const regressorProb = adj ? pickNum(adj, "regressor_prob") : null;
  const expected = adj ? pickNum(adj, "expected") : null;
  const blendWeight = adj ? pickNum(adj, "blend_weight") : null;
  const adjMarketType = adj ? pickStr(adj, "market_type") : null;
  const adjLine = adj ? pickNum(adj, "line") : null;
  const adjSide = adj ? pickStr(adj, "side") : null;
  // Flag rows where the regressor actually changed the YES/NO call the
  // operator sees — i.e. the blended live probability lands on the
  // opposite side of 0.5 from the binary baseline.  This is stricter
  // than "baseline vs regressor disagree" because a tiny regressor
  // edge with a small blend weight can leave the final call unchanged.
  const blendedForFlip = liveProb ?? (adj ? pickNum(adj, "blended") : null);
  const flipped =
    baselineProb !== null &&
    blendedForFlip !== null &&
    regressorProb !== null &&
    baselineProb >= 0.5 !== blendedForFlip >= 0.5;
  return (
    <div className="border border-border/60 bg-background/40 p-2 text-[10px]">
      <div className="flex items-center justify-between mb-1 gap-2">
        <MarketLabel
          ticker={market}
          className="font-bold text-foreground truncate max-w-[260px]"
        />
        <div className="flex items-center gap-2">
          <VenueChips venues={venues} />
          <span
            className={`uppercase tracking-wider ${final === "placed" ? "text-success" : final === "in-progress" ? "text-primary" : "text-muted-foreground"}`}
          >
            {final}
          </span>
        </div>
      </div>
      <div
        className="flex items-center gap-3 mb-1 text-[10px] uppercase tracking-wider"
        data-testid="candidate-probabilities"
      >
        <span className="text-muted-foreground">
          CACHED <span className="text-foreground font-bold">{fmtPct(cachedProb)}</span>
        </span>
        <span className={liveMissing ? "text-muted-foreground/60" : "text-muted-foreground"}>
          LIVE{" "}
          <span className={liveMissing ? "text-muted-foreground" : "text-primary font-bold"}>
            {fmtPct(liveProb)}
          </span>
          {liveModel && !liveMissing && (
            <span className="text-muted-foreground/60 normal-case">
              {" "}· {liveModel.split("/").pop()}
            </span>
          )}
        </span>
        {flipped && (
          <span
            className="px-1 py-px bg-warning/20 text-warning border border-warning/40"
            data-testid="margin-flip-badge"
            title="Regressor flipped the YES/NO call vs the binary baseline"
          >
            REG FLIP
          </span>
        )}
      </div>
      {adj && (
        <div
          className="flex flex-wrap items-center gap-x-3 gap-y-0.5 mb-1 text-[10px] uppercase tracking-wider text-muted-foreground"
          data-testid="margin-adjustment"
        >
          <span>
            BASE{" "}
            <span className="text-foreground font-bold">{fmtPct(baselineProb)}</span>
          </span>
          <span>
            REG{" "}
            <span className={`font-bold ${flipped ? "text-warning" : "text-foreground"}`}>
              {fmtPct(regressorProb)}
            </span>
          </span>
          {expected !== null && (
            <span>
              {adjMarketType === "Total" ? "EXP TOTAL" : "EXP MARGIN"}{" "}
              <span className="text-foreground font-bold normal-case">
                {expected.toFixed(1)}
              </span>
              {adjLine !== null && (
                <span className="text-muted-foreground/60 normal-case">
                  {" "}vs {adjLine.toFixed(1)}
                  {adjSide ? ` ${adjSide}` : ""}
                </span>
              )}
            </span>
          )}
          {blendWeight !== null && (
            <span>
              W <span className="text-foreground font-bold">{blendWeight.toFixed(2)}</span>
            </span>
          )}
        </div>
      )}
      <div className="space-y-0.5">
        {events.map((ev, i) => {
          const t = pickStr(ev, "type");
          if (t === "gate") return <GateRow key={i} ev={ev} />;
          return (
            <div key={i} className={`text-[10px] ${eventTone(t)}`}>
              <span className="uppercase tracking-wider">{t ?? "event"}</span>
              {pickStr(ev, "reason") && (
                <span className="text-muted-foreground"> · {pickStr(ev, "reason")}</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface OutOfBandFeedProps {
  title: string;
  events: Record<string, unknown>[];
  status: import("@/hooks/use-websocket").WSStatus;
  reconnectIn: number | null;
  closeCode: number | null;
  emptyText: string;
  formatLine: (ev: Record<string, unknown>) => import("react").ReactNode;
}

function OutOfBandFeed({
  title,
  events,
  status,
  reconnectIn,
  closeCode,
  emptyText,
  formatLine,
}: OutOfBandFeedProps) {
  const recent = events.slice(-30).reverse();
  return (
    <div className="border border-border bg-card flex flex-col h-[300px]">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex items-center justify-between shrink-0">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          {title}
        </h2>
        <WsStatusIndicator
          status={status}
          reconnectIn={reconnectIn}
          closeCode={closeCode}
          label="OPEN"
        />
      </div>
      <div className="overflow-auto grow text-[10px] font-mono">
        {recent.length === 0 ? (
          <div className="p-3 text-muted-foreground uppercase tracking-widest">
            {emptyText}
          </div>
        ) : (
          recent.map((ev, i) => {
            const t = pickStr(ev, "type");
            return (
              <div
                key={i}
                className={`px-3 py-1 border-b border-border/40 ${eventTone(t)}`}
              >
                {formatLine(ev)}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function CycleCard({ group }: { group: CycleGroup }) {
  const [open, setOpen] = useState(group.status === "running");
  const candidates = Array.from(group.candidates.entries());
  const statusColor =
    group.status === "complete"
      ? "text-success"
      : group.status === "halted"
        ? "text-destructive"
        : "text-primary animate-pulse";
  return (
    <div className="border border-border bg-card">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left p-3 flex items-center justify-between hover:bg-muted/20"
        data-testid="cycle-toggle"
      >
        <div className="flex items-center gap-3 text-xs">
          <span className={`uppercase tracking-widest ${statusColor}`}>
            {group.status}
          </span>
          <span className="text-foreground font-bold uppercase">
            {group.vertical ?? "—"}
          </span>
          <span className="text-muted-foreground">
            {fmtTime(group.started_at)}
            {group.completed_at ? ` → ${fmtTime(group.completed_at)}` : ""}
          </span>
        </div>
        <div className="text-[10px] uppercase tracking-widest text-muted-foreground flex gap-3">
          <span>EV {candidates.length}</span>
          <span>PLACED {group.trades_placed}</span>
          <span>{open ? "▴" : "▾"}</span>
        </div>
      </button>
      {open && (
        <div className="border-t border-border p-3 space-y-2">
          {group.errors && (
            <div className="text-[10px] uppercase text-destructive">
              ERR · {group.errors}
            </div>
          )}
          {candidates.length === 0 ? (
            <div className="text-[10px] text-muted-foreground uppercase tracking-widest">
              waiting for events…
            </div>
          ) : (
            candidates.map(([cid, evs]) => (
              <CandidateBlock key={cid} candidateId={cid} events={evs} />
            ))
          )}
        </div>
      )}
    </div>
  );
}

function fmtTimePart(s: string | null): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatSettlementLine(ev: Record<string, unknown>): import("react").ReactNode {
  const t = pickStr(ev, "type") ?? "event";
  const market = pickStr(ev, "market_id");
  const tradeId = pickStr(ev, "trade_id");
  const outcomeYes = ev["outcome_yes"];
  const pnl = pickNum(ev, "pnl");
  const ts = pickStr(ev, "ts") ?? pickStr(ev, "settled_at") ?? pickStr(ev, "completed_at");
  const head = `${fmtTimePart(ts)} · ${t.toUpperCase()}`;
  const tail: import("react").ReactNode[] = [];
  if (typeof tradeId === "string") tail.push(`trade=${tradeId.slice(0, 8)}`);
  if (typeof outcomeYes === "number") tail.push(`outcome=${outcomeYes ? "YES" : "NO"}`);
  if (typeof pnl === "number") tail.push(`pnl=$${pnl.toFixed(2)}`);
  return (
    <span>
      {head}
      {market ? (
        <>
          {" · "}
          <MarketLabel ticker={market} />
        </>
      ) : null}
      {tail.length > 0 ? ` · ${tail.join(" · ")}` : null}
    </span>
  );
}

const PIPELINE_CACHE_KEY = "pipeline.recent-events.v1";
const PIPELINE_CACHE_CYCLE_LIMIT = 50;
// Hard caps on the persisted blob so a busy day can't push it past the
// per-origin localStorage quota. Events are dropped oldest-cycle-first
// until both caps are satisfied. Tuned to leave plenty of headroom under
// the typical ~5MB browser quota even with other apps sharing storage.
const PIPELINE_CACHE_MAX_EVENTS = 2000;
const PIPELINE_CACHE_MAX_BYTES = 256 * 1024;

interface PipelineCacheDiagnostics {
  // Total number of events evicted from the persisted cache across the
  // session because of either the event-count or byte-size cap.
  droppedEvents: number;
  // Number of times the writer had to evict at least one cycle to fit
  // under the caps before persisting.
  evictions: number;
  // Bytes written on the most recent successful persist.
  lastBytes: number;
  // Events written on the most recent successful persist.
  lastEventCount: number;
  // Cycles written on the most recent successful persist.
  lastCycleCount: number;
  // Number of writes skipped entirely because the payload couldn't be
  // trimmed under the caps (or localStorage threw). Kept separate from
  // `evictions` so operators can spot total persistence failures.
  writeSkipped: number;
}

const pipelineCacheDiagnostics: PipelineCacheDiagnostics = {
  droppedEvents: 0,
  evictions: 0,
  lastBytes: 0,
  lastEventCount: 0,
  lastCycleCount: 0,
  writeSkipped: 0,
};

if (typeof window !== "undefined") {
  // Exposed on `window` so operators can inspect cache pressure from
  // the browser devtools without wiring up a UI surface for it.
  (window as unknown as Record<string, unknown>).__pipelineCacheDiagnostics =
    pipelineCacheDiagnostics;
}

function loadCachedEvents(): Record<string, unknown>[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(PIPELINE_CACHE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as Record<string, unknown>[]) : [];
  } catch {
    return [];
  }
}

// Persist the given cycles (newest-first) capped by event count and
// serialized byte size. Oldest cycles are evicted first; if a single
// newest cycle still exceeds the caps it is persisted alone so the
// page always retains some context.
function byteLength(s: string): number {
  if (typeof TextEncoder !== "undefined") {
    return new TextEncoder().encode(s).length;
  }
  // Fallback for environments without TextEncoder: count UTF-8 bytes
  // manually so the byte-cap stays correct on multibyte payloads.
  let n = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c < 0x80) n += 1;
    else if (c < 0x800) n += 2;
    else if (c >= 0xd800 && c <= 0xdbff) {
      n += 4;
      i += 1;
    } else n += 3;
  }
  return n;
}

function saveCachedCycles(cyclesNewestFirst: CycleGroup[]): void {
  if (typeof window === "undefined") return;
  // Retain the newest contiguous prefix that fits under the event cap.
  // Strictly oldest-first eviction — never skip a middle cycle to keep
  // older ones.
  const kept: CycleGroup[] = [];
  let totalEvents = 0;
  let droppedEvents = 0;
  let evicted = false;
  // Always seed with the newest cycle (if any). Even when that single
  // cycle blows the event cap on its own, we still want to retain it
  // and trim its events later — otherwise a high-volume cycle would
  // wipe out all persisted context.
  if (cyclesNewestFirst.length > 0) {
    const head = cyclesNewestFirst[0];
    kept.push(head);
    totalEvents += head.events.length;
  }
  for (let i = 1; i < cyclesNewestFirst.length; i++) {
    const g = cyclesNewestFirst[i];
    const evCount = g.events.length;
    if (totalEvents + evCount > PIPELINE_CACHE_MAX_EVENTS) {
      break;
    }
    kept.push(g);
    totalEvents += evCount;
  }
  // Tally everything past the kept prefix as dropped.
  for (let i = kept.length; i < cyclesNewestFirst.length; i++) {
    droppedEvents += cyclesNewestFirst[i].events.length;
    evicted = true;
  }

  // Replay events oldest-first so a future merge re-groups identically.
  const buildEvents = (groups: CycleGroup[]): Record<string, unknown>[] => {
    const out: Record<string, unknown>[] = [];
    for (let i = groups.length - 1; i >= 0; i--) out.push(...groups[i].events);
    return out;
  };

  let events = buildEvents(kept);
  let serialized = JSON.stringify(events);
  let bytes = byteLength(serialized);
  // If we're still over the byte cap, drop oldest cycles one at a time
  // until we fit.
  while (bytes > PIPELINE_CACHE_MAX_BYTES && kept.length > 1) {
    const removed = kept.pop();
    if (removed) {
      droppedEvents += removed.events.length;
      evicted = true;
    }
    events = buildEvents(kept);
    serialized = JSON.stringify(events);
    bytes = byteLength(serialized);
  }

  // Edge case: a single newest cycle is itself larger than the caps.
  // Trim its oldest events until both event and byte caps are satisfied
  // so the persisted blob is always bounded, even if it costs us some
  // intra-cycle history.
  if (
    kept.length === 1 &&
    (events.length > PIPELINE_CACHE_MAX_EVENTS ||
      bytes > PIPELINE_CACHE_MAX_BYTES)
  ) {
    if (events.length > PIPELINE_CACHE_MAX_EVENTS) {
      const drop = events.length - PIPELINE_CACHE_MAX_EVENTS;
      droppedEvents += drop;
      evicted = true;
      events = events.slice(drop);
      serialized = JSON.stringify(events);
      bytes = byteLength(serialized);
    }
    while (bytes > PIPELINE_CACHE_MAX_BYTES && events.length > 1) {
      events = events.slice(1);
      droppedEvents += 1;
      evicted = true;
      serialized = JSON.stringify(events);
      bytes = byteLength(serialized);
    }
  }

  // If even a single event is still over the byte cap, skip the write
  // so we never push an oversized payload into localStorage.
  const writeSkipped =
    bytes > PIPELINE_CACHE_MAX_BYTES || events.length > PIPELINE_CACHE_MAX_EVENTS;

  try {
    if (writeSkipped) {
      pipelineCacheDiagnostics.droppedEvents += droppedEvents + events.length;
      pipelineCacheDiagnostics.evictions += 1;
      pipelineCacheDiagnostics.writeSkipped += 1;
      return;
    }
    window.localStorage.setItem(PIPELINE_CACHE_KEY, serialized);
    pipelineCacheDiagnostics.droppedEvents += droppedEvents;
    if (evicted) pipelineCacheDiagnostics.evictions += 1;
    pipelineCacheDiagnostics.lastBytes = bytes;
    pipelineCacheDiagnostics.lastEventCount = events.length;
    pipelineCacheDiagnostics.lastCycleCount = kept.length;
  } catch {
    // quota exceeded or storage disabled — non-fatal.
    pipelineCacheDiagnostics.writeSkipped += 1;
  }
}

export default function PipelinePage() {
  const { getToken, isSignedIn } = useAuth();
  // Pull connection state from the shared shell-level pipeline socket
  // and layer a page-local 500-frame buffer on top via subscription —
  // never open a second `/api/ws/pipeline` connection from this page.
  const sharedPipeline = usePipelineSocket();
  const pipelineBuffer = usePipelineEventBuffer(500);
  const ws = useMemo(
    () => ({
      status: sharedPipeline.status,
      reconnectIn: sharedPipeline.reconnectIn,
      closeCode: sharedPipeline.closeCode,
      reconnectNow: sharedPipeline.reconnectNow,
      events: pipelineBuffer.events,
      clear: pipelineBuffer.clear,
      rotationCount: sharedPipeline.rotationCount,
      lastRotationAt: sharedPipeline.lastRotationAt,
      lastRotationError: sharedPipeline.lastRotationError,
    }),
    [
      sharedPipeline.status,
      sharedPipeline.reconnectIn,
      sharedPipeline.closeCode,
      sharedPipeline.reconnectNow,
      pipelineBuffer.events,
      pipelineBuffer.clear,
      sharedPipeline.rotationCount,
      sharedPipeline.lastRotationAt,
      sharedPipeline.lastRotationError,
    ],
  );
  const swarm = useSwarmTranscript();
  const settlement = useWebSocket({ path: "/api/ws/settlement", bufferSize: 200 });

  // Persisted cache of the last 50 cycles' events, hydrated synchronously
  // from localStorage so a refresh keeps recent context on screen even
  // before the WS reconnects or the REST snapshot returns.
  const [cachedEvents, setCachedEvents] = useState<Record<string, unknown>[]>(
    () => loadCachedEvents(),
  );

  // Hydrate from REST so reloading the page mid-cycle still shows the
  // last few cycles instead of an empty buffer until the next event.
  const [snapshot, setSnapshot] = useState<Record<string, unknown>[]>([]);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  useEffect(() => {
    if (!isSignedIn) return;
    let cancelled = false;
    apiFetch<{ events: Record<string, unknown>[] }>(
      "/api/orchestrator/recent-events?cycles=5",
      { getToken },
    )
      .then((res) => {
        if (cancelled) return;
        setSnapshot(Array.isArray(res?.events) ? res.events : []);
      })
      .catch((e) => {
        if (cancelled) return;
        setSnapshotError(String(e?.message ?? e));
      });
    return () => {
      cancelled = true;
    };
  }, [isSignedIn, getToken]);

  // Merge cached + snapshot + live WS events, dedup'd on the server-assigned
  // _seq. Snapshot/cache events that the WS later replays are filtered out
  // so the same cycle card never doubles up.
  const mergedEvents = useMemo(() => {
    const seenSeq = new Set<number>();
    const out: Record<string, unknown>[] = [];
    const push = (ev: Record<string, unknown>) => {
      const seqVal = ev["_seq"];
      const seq = typeof seqVal === "number" ? seqVal : null;
      if (seq !== null) {
        if (seenSeq.has(seq)) return;
        seenSeq.add(seq);
      }
      out.push(ev);
    };
    for (const ev of cachedEvents) push(ev);
    for (const ev of snapshot) push(ev);
    for (const ev of ws.events) push(ev);
    return out;
  }, [cachedEvents, snapshot, ws.events]);

  const groups = useMemo(() => groupByCycle(mergedEvents), [mergedEvents]);

  // Persist the most recent cycles back to localStorage so the next
  // page load has immediate context. Capped at PIPELINE_CACHE_CYCLE_LIMIT
  // cycles, PIPELINE_CACHE_MAX_EVENTS total events, and
  // PIPELINE_CACHE_MAX_BYTES serialized bytes (oldest cycles evicted
  // first) to bound storage growth on busy days.
  useEffect(() => {
    if (groups.length === 0) return;
    const top = groups.slice(0, PIPELINE_CACHE_CYCLE_LIMIT);
    saveCachedCycles(top);
  }, [groups]);
  const titleResolver = useMarketTitleResolver();
  // Tie the swarm transcript to the most recent in-flight candidate by
  // surfacing its market_id; the transcript outlines that debate's card
  // in the primary colour so operators can correlate at a glance.
  const visibleGroups = useMemo(
    () => groups.slice(0, PIPELINE_CACHE_CYCLE_LIMIT),
    [groups],
  );

  // Auto-scroll-to-latest: when a new cycle appears at the top of the
  // list, jump the cycles section to the top so operators can keep an
  // eye on the freshest activity. The pause toggle lets them stop the
  // jump while inspecting an older cycle without losing their place.
  const [autoScroll, setAutoScroll] = useState(true);
  const cyclesTopRef = useRef<HTMLDivElement | null>(null);
  const latestCycleId = visibleGroups[0]?.cycle_id ?? null;
  const lastScrolledCycleRef = useRef<string | null>(null);
  useEffect(() => {
    if (!autoScroll) return;
    if (!latestCycleId) return;
    if (lastScrolledCycleRef.current === latestCycleId) return;
    lastScrolledCycleRef.current = latestCycleId;
    const el = cyclesTopRef.current;
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [autoScroll, latestCycleId]);

  // While auto-scroll is paused, remember which cycle was newest at
  // pause-time so we can show operators how many cycles have arrived
  // since. The anchor also resets whenever the operator scrolls the
  // latest cycle back into view or explicitly jumps to it.
  const [pausedAnchorCycleId, setPausedAnchorCycleId] = useState<string | null>(
    null,
  );
  const [cyclesTopVisible, setCyclesTopVisible] = useState(true);
  useEffect(() => {
    const el = cyclesTopRef.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const obs = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          setCyclesTopVisible(entry.isIntersecting);
        }
      },
      { threshold: 0 },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);
  // Reset the "new since pause" counter when the latest cycle is back
  // on screen — operators are caught up even if they haven't resumed.
  useEffect(() => {
    if (!autoScroll && cyclesTopVisible) {
      setPausedAnchorCycleId(latestCycleId);
    }
  }, [autoScroll, cyclesTopVisible, latestCycleId]);

  const newCyclesSincePause = useMemo(() => {
    if (autoScroll) return 0;
    if (!pausedAnchorCycleId) return 0;
    const idx = visibleGroups.findIndex(
      (g) => g.cycle_id === pausedAnchorCycleId,
    );
    if (idx < 0) return visibleGroups.length;
    return idx;
  }, [autoScroll, pausedAnchorCycleId, visibleGroups]);

  const toggleAutoScroll = useCallback(() => {
    setAutoScroll((prev) => {
      const next = !prev;
      if (!next) {
        setPausedAnchorCycleId(latestCycleId);
      } else {
        setPausedAnchorCycleId(null);
      }
      return next;
    });
  }, [latestCycleId]);

  const jumpToLatest = useCallback(() => {
    const el = cyclesTopRef.current;
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    setPausedAnchorCycleId(latestCycleId);
  }, [latestCycleId]);

  const showJumpToLatest = !autoScroll && !cyclesTopVisible;

  const liveMarketId = useMemo<string | null>(() => {
    for (const g of groups) {
      if (g.status !== "running") continue;
      // Newest candidate within the running cycle wins.
      const candIds = Array.from(g.candidates.keys());
      for (let i = candIds.length - 1; i >= 0; i--) {
        const evs = g.candidates.get(candIds[i]) ?? [];
        const last = evs[evs.length - 1];
        if (pickStr(last ?? {}, "type") === "candidate_complete") continue;
        return pickStr(evs[0] ?? {}, "market_id") ?? candIds[i];
      }
    }
    return null;
  }, [groups]);
  return (
    <MarketTitleContext.Provider value={titleResolver}>
    <div className="space-y-5 font-mono" data-testid="page-pipeline">
      <header className="flex flex-col gap-2 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
            ORCHESTRATOR
          </div>
          <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
            Pipeline monitor
          </h1>
          <p className="mt-1 text-xs text-muted-foreground">
            Live cycle stream · gates · candidate decisions.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          <WsStatusIndicator
            status={ws.status}
            reconnectIn={ws.reconnectIn}
            closeCode={ws.closeCode}
            label="WS_OPEN"
            rotationCount={ws.rotationCount}
            lastRotationAt={ws.lastRotationAt}
            lastRotationError={ws.lastRotationError}
          />
        </div>
      </header>

      <TradeTrainTicker />

      <PipelineFlowchart />

      <BackfillPanel />

      <div className="flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground">
        <span>
          BUFFERED_EVENTS :: {mergedEvents.length}
          {snapshot.length > 0 && (
            <span className="text-foreground/60"> · {snapshot.length} HYDRATED</span>
          )}
          {snapshotError && (
            <span className="text-destructive" title={snapshotError}>
              {" "}
              · HYDRATE_FAILED
            </span>
          )}
        </span>
        <div className="flex gap-2">
          {showJumpToLatest && (
            <button
              type="button"
              onClick={jumpToLatest}
              className="border border-primary text-primary px-2 py-1 hover:bg-primary/10 flex items-center gap-2"
              data-testid="button-jump-to-latest"
              title="Scroll to the most recent cycle"
            >
              <span>↑ Jump to latest</span>
              {newCyclesSincePause > 0 && (
                <span
                  className="bg-primary text-primary-foreground px-1.5 py-0.5 text-[10px] font-bold rounded-sm"
                  data-testid="badge-new-cycles"
                >
                  +{newCyclesSincePause}
                </span>
              )}
            </button>
          )}
          <button
            type="button"
            onClick={toggleAutoScroll}
            className={`border px-2 py-1 ${
              autoScroll
                ? "border-primary text-primary"
                : "border-border text-muted-foreground hover:text-primary hover:border-primary"
            }`}
            data-testid="button-autoscroll"
            aria-pressed={autoScroll}
            title={
              autoScroll
                ? "Auto-scroll is on — click to pause"
                : "Auto-scroll is paused — click to resume"
            }
          >
            {autoScroll ? "▮▮ Pause auto-scroll" : "▶ Resume auto-scroll"}
          </button>
          <button
            type="button"
            onClick={ws.reconnectNow}
            className="border border-border px-2 py-1 hover:text-primary hover:border-primary"
            data-testid="button-reconnect"
          >
            Reconnect
          </button>
          <button
            type="button"
            onClick={() => {
              setSnapshot([]);
              setCachedEvents([]);
              saveCachedCycles([]);
              ws.clear();
            }}
            className="border border-border px-2 py-1 hover:text-primary hover:border-primary"
            data-testid="button-clear"
          >
            Clear
          </button>
        </div>
      </div>

      <main className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <section className="xl:col-span-2 space-y-3">
          <div ref={cyclesTopRef} aria-hidden="true" />
          {visibleGroups.length === 0 ? (
            <div className="border border-border bg-card p-8 text-center text-muted-foreground text-xs uppercase tracking-widest">
              {ws.status === "open"
                ? "Waiting for the next cycle event…"
                : "Stream not connected."}
            </div>
          ) : (
            visibleGroups.map((g, i) => (
              <CycleCard key={`${g.cycle_id ?? "x"}-${i}`} group={g} />
            ))
          )}
        </section>
        <aside className="space-y-3">
          <SwarmTranscript
            store={swarm.store}
            status={swarm.ws.status}
            reconnectIn={swarm.ws.reconnectIn}
            closeCode={swarm.ws.closeCode}
            highlightMarketId={liveMarketId}
          />
          <OutOfBandFeed
            title="SETTLEMENT_FILLS"
            events={settlement.events}
            status={settlement.status}
            reconnectIn={settlement.reconnectIn}
            closeCode={settlement.closeCode}
            emptyText="No settlement events yet…"
            formatLine={formatSettlementLine}
          />
          <RecentRetrains />
        </aside>
      </main>

      <ConsoleTail
        events={ws.events}
        status={ws.status}
        defaultOpen={false}
        testId="card-pipeline-console"
      />
    </div>
    </MarketTitleContext.Provider>
  );
}
