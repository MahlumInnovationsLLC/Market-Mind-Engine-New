import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useWebSocket, type WSStatus } from "@/hooks/use-websocket";
import WsStatusIndicator from "@/components/ws-status-indicator";

/**
 * Live transcript of /api/ws/llm-logs.
 *
 * Each `debate_id` produces one card with one accumulating block per
 * persona (six analysts + moderator + gatekeeper).  Token deltas land in
 * a small in-memory store keyed by `${debate_id}::${persona}`; the
 * persona text is rendered through a ref so React never re-renders the
 * whole transcript on every chunk — the persona's <pre> just gets its
 * `textContent` reassigned.  Verdicts and gatekeeper outcomes drive a
 * coarse re-render of the parent card via a per-debate version counter.
 */

type PersonaState = {
  display: string | null;
  provider: string | null;
  model: string | null;
  text: string;
  status: "streaming" | "done" | "errored";
  verdict: {
    probability: number | null;
    confidence: number | null;
    lean: string | null;
    reasoning: string | null;
    latency_ms: number | null;
    error: string | null;
  } | null;
  fallback: {
    fromProvider: string | null;
    fromModel: string | null;
    toProvider: string | null;
    toModel: string | null;
    reason: string | null;
  } | null;
};

type DebateState = {
  id: string;
  startedAt: string | null;
  completedAt: string | null;
  gameId: string | null;
  matchup: string | null;
  league: string | null;
  marketId: string | null;
  marketType: string | null;
  side: string | null;
  currentPrice: number | null;
  aggressionMode: string | null;
  status: "running" | "complete";
  recommendedAction: string | null;
  approved: boolean | null;
  consensus: number | null;
  agreement: number | null;
  costEstimate: number | null;
  latencyMs: number | null;
  vetoReason: string | null;
  personas: Map<string, PersonaState>;
  personaOrder: string[];
  version: number; // bumped on any non-text change
};

type Listener = () => void;

class SwarmTranscriptStore {
  debates = new Map<string, DebateState>();
  order: string[] = [];
  private indexListeners = new Set<Listener>();
  private debateListeners = new Map<string, Set<Listener>>();
  private personaTextListeners = new Map<string, Set<(text: string) => void>>();

  private notifyIndex() {
    for (const cb of this.indexListeners) cb();
  }
  private notifyDebate(id: string) {
    const set = this.debateListeners.get(id);
    if (!set) return;
    for (const cb of set) cb();
  }
  private notifyPersonaText(debateId: string, persona: string, text: string) {
    const key = `${debateId}::${persona}`;
    const set = this.personaTextListeners.get(key);
    if (!set) return;
    for (const cb of set) cb(text);
  }

  subscribeIndex = (cb: Listener) => {
    this.indexListeners.add(cb);
    return () => {
      this.indexListeners.delete(cb);
    };
  };

  subscribeDebate = (id: string, cb: Listener) => {
    let set = this.debateListeners.get(id);
    if (!set) {
      set = new Set();
      this.debateListeners.set(id, set);
    }
    set.add(cb);
    return () => {
      const s = this.debateListeners.get(id);
      if (!s) return;
      s.delete(cb);
      if (s.size === 0) this.debateListeners.delete(id);
    };
  };

  subscribePersonaText = (
    debateId: string,
    persona: string,
    cb: (text: string) => void,
  ) => {
    const key = `${debateId}::${persona}`;
    let set = this.personaTextListeners.get(key);
    if (!set) {
      set = new Set();
      this.personaTextListeners.set(key, set);
    }
    set.add(cb);
    return () => {
      const s = this.personaTextListeners.get(key);
      if (!s) return;
      s.delete(cb);
      if (s.size === 0) this.personaTextListeners.delete(key);
    };
  };

  getOrder = () => this.order;
  getDebate = (id: string) => this.debates.get(id);

  private ensureDebate(id: string): DebateState {
    let d = this.debates.get(id);
    if (!d) {
      d = {
        id,
        startedAt: null,
        completedAt: null,
        gameId: null,
        matchup: null,
        league: null,
        marketId: null,
        marketType: null,
        side: null,
        currentPrice: null,
        aggressionMode: null,
        status: "running",
        recommendedAction: null,
        approved: null,
        consensus: null,
        agreement: null,
        costEstimate: null,
        latencyMs: null,
        vetoReason: null,
        personas: new Map(),
        personaOrder: [],
        version: 0,
      };
      this.debates.set(id, d);
      // Newest first so the latest debate sits at the top.
      const MAX_DEBATES = 25;
      const next = [id, ...this.order];
      if (next.length > MAX_DEBATES) {
        // Prune the underlying map too so long sessions don't grow
        // unbounded — listeners for evicted debates are likewise dropped
        // so cleanup is not deferred until the next listener teardown.
        const evicted = next.slice(MAX_DEBATES);
        for (const evictId of evicted) {
          this.debates.delete(evictId);
          this.debateListeners.delete(evictId);
          for (const k of Array.from(this.personaTextListeners.keys())) {
            if (k.startsWith(`${evictId}::`)) this.personaTextListeners.delete(k);
          }
        }
      }
      this.order = next.slice(0, MAX_DEBATES);
      this.notifyIndex();
    }
    return d;
  }

  private ensurePersona(d: DebateState, persona: string): PersonaState {
    let p = d.personas.get(persona);
    if (!p) {
      p = {
        display: null,
        provider: null,
        model: null,
        text: "",
        status: "streaming",
        verdict: null,
        fallback: null,
      };
      d.personas.set(persona, p);
      d.personaOrder.push(persona);
    }
    return p;
  }

  ingest = (raw: Record<string, unknown>) => {
    const debateId = typeof raw["debate_id"] === "string" ? (raw["debate_id"] as string) : null;
    if (!debateId) return; // pre-task events without a debate_id can be ignored here
    const type = typeof raw["type"] === "string" ? (raw["type"] as string) : "";
    const d = this.ensureDebate(debateId);

    if (type === "debate_start") {
      d.startedAt = (raw["ts"] as string) ?? d.startedAt;
      d.gameId = (raw["game_id"] as string) ?? d.gameId;
      d.matchup = (raw["matchup"] as string) ?? d.matchup;
      d.league = (raw["league"] as string) ?? d.league;
      d.marketId = (raw["market_id"] as string) ?? (raw["kalshi_market_id"] as string) ?? d.marketId;
      d.marketType = (raw["market_type"] as string) ?? d.marketType;
      d.side = (raw["side"] as string) ?? d.side;
      d.currentPrice = typeof raw["current_price"] === "number" ? (raw["current_price"] as number) : d.currentPrice;
      d.aggressionMode = (raw["aggression_mode"] as string) ?? d.aggressionMode;
      const personas = raw["personas"];
      if (Array.isArray(personas)) {
        for (const pid of personas) {
          if (typeof pid === "string") this.ensurePersona(d, pid);
        }
      }
      d.version += 1;
      this.notifyDebate(debateId);
      return;
    }

    if (type === "persona_start") {
      const persona = (raw["persona"] as string) ?? "?";
      const p = this.ensurePersona(d, persona);
      p.display = (raw["display"] as string) ?? p.display;
      p.provider = (raw["provider"] as string) ?? p.provider;
      p.model = (raw["model"] as string) ?? p.model;
      p.status = "streaming";
      d.version += 1;
      this.notifyDebate(debateId);
      return;
    }

    if (type === "persona_delta") {
      const persona = (raw["persona"] as string) ?? "?";
      const text = (raw["text"] as string) ?? "";
      if (!text) return;
      const p = this.ensurePersona(d, persona);
      p.text = p.text + text;
      // Surgical: only notify the persona's <pre>, not the parent.
      this.notifyPersonaText(debateId, persona, p.text);
      return;
    }

    if (type === "persona_fallback") {
      const persona = (raw["persona"] as string) ?? "?";
      const p = this.ensurePersona(d, persona);
      const fromObj = (raw["from"] as Record<string, unknown>) ?? {};
      const toObj = (raw["to"] as Record<string, unknown>) ?? {};
      p.fallback = {
        fromProvider: typeof fromObj["provider"] === "string" ? (fromObj["provider"] as string) : null,
        fromModel: typeof fromObj["model"] === "string" ? (fromObj["model"] as string) : null,
        toProvider: typeof toObj["provider"] === "string" ? (toObj["provider"] as string) : null,
        toModel: typeof toObj["model"] === "string" ? (toObj["model"] as string) : null,
        reason: typeof raw["reason"] === "string" ? (raw["reason"] as string) : null,
      };
      // Surface the substitute model in the persona header right away.
      if (p.fallback.toProvider) p.provider = p.fallback.toProvider;
      if (p.fallback.toModel) p.model = p.fallback.toModel;
      d.version += 1;
      this.notifyDebate(debateId);
      return;
    }

    if (type === "persona_done") {
      const persona = (raw["persona"] as string) ?? "?";
      const p = this.ensurePersona(d, persona);
      const verdict = raw["verdict"] as Record<string, unknown> | undefined;
      p.verdict = {
        probability: typeof verdict?.["probability"] === "number" ? (verdict["probability"] as number) : null,
        confidence: typeof verdict?.["confidence"] === "number" ? (verdict["confidence"] as number) : null,
        lean: typeof verdict?.["lean"] === "string" ? (verdict["lean"] as string) : null,
        reasoning: typeof verdict?.["reasoning"] === "string" ? (verdict["reasoning"] as string) : null,
        latency_ms: typeof verdict?.["latency_ms"] === "number" ? (verdict["latency_ms"] as number) : null,
        error: typeof verdict?.["error"] === "string" ? (verdict["error"] as string) : null,
      };
      p.status = p.verdict.error ? "errored" : "done";
      d.version += 1;
      this.notifyDebate(debateId);
      return;
    }

    if (type === "moderator_done") {
      d.consensus = typeof raw["consensus_probability"] === "number" ? (raw["consensus_probability"] as number) : d.consensus;
      d.agreement = typeof raw["agreement_rate"] === "number" ? (raw["agreement_rate"] as number) : d.agreement;
      d.recommendedAction = (raw["recommended_action"] as string) ?? d.recommendedAction;
      d.version += 1;
      this.notifyDebate(debateId);
      return;
    }

    if (type === "gatekeeper_done") {
      d.approved = typeof raw["approved"] === "boolean" ? (raw["approved"] as boolean) : d.approved;
      d.recommendedAction = (raw["recommended_action"] as string) ?? d.recommendedAction;
      d.vetoReason = (raw["veto_reason"] as string) ?? d.vetoReason;
      d.version += 1;
      this.notifyDebate(debateId);
      return;
    }

    if (type === "debate_complete") {
      d.status = "complete";
      d.completedAt = (raw["ts"] as string) ?? d.completedAt;
      d.recommendedAction = (raw["recommended_action"] as string) ?? d.recommendedAction;
      d.approved = typeof raw["approved"] === "boolean" ? (raw["approved"] as boolean) : d.approved;
      d.consensus = typeof raw["consensus_probability"] === "number" ? (raw["consensus_probability"] as number) : d.consensus;
      d.agreement = typeof raw["agreement_rate"] === "number" ? (raw["agreement_rate"] as number) : d.agreement;
      d.costEstimate = typeof raw["cost_estimate"] === "number" ? (raw["cost_estimate"] as number) : d.costEstimate;
      d.latencyMs = typeof raw["latency_ms"] === "number" ? (raw["latency_ms"] as number) : d.latencyMs;
      d.version += 1;
      this.notifyDebate(debateId);
      return;
    }
  };

  clear = () => {
    this.debates.clear();
    this.order = [];
    this.notifyIndex();
  };
}

function fmtTimePart(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function actionTone(action: string | null, approved: boolean | null): string {
  if (approved === false) return "text-destructive";
  if (action === "BUY_YES" || action === "BUY_NO") return "text-success";
  if (action === "PASS") return "text-muted-foreground";
  return "text-primary";
}

function leanTone(lean: string | null): string {
  if (lean === "YES") return "text-success";
  if (lean === "NO") return "text-destructive";
  return "text-muted-foreground";
}

function PersonaText({
  store,
  debateId,
  persona,
}: {
  store: SwarmTranscriptStore;
  debateId: string;
  persona: string;
}) {
  const ref = useRef<HTMLPreElement>(null);
  useEffect(() => {
    // Hydrate with whatever's already buffered (the debate may have
    // been streaming before this card mounted).
    const d = store.getDebate(debateId);
    const p = d?.personas.get(persona);
    if (ref.current) ref.current.textContent = p?.text ?? "";
    return store.subscribePersonaText(debateId, persona, (text) => {
      const el = ref.current;
      if (!el) return;
      el.textContent = text;
      // Auto-scroll to keep the latest tokens in view.
      el.scrollTop = el.scrollHeight;
    });
  }, [store, debateId, persona]);
  return (
    <pre
      ref={ref}
      className="m-0 max-h-32 overflow-auto whitespace-pre-wrap text-[10px] leading-snug text-foreground/80 font-mono"
      data-testid={`persona-text-${persona}`}
    />
  );
}

function PersonaBlock({
  store,
  debateId,
  persona,
}: {
  store: SwarmTranscriptStore;
  debateId: string;
  persona: string;
}) {
  // Subscribe to coarse changes for this debate, then read the slice.
  // We don't snapshot the whole persona — text streams via PersonaText.
  const subscribe = useCallback((cb: Listener) => store.subscribeDebate(debateId, cb), [store, debateId]);
  const getSnapshot = useCallback(() => {
    const d = store.getDebate(debateId);
    const p = d?.personas.get(persona);
    if (!p) return "0|||";
    return `${d?.version ?? 0}|${p.status}|${p.verdict?.lean ?? ""}|${p.verdict?.probability ?? ""}|${p.display ?? ""}|${p.model ?? ""}|${p.fallback ? "fb" : ""}`;
  }, [store, debateId, persona]);
  useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const d = store.getDebate(debateId);
  const p = d?.personas.get(persona);
  const display = p?.display ?? persona;
  const lean = p?.verdict?.lean ?? null;
  const prob = p?.verdict?.probability;
  const status = p?.status ?? "streaming";

  return (
    <div className="border border-border/60 bg-background/40 p-2" data-testid={`persona-block-${persona}`}>
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wider mb-1">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={`w-1.5 h-1.5 rounded-full shrink-0 ${
              status === "streaming"
                ? "bg-primary animate-pulse"
                : status === "errored"
                  ? "bg-destructive"
                  : "bg-success"
            }`}
          />
          <span className="font-bold text-foreground truncate" title={persona}>
            {display}
          </span>
          {p?.model && (
            <span className="text-muted-foreground truncate hidden sm:inline" title={p.model}>
              · {p.model}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {p?.fallback && (
            <span
              className="border border-warning/60 text-warning px-1 py-px text-[9px] tracking-widest"
              data-testid={`persona-fallback-badge-${persona}`}
              title={`Primary ${p.fallback.fromProvider ?? "?"}/${p.fallback.fromModel ?? "?"} failed; answered by ${p.fallback.toProvider ?? "?"}/${p.fallback.toModel ?? "?"}.${p.fallback.reason ? ` Reason: ${p.fallback.reason}` : ""}`}
            >
              FALLBACK
            </span>
          )}
          {lean && <span className={leanTone(lean)}>{lean}</span>}
          {typeof prob === "number" && (
            <span className="text-muted-foreground">{(prob * 100).toFixed(0)}%</span>
          )}
        </div>
      </div>
      <PersonaText store={store} debateId={debateId} persona={persona} />
      {p?.verdict?.error && (
        <div className="mt-1 text-[10px] text-destructive truncate" title={p.verdict.error}>
          ERR · {p.verdict.error}
        </div>
      )}
    </div>
  );
}

function DebateCard({
  store,
  debateId,
  highlightMarketId,
}: {
  store: SwarmTranscriptStore;
  debateId: string;
  highlightMarketId: string | null;
}) {
  const subscribe = useCallback((cb: Listener) => store.subscribeDebate(debateId, cb), [store, debateId]);
  const getSnapshot = useCallback(() => {
    const d = store.getDebate(debateId);
    if (!d) return "0";
    return `${d.version}|${d.personaOrder.join(",")}|${d.status}`;
  }, [store, debateId]);
  useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const d = store.getDebate(debateId);
  const [open, setOpen] = useState(true);
  if (!d) return null;

  const tone = actionTone(d.recommendedAction, d.approved);
  const linkedToCandidate = !!(highlightMarketId && d.marketId && d.marketId === highlightMarketId);
  const headerLabel = d.matchup ?? d.gameId ?? d.id.slice(0, 8);
  return (
    <div
      className={`border bg-card ${linkedToCandidate ? "border-primary" : "border-border"}`}
      data-testid={`debate-card-${d.id}`}
    >
      <button
        type="button"
        className="w-full text-left p-2 px-3 flex items-center justify-between hover:bg-muted/20"
        onClick={() => setOpen((v) => !v)}
        data-testid="debate-toggle"
      >
        <div className="flex items-center gap-2 min-w-0 text-[10px] uppercase tracking-wider">
          <span
            className={`w-1.5 h-1.5 rounded-full shrink-0 ${
              d.status === "running" ? "bg-primary animate-pulse" : "bg-success"
            }`}
          />
          <span className="font-bold text-foreground truncate max-w-[220px]" title={headerLabel}>
            {headerLabel}
          </span>
          {d.league && <span className="text-muted-foreground">{d.league}</span>}
          <span className="text-muted-foreground">{fmtTimePart(d.startedAt)}</span>
        </div>
        <div className="flex items-center gap-2 shrink-0 text-[10px] uppercase tracking-wider">
          {typeof d.consensus === "number" && (
            <span className="text-muted-foreground">cons {(d.consensus * 100).toFixed(0)}%</span>
          )}
          <span className={tone}>{d.recommendedAction ?? (d.status === "running" ? "..." : "—")}</span>
          <span className="text-muted-foreground">{open ? "▴" : "▾"}</span>
        </div>
      </button>
      {open && (
        <div className="border-t border-border p-2 space-y-2">
          {d.vetoReason && (
            <div className="text-[10px] uppercase text-destructive">
              GATEKEEPER VETO · {d.vetoReason}
            </div>
          )}
          {d.personaOrder.length === 0 ? (
            <div className="text-[10px] text-muted-foreground uppercase tracking-widest">
              waiting for first token…
            </div>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
              {d.personaOrder.map((pid) => (
                <PersonaBlock key={pid} store={store} debateId={d.id} persona={pid} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface SwarmTranscriptProps {
  status: WSStatus;
  reconnectIn: number | null;
  closeCode: number | null;
  highlightMarketId?: string | null;
  // The store is kept outside so the parent can wire it to the WS.
  store: SwarmTranscriptStore;
}

function SwarmTranscript({ status, reconnectIn, closeCode, highlightMarketId, store }: SwarmTranscriptProps) {
  const subscribe = useCallback((cb: Listener) => store.subscribeIndex(cb), [store]);
  const getSnapshot = useCallback(() => store.getOrder().join(","), [store]);
  useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const order = store.getOrder();

  return (
    <div className="border border-border bg-card flex flex-col h-[640px]" data-testid="swarm-transcript">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex items-center justify-between shrink-0">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          SWARM_TRANSCRIPT
        </h2>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={store.clear}
            className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-primary hover:border-primary"
            data-testid="button-transcript-clear"
          >
            Clear
          </button>
          <WsStatusIndicator
            status={status}
            reconnectIn={reconnectIn}
            closeCode={closeCode}
            label="OPEN"
          />
        </div>
      </div>
      <div className="overflow-auto grow p-2 space-y-2">
        {order.length === 0 ? (
          <div className="p-3 text-muted-foreground text-[10px] uppercase tracking-widest text-center">
            Awaiting first debate…
          </div>
        ) : (
          order.map((id) => (
            <DebateCard
              key={id}
              store={store}
              debateId={id}
              highlightMarketId={highlightMarketId ?? null}
            />
          ))
        )}
      </div>
    </div>
  );
}

/**
 * Convenience hook: opens /api/ws/llm-logs, feeds the store, and returns
 * the props the panel needs.  Kept as a hook so the parent owns the
 * websocket lifecycle.
 */
export function useSwarmTranscript() {
  const storeRef = useRef<SwarmTranscriptStore | null>(null);
  if (storeRef.current === null) storeRef.current = new SwarmTranscriptStore();
  const store = storeRef.current;

  const onMessage = useCallback(
    (ev: Record<string, unknown>) => {
      store.ingest(ev);
    },
    [store],
  );

  const ws = useWebSocket({
    path: "/api/ws/llm-logs",
    bufferSize: 50, // we keep our own state; the buffer is just for the status indicator
    onMessage,
  });

  return useMemo(
    () => ({ ws, store }),
    [ws, store],
  );
}

export default SwarmTranscript;
