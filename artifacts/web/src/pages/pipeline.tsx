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
  return (
    <div className="flex items-center gap-2 text-[10px] uppercase">
      <span
        className={`w-1.5 h-1.5 rounded-full ${passed ? "bg-success" : "bg-destructive"}`}
      />
      <span className="text-muted-foreground tracking-wider">{gate}</span>
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
  return (
    <div className="border border-border/60 bg-background/40 p-2 text-[10px]">
      <div className="flex items-center justify-between mb-1">
        <MarketLabel
          ticker={market}
          className="font-bold text-foreground truncate max-w-[260px]"
        />
        <span
          className={`uppercase tracking-wider ${final === "placed" ? "text-success" : final === "in-progress" ? "text-primary" : "text-muted-foreground"}`}
        >
          {final}
        </span>
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
      </div>
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

function saveCachedEvents(events: Record<string, unknown>[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(PIPELINE_CACHE_KEY, JSON.stringify(events));
  } catch {
    // quota exceeded or storage disabled — non-fatal.
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
    }),
    [
      sharedPipeline.status,
      sharedPipeline.reconnectIn,
      sharedPipeline.closeCode,
      sharedPipeline.reconnectNow,
      pipelineBuffer.events,
      pipelineBuffer.clear,
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
  // cycles (newest first in `groups`) to bound storage growth.
  useEffect(() => {
    if (groups.length === 0) return;
    const top = groups.slice(0, PIPELINE_CACHE_CYCLE_LIMIT);
    const events: Record<string, unknown>[] = [];
    // Replay events in their original arrival order (oldest cycle first,
    // and within a cycle in event order) so a future merge re-groups
    // them identically.
    for (let i = top.length - 1; i >= 0; i--) {
      events.push(...top[i].events);
    }
    saveCachedEvents(events);
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
          />
        </div>
      </header>

      <PipelineFlowchart />

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
          <button
            type="button"
            onClick={() => setAutoScroll((v) => !v)}
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
              saveCachedEvents([]);
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
