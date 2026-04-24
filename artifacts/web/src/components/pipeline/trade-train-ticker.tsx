import { useCallback, useMemo, useState } from "react";
import { Link } from "wouter";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { usePipelineSubscription } from "@/components/realtime/websocket-provider";
import { extractVenuesFromEvent, venueDisplayLabel } from "@/lib/venues";

/**
 * Per-trade pipeline ticker.
 *
 * Each in-flight candidate becomes its own horizontal "train" of
 * stage chips:
 *
 *   PREDICT → SWARM → GATEKEEPER → 12 risk gates → PLACE
 *
 * The chips pulse amber while the stage is the current head of the
 * train, flip green ✓ on pass, red ✗ on fail, dim grey on skip
 * (stages that never run because an earlier gate rejected the
 * candidate). Click any chip to drill into the decision payload that
 * gate emitted — gate name, reason, full details JSON, and a link
 * to `/trade/:ref` for the post-hoc deep-dive.
 *
 * Data source: the existing pipeline-WS event stream
 * (`usePipelineSubscription`). No additional REST calls or DB writes
 * are required — this is a pure derivation of events that already
 * fan out from `app/orchestrator/pipeline_event_hub`.
 *
 * Event mapping:
 *   - `candidate_start`  → PREDICT, SWARM, GATEKEEPER all flip green
 *                          (a candidate only reaches this point AFTER
 *                          the swarm gatekeeper approved it; see
 *                          `cycle.py` candidate-filter at L330).
 *   - `gate`             → flip the named gate green/red.
 *   - `candidate_complete` outcome=placed     → PLACE green
 *                                rejected     → PLACE skip (rejecting gate already red)
 *                                place_error  → PLACE red
 */

const TWELVE_GATES = [
  "kill_switch",
  "market_cooldown",
  "swarm_approval",
  "min_edge",
  "drift",
  "agreement",
  "kelly_fraction",
  "bankroll_floor",
  "global_exposure_cap",
  "per_vertical_cap",
  "max_concurrent_positions",
  "duplicate_trade",
] as const;

type GateName = (typeof TWELVE_GATES)[number];

const STAGE_KEYS = [
  "predict",
  "swarm",
  "gatekeeper",
  ...TWELVE_GATES,
  "place",
] as const;

type StageKey = (typeof STAGE_KEYS)[number];

const STAGE_LABEL: Record<StageKey, string> = {
  predict: "PREDICT",
  swarm: "SWARM",
  gatekeeper: "GATE-KEEP",
  kill_switch: "KILL-SW",
  market_cooldown: "COOLDOWN",
  swarm_approval: "SWRM-OK",
  min_edge: "EDGE",
  drift: "DRIFT",
  agreement: "AGREE",
  kelly_fraction: "KELLY",
  bankroll_floor: "BANKROLL",
  global_exposure_cap: "EXPOSURE",
  per_vertical_cap: "VERT-CAP",
  max_concurrent_positions: "MAX-OPEN",
  duplicate_trade: "DEDUP",
  place: "PLACE",
};

type StageStatus = "pending" | "pass" | "fail" | "skip";

interface StageState {
  status: StageStatus;
  detail?: Record<string, unknown>;
  ts?: number;
}

interface TradeTrain {
  id: string;
  cycle_id: string | number | null;
  market_id: string;
  side: string;
  vertical?: string;
  debate_id?: string | number | null;
  startedAt: number;
  finalizedAt?: number;
  outcome?: "placed" | "rejected" | "place_error" | "pending";
  rejectingGate?: string;
  reason?: string;
  modelProbability?: number;
  swarmConsensus?: number;
  edge?: number;
  currentPrice?: number;
  liveModelName?: string;
  placement?: Record<string, unknown>;
  // Lower-cased venue tags ("kalshi" / "polymarket") collected from the
  // candidate_complete payload — single-venue placements yield one
  // entry, fan-out yields one per venue.
  venues?: string[];
  stages: Record<StageKey, StageState>;
}

const MAX_TRAINS = 24;

function emptyStages(): Record<StageKey, StageState> {
  const obj = {} as Record<StageKey, StageState>;
  for (const k of STAGE_KEYS) obj[k] = { status: "pending" };
  return obj;
}

function trainKey(ev: Record<string, unknown>): string | null {
  const cycle = ev.cycle_id;
  const market = ev.market_id;
  const side = ev.side;
  if (cycle === undefined || cycle === null) return null;
  if (typeof market !== "string" || !market) return null;
  if (typeof side !== "string" || !side) return null;
  return `${cycle}::${market}::${side}`;
}

function getNum(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function getStr(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined;
}

function applyEvent(
  trains: Map<string, TradeTrain>,
  ev: Record<string, unknown>,
): Map<string, TradeTrain> {
  const type = String(ev.type ?? "");
  if (type !== "candidate_start" && type !== "gate" && type !== "candidate_complete") {
    return trains;
  }
  const key = trainKey(ev);
  if (!key) return trains;

  const next = new Map(trains);
  let train = next.get(key);

  if (type === "candidate_start") {
    if (!train) {
      train = {
        id: key,
        cycle_id: (ev.cycle_id as string | number | null) ?? null,
        market_id: String(ev.market_id),
        side: String(ev.side),
        vertical: getStr(ev.vertical),
        debate_id: (ev.debate_id as string | number | null | undefined) ?? null,
        startedAt: Date.now(),
        outcome: "pending",
        stages: emptyStages(),
        modelProbability: getNum(ev.model_probability) ?? getNum(ev.live_model_probability),
        swarmConsensus: getNum(ev.swarm_consensus),
        edge: getNum(ev.edge),
        currentPrice: getNum(ev.current_price),
        liveModelName: getStr(ev.live_model_name),
      };
    }
    // PREDICT / SWARM / GATEKEEPER all complete once a candidate
    // reaches this event (cycle.py filters approved=false BEFORE
    // this point — see L330).
    train.stages.predict = {
      status: "pass",
      detail: {
        live_model_probability: ev.live_model_probability,
        live_model_version: ev.live_model_version,
        live_model_name: ev.live_model_name,
        live_model_feature_count: ev.live_model_feature_count,
        live_model_feature_inputs: ev.live_model_feature_inputs,
        edge: ev.edge,
      },
      ts: Date.now(),
    };
    train.stages.swarm = {
      status: "pass",
      detail: {
        swarm_consensus: ev.swarm_consensus,
        debate_id: ev.debate_id,
      },
      ts: Date.now(),
    };
    train.stages.gatekeeper = {
      status: "pass",
      detail: {
        approved: true,
        note:
          "Gatekeeper approval is implicit at candidate_start — " +
          "the orchestrator filters approved=false debates before " +
          "they reach the gate stage. See debate transcript on the " +
          "trade detail page.",
      },
      ts: Date.now(),
    };
    next.set(key, train);
    return next;
  }

  // For gate / candidate_complete, the candidate_start may have been
  // dropped from the buffer (page mounted mid-stream); synthesise a
  // stub so the stage flip still renders.
  if (!train) {
    train = {
      id: key,
      cycle_id: (ev.cycle_id as string | number | null) ?? null,
      market_id: String(ev.market_id),
      side: String(ev.side),
      vertical: getStr(ev.vertical),
      debate_id: (ev.debate_id as string | number | null | undefined) ?? null,
      startedAt: Date.now(),
      outcome: "pending",
      stages: emptyStages(),
    };
    // Mid-stream attach: the predict/swarm/gatekeeper already passed,
    // we just don't have their details — mark them pass with an
    // explanatory note so the chip shows green.
    for (const k of ["predict", "swarm", "gatekeeper"] as const) {
      train.stages[k] = {
        status: "pass",
        detail: { note: "Mid-stream attach — earlier event not in buffer" },
        ts: Date.now(),
      };
    }
  }

  if (type === "gate") {
    const name = String(ev.gate ?? ev.name ?? "");
    if ((TWELVE_GATES as readonly string[]).includes(name)) {
      const passed = ev.passed === true;
      train.stages[name as GateName] = {
        status: passed ? "pass" : "fail",
        detail: ev,
        ts: Date.now(),
      };
    }
    next.set(key, train);
    return next;
  }

  // candidate_complete
  const outcome = String(ev.outcome ?? "pending") as TradeTrain["outcome"];
  train.outcome = outcome;
  train.finalizedAt = Date.now();
  if (outcome === "placed") {
    train.stages.place = {
      status: "pass",
      detail: ev,
      ts: Date.now(),
    };
    train.placement = (ev.placement as Record<string, unknown> | undefined) ?? undefined;
    // Collect venue tags (lower-cased, deduped, primary-first) so the
    // train row can render `KALSHI · +1 venue` for fan-out placements.
    const venues = extractVenuesFromEvent(ev);
    if (venues.length > 0) train.venues = venues;
  } else if (outcome === "rejected") {
    train.rejectingGate = getStr(ev.rejecting_gate);
    train.reason = getStr(ev.reason);
    train.stages.place = {
      status: "skip",
      detail: ev,
      ts: Date.now(),
    };
    // Force the rejecting gate to fail with the completion payload.
    // The per-gate event may have been dropped from the buffer (page
    // mounted mid-cycle) — without this, a rejected train can render
    // with no red chip at all.
    if (
      train.rejectingGate &&
      (TWELVE_GATES as readonly string[]).includes(train.rejectingGate)
    ) {
      const rg = train.rejectingGate as GateName;
      const prev = train.stages[rg];
      if (prev.status !== "fail") {
        train.stages[rg] = {
          status: "fail",
          detail: { ...(prev.detail ?? {}), rejected_at_completion: true, reason: train.reason, ...ev },
          ts: Date.now(),
        };
      }
    }
  } else if (outcome === "place_error") {
    train.stages.place = {
      status: "fail",
      detail: ev,
      ts: Date.now(),
    };
    train.reason = getStr(ev.reason);
  }

  // Sweep: on ANY finalised train, no chip should remain `pending`.
  // Pre-failure pendings become `pass` (the orchestrator only emits
  // a candidate_complete after every earlier stage cleared); post-
  // failure pendings become `skip`. This also fixes mid-stream
  // attaches that observe a late gate or a completion event without
  // the predicate gate frames.
  const failIdx = STAGE_KEYS.findIndex((k) => train!.stages[k].status === "fail");
  STAGE_KEYS.forEach((k, idx) => {
    const s = train!.stages[k];
    if (s.status !== "pending") return;
    if (failIdx === -1 || idx < failIdx) {
      train!.stages[k] = {
        status: "pass",
        detail: { note: "Inferred pass — earlier event not in buffer or implicit at completion" },
        ts: Date.now(),
      };
    } else if (idx > failIdx) {
      train!.stages[k] = { status: "skip", ts: Date.now() };
    }
  });

  next.set(key, train);
  return next;
}

// LRU-trim to the most recent N trains by startedAt.
function trimTrains(trains: Map<string, TradeTrain>): Map<string, TradeTrain> {
  if (trains.size <= MAX_TRAINS) return trains;
  const sorted = Array.from(trains.values()).sort((a, b) => b.startedAt - a.startedAt);
  const keep = new Map<string, TradeTrain>();
  for (const t of sorted.slice(0, MAX_TRAINS)) keep.set(t.id, t);
  return keep;
}

const STATUS_CHIP: Record<StageStatus, string> = {
  pending:
    "border-amber-400/60 bg-amber-400/10 text-amber-200 animate-pulse",
  pass: "border-success/70 bg-success/15 text-success",
  fail: "border-loss/70 bg-loss/20 text-loss",
  skip: "border-border bg-card/40 text-muted-foreground/60",
};

const STATUS_GLYPH: Record<StageStatus, string> = {
  pending: "•",
  pass: "✓",
  fail: "✗",
  skip: "–",
};

function formatRelTime(ts: number): string {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  return `${h}h ago`;
}

interface DrillTarget {
  trainId: string;
  stageKey: StageKey;
}

export default function TradeTrainTicker() {
  const [trains, setTrains] = useState<Map<string, TradeTrain>>(() => new Map());
  const [drill, setDrill] = useState<DrillTarget | null>(null);

  const handleEvent = useCallback((event: Record<string, unknown>) => {
    setTrains((prev) => trimTrains(applyEvent(prev, event)));
  }, []);

  usePipelineSubscription(handleEvent);

  const orderedTrains = useMemo(() => {
    return Array.from(trains.values()).sort((a, b) => b.startedAt - a.startedAt);
  }, [trains]);

  const drillTrain = drill ? trains.get(drill.trainId) ?? null : null;
  const drillStage = drillTrain && drill ? drillTrain.stages[drill.stageKey] : null;

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-trade-train-ticker">
      <SectionHeader
        eyebrow="LIVE TRADES"
        title="Per-trade pipeline ticker"
        subtitle="Each candidate threads through 15 stages — predict, swarm, gatekeeper, 12 risk gates, place. Click any chip to drill into the decision."
      />

      {orderedTrains.length === 0 ? (
        <div
          className="mt-4 rounded-md border border-dashed border-border bg-card/40 p-6 text-center text-sm text-muted-foreground"
          data-testid="text-trade-train-empty"
        >
          Waiting for the next candidate to enter the pipeline…
        </div>
      ) : (
        <ul className="mt-4 space-y-2">
          {orderedTrains.map((t) => (
            <TrainRow
              key={t.id}
              train={t}
              onDrill={(stageKey) => setDrill({ trainId: t.id, stageKey })}
            />
          ))}
        </ul>
      )}

      {drillTrain && drillStage ? (
        <DrillPanel
          train={drillTrain}
          stageKey={drill!.stageKey}
          stage={drillStage}
          onClose={() => setDrill(null)}
        />
      ) : null}
    </GlassCard>
  );
}

function venueBadgeClass(raw: string): string {
  if (raw === "kalshi") return "border-primary/60 bg-primary/10 text-primary";
  if (raw === "polymarket") return "border-warning/60 bg-warning/10 text-warning";
  return "border-border bg-card/40 text-muted-foreground";
}

// Compact venue chip for the train row. Single-venue placements show
// just the routed exchange; fan-out adds a "+N venue" overflow chip so
// operators can see at a glance that the trade landed on both books.
function VenueBadge({ venues }: { venues: string[] }) {
  if (venues.length === 0) return null;
  const primary = venues[0]!;
  const extra = venues.length - 1;
  return (
    <span
      className="inline-flex items-center gap-1"
      data-testid="train-venues"
      data-venue-count={venues.length}
    >
      <span
        className={`rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${venueBadgeClass(primary)}`}
        data-testid="train-venue-primary"
      >
        {venueDisplayLabel(primary)}
      </span>
      {extra > 0 ? (
        <span
          className="rounded border border-border bg-card/40 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground"
          data-testid="train-venue-overflow"
          title={`Also routed to: ${venues.slice(1).map(venueDisplayLabel).join(", ")}`}
        >
          +{extra} venue{extra === 1 ? "" : "s"}
        </span>
      ) : null}
    </span>
  );
}

interface TrainRowProps {
  train: TradeTrain;
  onDrill: (stage: StageKey) => void;
}

function TrainRow({ train, onDrill }: TrainRowProps) {
  const finalised =
    train.outcome === "placed" ||
    train.outcome === "rejected" ||
    train.outcome === "place_error";

  const outcomeBadge = (() => {
    if (train.outcome === "placed")
      return { text: "PLACED", className: "bg-success/15 text-success border-success/60" };
    if (train.outcome === "rejected")
      return { text: "REJECTED", className: "bg-loss/15 text-loss border-loss/60" };
    if (train.outcome === "place_error")
      return { text: "PLACE-ERR", className: "bg-loss/20 text-loss border-loss/70" };
    return { text: "RUNNING", className: "bg-primary/10 text-primary border-primary/60 animate-pulse" };
  })();

  return (
    <li
      className="rounded-md border border-border bg-card/30 p-3"
      data-testid={`train-row-${train.id}`}
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
        <span className="font-semibold text-foreground" data-testid="text-train-market">
          {train.market_id}
        </span>
        <span className="text-muted-foreground">{train.side}</span>
        {train.vertical ? (
          <span className="text-muted-foreground">· {train.vertical}</span>
        ) : null}
        {train.modelProbability !== undefined ? (
          <span className="text-muted-foreground">
            · model {(train.modelProbability * 100).toFixed(1)}%
          </span>
        ) : null}
        {train.swarmConsensus !== undefined ? (
          <span className="text-muted-foreground">
            · swarm {(train.swarmConsensus * 100).toFixed(1)}%
          </span>
        ) : null}
        {train.edge !== undefined ? (
          <span className="text-muted-foreground">
            · edge {(train.edge * 100).toFixed(2)}%
          </span>
        ) : null}
        <span className="ml-auto flex items-center gap-2">
          {train.outcome === "placed" && train.venues && train.venues.length > 0 ? (
            <VenueBadge venues={train.venues} />
          ) : null}
          <span
            className={`rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${outcomeBadge.className}`}
            data-testid="text-train-outcome"
          >
            {outcomeBadge.text}
          </span>
          <span className="text-muted-foreground">
            {formatRelTime(train.startedAt)}
          </span>
          {train.debate_id ? (
            <Link
              href={`/trade/${train.debate_id}`}
              className="text-primary underline-offset-2 hover:underline"
              data-testid="link-train-detail"
            >
              detail →
            </Link>
          ) : null}
        </span>
      </div>

      <div className="mt-2 flex flex-nowrap items-center gap-1 overflow-x-auto pb-1">
        {STAGE_KEYS.map((k, idx) => {
          const stage = train.stages[k];
          // The first 'pending' stage on a non-finalised train is the
          // active head — let it pulse, leave the rest neutral.
          let display: StageStatus = stage.status;
          if (!finalised && stage.status === "pending") {
            const firstPending = STAGE_KEYS.find(
              (s) => train.stages[s].status === "pending",
            );
            if (firstPending !== k) {
              display = "skip";
            }
          }
          const cls = STATUS_CHIP[display];
          return (
            <button
              key={k}
              type="button"
              onClick={() => onDrill(k)}
              className={`shrink-0 rounded border px-2 py-1 font-mono text-[10px] uppercase tracking-wider transition-colors hover:brightness-125 ${cls}`}
              data-testid={`chip-${train.id}-${k}`}
              title={`${STAGE_LABEL[k]} — click for detail`}
            >
              <span aria-hidden className="mr-1">
                {STATUS_GLYPH[display]}
              </span>
              {STAGE_LABEL[k]}
              {idx < STAGE_KEYS.length - 1 ? null : null}
            </button>
          );
        })}
      </div>
    </li>
  );
}

interface DrillPanelProps {
  train: TradeTrain;
  stageKey: StageKey;
  stage: StageState;
  onClose: () => void;
}

function DrillPanel({ train, stageKey, stage, onClose }: DrillPanelProps) {
  return (
    <div
      className="fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col border-l border-border bg-background/95 backdrop-blur"
      data-testid="panel-trade-train-drill"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border p-4">
        <div>
          <div className="text-xs uppercase tracking-wider text-muted-foreground">
            Stage detail
          </div>
          <h3 className="mt-1 font-mono text-sm font-semibold text-foreground">
            {STAGE_LABEL[stageKey]}
          </h3>
          <div className="mt-1 text-xs text-muted-foreground">
            {train.market_id} · {train.side}
            {train.vertical ? ` · ${train.vertical}` : ""}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-card"
          data-testid="button-trade-train-drill-close"
        >
          close
        </button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4 text-xs">
        <div className="grid grid-cols-2 gap-2">
          <Stat label="Status" value={stage.status.toUpperCase()} />
          <Stat
            label="When"
            value={stage.ts ? formatRelTime(stage.ts) : "—"}
          />
          {train.modelProbability !== undefined ? (
            <Stat
              label="Model prob"
              value={`${(train.modelProbability * 100).toFixed(2)}%`}
            />
          ) : null}
          {train.swarmConsensus !== undefined ? (
            <Stat
              label="Swarm consensus"
              value={`${(train.swarmConsensus * 100).toFixed(2)}%`}
            />
          ) : null}
          {train.edge !== undefined ? (
            <Stat label="Edge" value={`${(train.edge * 100).toFixed(2)}%`} />
          ) : null}
          {train.currentPrice !== undefined ? (
            <Stat
              label="Price"
              value={`${(train.currentPrice * 100).toFixed(1)}¢`}
            />
          ) : null}
        </div>

        {train.outcome === "rejected" && train.rejectingGate ? (
          <div className="rounded-md border border-loss/60 bg-loss/10 p-3 text-loss">
            <div className="font-semibold">Rejected at: {train.rejectingGate}</div>
            {train.reason ? (
              <div className="mt-1 text-loss/80">{train.reason}</div>
            ) : null}
          </div>
        ) : null}

        {(() => {
          // Surface the per-vertical-cap `sub_check` payload (e.g.
          // `commodities_correlation`) inline so operators don't have
          // to scan the raw JSON to understand why the gate fired.
          const detail = (stage.detail ?? {}) as Record<string, unknown>;
          const det =
            detail["details"] && typeof detail["details"] === "object"
              ? (detail["details"] as Record<string, unknown>)
              : detail;
          const subCheck = typeof det["sub_check"] === "string" ? (det["sub_check"] as string) : null;
          if (!subCheck) return null;
          const intendedSymbol =
            typeof det["intended_symbol"] === "string"
              ? (det["intended_symbol"] as string)
              : null;
          const projected = typeof det["projected"] === "number" ? (det["projected"] as number) : null;
          const capUsd = typeof det["cap_usd"] === "number" ? (det["cap_usd"] as number) : null;
          const capPct = typeof det["cap_pct"] === "number" ? (det["cap_pct"] as number) : null;
          const pairsRaw = det["pair_contributions"];
          const pairs =
            Array.isArray(pairsRaw)
              ? (pairsRaw as Array<Record<string, unknown>>).slice(0, 5)
              : [];
          return (
            <div
              className="rounded-md border border-warning/50 bg-warning/5 p-3 text-warning"
              data-testid="panel-gate-sub-check"
            >
              <div className="font-semibold uppercase tracking-wider text-[11px]">
                Sub-check: {subCheck}
              </div>
              <div className="mt-2 grid grid-cols-2 gap-2 text-[11px] text-warning/90">
                {intendedSymbol ? (
                  <Stat label="Intended symbol" value={intendedSymbol} />
                ) : null}
                {projected !== null ? (
                  <Stat
                    label="Projected exposure"
                    value={`$${projected.toFixed(2)}`}
                  />
                ) : null}
                {capUsd !== null ? (
                  <Stat label="Cap" value={`$${capUsd.toFixed(2)}`} />
                ) : null}
                {capPct !== null ? (
                  <Stat label="Cap %" value={`${(capPct * 100).toFixed(1)}%`} />
                ) : null}
              </div>
              {pairs.length > 0 ? (
                <div className="mt-3">
                  <div className="text-[10px] uppercase tracking-wider text-warning/80">
                    Top correlated pair contributions
                  </div>
                  <ul className="mt-1 space-y-0.5 text-[11px]">
                    {pairs.map((p, i) => {
                      const sym =
                        typeof p["symbol"] === "string" ? (p["symbol"] as string) : "—";
                      const corr =
                        typeof p["correlation"] === "number"
                          ? (p["correlation"] as number)
                          : null;
                      const contrib =
                        typeof p["contribution_usd"] === "number"
                          ? (p["contribution_usd"] as number)
                          : null;
                      return (
                        <li key={`${sym}-${i}`} className="flex justify-between gap-3 font-mono">
                          <span>{sym}</span>
                          <span className="text-warning/80">
                            ρ={corr === null ? "—" : corr.toFixed(2)}
                            {contrib !== null ? ` · +$${contrib.toFixed(2)}` : ""}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ) : null}
            </div>
          );
        })()}

        {train.outcome === "place_error" && train.reason ? (
          <div className="rounded-md border border-loss/60 bg-loss/10 p-3 text-loss">
            <div className="font-semibold">Placement failed</div>
            <div className="mt-1 text-loss/80">{train.reason}</div>
          </div>
        ) : null}

        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
            Decision payload
          </div>
          <pre
            className="max-h-96 overflow-auto rounded-md border border-border bg-card/40 p-2 font-mono text-[11px] leading-snug text-foreground"
            data-testid="pre-trade-train-drill-payload"
          >
            {JSON.stringify(stage.detail ?? {}, null, 2)}
          </pre>
        </div>

        {train.debate_id ? (
          <div className="border-t border-border pt-3">
            <Link
              href={`/trade/${train.debate_id}`}
              className="text-primary underline-offset-2 hover:underline"
              data-testid="link-trade-train-drill-detail"
            >
              View full trade transcript →
            </Link>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-card/40 p-2">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="mt-0.5 font-mono text-foreground">{value}</div>
    </div>
  );
}
