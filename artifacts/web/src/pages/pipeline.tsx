import { useEffect, useMemo, useState } from "react";
import { Link } from "wouter";
import { useAuth, useClerk, useUser } from "@clerk/react";
import { useWebSocket } from "@/hooks/use-websocket";
import WsStatusIndicator from "@/components/ws-status-indicator";
import { apiFetch } from "@/lib/api";

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
        <span
          className="font-bold text-foreground truncate max-w-[260px]"
          title={market}
        >
          {market}
        </span>
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
  formatLine: (ev: Record<string, unknown>) => string;
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

function formatSwarmLine(ev: Record<string, unknown>): string {
  const t = pickStr(ev, "type") ?? "event";
  const persona = pickStr(ev, "persona") ?? pickStr(ev, "analyst_id");
  const debate = pickStr(ev, "debate_id");
  const verdict =
    pickStr(ev, "recommended_action") ??
    pickStr(ev, "verdict") ??
    pickStr(ev, "decision");
  const matchup = pickStr(ev, "matchup");
  const created = pickStr(ev, "ts") ?? pickStr(ev, "created_at");
  const head = `${fmtTimePart(created)} · ${t.toUpperCase()}`;
  const tail = [persona && `@${persona}`, matchup, verdict && `→ ${verdict}`, debate && `[${debate.slice(0, 8)}]`]
    .filter(Boolean)
    .join(" · ");
  return tail ? `${head} · ${tail}` : head;
}

function formatSettlementLine(ev: Record<string, unknown>): string {
  const t = pickStr(ev, "type") ?? "event";
  const market = pickStr(ev, "market_id");
  const tradeId = pickStr(ev, "trade_id");
  const outcomeYes = ev["outcome_yes"];
  const pnl = pickNum(ev, "pnl");
  const ts = pickStr(ev, "ts") ?? pickStr(ev, "settled_at") ?? pickStr(ev, "completed_at");
  const head = `${fmtTimePart(ts)} · ${t.toUpperCase()}`;
  const parts = [
    market,
    tradeId && `trade=${tradeId.slice(0, 8)}`,
    typeof outcomeYes === "number" ? `outcome=${outcomeYes ? "YES" : "NO"}` : null,
    typeof pnl === "number" ? `pnl=$${pnl.toFixed(2)}` : null,
  ].filter(Boolean) as string[];
  return parts.length ? `${head} · ${parts.join(" · ")}` : head;
}

export default function PipelinePage() {
  const { user } = useUser();
  const { signOut } = useClerk();
  const { getToken, isSignedIn } = useAuth();
  const ws = useWebSocket({ path: "/api/ws/pipeline", bufferSize: 500 });
  const llm = useWebSocket({ path: "/api/ws/llm-logs", bufferSize: 200 });
  const settlement = useWebSocket({ path: "/api/ws/settlement", bufferSize: 200 });

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

  // Merge snapshot + live WS events, dedup'd on the server-assigned _seq.
  // Snapshot events that the WS later replays are filtered out so the
  // same cycle card never doubles up.
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
    for (const ev of snapshot) push(ev);
    for (const ev of ws.events) push(ev);
    return out;
  }, [snapshot, ws.events]);

  const groups = useMemo(() => groupByCycle(mergedEvents), [mergedEvents]);
  const operatorLabel =
    user?.primaryEmailAddress?.emailAddress ?? user?.username ?? user?.id ?? "—";

  return (
    <div className="min-h-[100dvh] bg-background text-foreground p-4 md:p-6 font-mono">
      <header className="mb-6 flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl md:text-2xl font-bold tracking-tight text-primary uppercase">
            AXIOM_PREDICT :: PIPELINE_MONITOR
          </h1>
          <p className="text-muted-foreground text-xs uppercase tracking-widest mt-1">
            Live cycle stream · gates · candidate decisions
          </p>
        </div>
        <div className="text-xs text-muted-foreground flex flex-wrap items-center gap-4">
          <WsStatusIndicator
            status={ws.status}
            reconnectIn={ws.reconnectIn}
            closeCode={ws.closeCode}
            label="WS_OPEN"
          />
          <Link
            href="/"
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
            data-testid="link-dashboard"
          >
            ← Dashboard
          </Link>
          <div className="flex items-center gap-2">
            <span className="uppercase tracking-widest">OP</span>
            <span className="text-foreground truncate max-w-[200px]">
              {operatorLabel}
            </span>
          </div>
          <button
            type="button"
            onClick={() => signOut({ redirectUrl: import.meta.env.BASE_URL })}
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-destructive hover:border-destructive"
            data-testid="button-sign-out"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="flex items-center justify-between mb-3 text-[10px] uppercase tracking-widest text-muted-foreground">
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
          {groups.length === 0 ? (
            <div className="border border-border bg-card p-8 text-center text-muted-foreground text-xs uppercase tracking-widest">
              {ws.status === "open"
                ? "Waiting for the next cycle event…"
                : "Stream not connected."}
            </div>
          ) : (
            groups.map((g, i) => (
              <CycleCard key={`${g.cycle_id ?? "x"}-${i}`} group={g} />
            ))
          )}
        </section>
        <aside className="space-y-3">
          <OutOfBandFeed
            title="SWARM_VERDICTS"
            events={llm.events}
            status={llm.status}
            reconnectIn={llm.reconnectIn}
            closeCode={llm.closeCode}
            emptyText="Awaiting persona / gatekeeper output…"
            formatLine={formatSwarmLine}
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
    </div>
  );
}
