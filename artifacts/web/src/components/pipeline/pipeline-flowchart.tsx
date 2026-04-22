import { useMemo } from "react";
import {
  getGetRecentCyclesQueryKey,
  useGetRecentCycles,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

/**
 * 6-stage pipeline flowchart strip.
 *
 * Stages mirror the orchestrator loop documented in the frontend
 * report (§6.13): universe → features → predict → debate → risk →
 * execute. Status dots derive from the most-recent cycle (the one
 * the operator is most likely watching) plus rolling counts from
 * the last `limit` cycles for context.
 *
 * `recent-events` stages aren't on a public endpoint, so we infer
 * status conservatively from cycle telemetry:
 *   - cycle still running → all stages "active"
 *   - cycle errored → "fail"
 *   - completed with picks_evaluated > 0 → "ok"
 *   - completed with no picks → "idle"
 */
const STAGES: { key: string; label: string; description: string }[] = [
  { key: "universe", label: "UNIVERSE", description: "Markets pulled" },
  { key: "features", label: "FEATURES", description: "Signals built" },
  { key: "predict", label: "PREDICT", description: "Model probability" },
  { key: "debate", label: "DEBATE", description: "Swarm consensus" },
  { key: "risk", label: "RISK", description: "12-gate check" },
  { key: "execute", label: "EXECUTE", description: "Order placed" },
];

type StageStatus = "ok" | "active" | "fail" | "idle";

const STATUS_CLASS: Record<StageStatus, string> = {
  ok: "bg-success/15 border-success text-success",
  active: "bg-primary/15 border-primary text-primary animate-pulse",
  fail: "bg-loss/15 border-loss text-loss",
  idle: "bg-card border-border text-muted-foreground",
};

export default function PipelineFlowchart() {
  const params = { limit: 20 };
  const { data, isLoading } = useGetRecentCycles(params, {
    query: {
      queryKey: getGetRecentCyclesQueryKey(params),
      refetchInterval: 30_000,
    },
  });

  const stats = useMemo(() => {
    const items = data?.items ?? [];
    const total = items.length;
    let running = 0;
    let errored = 0;
    let placed = 0;
    let evaluated = 0;
    for (const c of items) {
      if (!c.completed_at) running++;
      if (c.errors) errored++;
      placed += c.trades_placed ?? 0;
      evaluated += c.picks_evaluated ?? 0;
    }
    const latest = items[0] ?? null;
    let stageStatus: StageStatus = "idle";
    if (latest) {
      if (!latest.completed_at) stageStatus = "active";
      else if (latest.errors) stageStatus = "fail";
      else if ((latest.picks_evaluated ?? 0) > 0) stageStatus = "ok";
      else stageStatus = "idle";
    }
    return { total, running, errored, placed, evaluated, latest, stageStatus };
  }, [data]);

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-pipeline-flowchart">
      <SectionHeader
        eyebrow="ORCHESTRATOR"
        title="Cycle flowchart"
        subtitle="Six-stage trading loop · status reflects the most recent cycle."
        actions={
          <div className="flex flex-wrap gap-2 text-[10px] uppercase tracking-widest text-muted-foreground">
            <Stat label="cycles" value={stats.total} testId="stat-flow-total" />
            <Stat
              label="running"
              value={stats.running}
              valueClass={stats.running > 0 ? "text-primary" : undefined}
              testId="stat-flow-running"
            />
            <Stat
              label="errors"
              value={stats.errored}
              valueClass={stats.errored > 0 ? "text-loss" : undefined}
              testId="stat-flow-errors"
            />
            <Stat
              label="placed"
              value={stats.placed}
              valueClass={stats.placed > 0 ? "text-win" : undefined}
              testId="stat-flow-placed"
            />
          </div>
        }
      />
      {isLoading ? (
        <div
          className="flex flex-wrap gap-2"
          data-testid="skeleton-flowchart"
        >
          {STAGES.map((s) => (
            <div
              key={s.key}
              className="h-16 w-32 animate-pulse bg-muted/10"
            />
          ))}
        </div>
      ) : (
        <div
          className="flex flex-wrap items-stretch gap-2"
          data-testid="flowchart-stages"
        >
          {STAGES.map((s, i) => {
            const status: StageStatus = stats.stageStatus;
            return (
              <div
                key={s.key}
                className="flex items-stretch gap-2"
                data-testid={`stage-${s.key}`}
              >
                <div
                  className={`flex min-w-[140px] flex-col justify-between border-2 px-3 py-2 transition-colors ${STATUS_CLASS[status]}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] font-bold tracking-widest">
                      {s.label}
                    </span>
                    <span
                      className={`h-2 w-2 rounded-full ${
                        status === "active"
                          ? "bg-primary animate-pulse"
                          : status === "fail"
                            ? "bg-loss"
                            : status === "ok"
                              ? "bg-success"
                              : "bg-muted-foreground"
                      }`}
                    />
                  </div>
                  <p className="mt-1 text-[10px] uppercase tracking-widest opacity-80">
                    {s.description}
                  </p>
                </div>
                {i < STAGES.length - 1 && (
                  <div className="flex items-center text-muted-foreground">
                    →
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </GlassCard>
  );
}

function Stat({
  label,
  value,
  valueClass,
  testId,
}: {
  label: string;
  value: number;
  valueClass?: string;
  testId?: string;
}) {
  return (
    <div
      className="border border-border bg-card/50 px-2 py-1"
      data-testid={testId}
    >
      <span className="opacity-60">{label}</span>{" "}
      <span className={`tabular-nums ${valueClass ?? "text-foreground"}`}>
        {value}
      </span>
    </div>
  );
}
