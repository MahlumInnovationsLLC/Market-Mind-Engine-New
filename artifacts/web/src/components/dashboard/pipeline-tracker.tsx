import { useCallback, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getGetRecentCyclesQueryKey,
  useGetRecentCycles,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { useWebSocket } from "@/hooks/use-websocket";

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = (seconds - m * 60).toFixed(0);
  return `${m}m${s}s`;
}

const STATUS_COLOR: Record<string, string> = {
  open: "text-success",
  connecting: "text-warn",
  closed: "text-loss",
  error: "text-loss",
  unauthorized: "text-loss",
  idle: "text-muted-foreground",
};

export default function PipelineTracker() {
  const queryClient = useQueryClient();
  const params = { limit: 5 };
  const [lastEvent, setLastEvent] = useState<string | null>(null);

  const { data, isLoading } = useGetRecentCycles(params, {
    query: {
      queryKey: getGetRecentCyclesQueryKey(params),
      refetchInterval: 30_000,
    },
  });

  const onEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      if (t) setLastEvent(t);
      if (
        t === "cycle_complete" ||
        t === "cycle_started" ||
        t === "candidate_complete"
      ) {
        queryClient.invalidateQueries({
          queryKey: getGetRecentCyclesQueryKey(params),
        });
      }
    },
    [queryClient],
  );

  const ws = useWebSocket({
    path: "/api/ws/pipeline",
    onMessage: onEvent,
    bufferSize: 50,
  });
  const statusClass = STATUS_COLOR[ws.status] ?? "text-muted-foreground";
  const cycles = data?.items ?? [];
  const latest = cycles[0];

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-pipeline-tracker">
      <SectionHeader
        eyebrow="ORCHESTRATOR"
        title="Pipeline tracker"
        subtitle="Live swarm pipeline + last 5 cycles."
        actions={
          <span
            className={`inline-flex items-center gap-1 text-[10px] uppercase tracking-widest ${statusClass}`}
            data-testid="text-pipeline-ws-status"
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                ws.status === "open"
                  ? "bg-success animate-pulse"
                  : ws.status === "connecting"
                    ? "bg-warn"
                    : "bg-muted-foreground"
              }`}
            />
            {ws.status}
          </span>
        }
      />

      {isLoading ? (
        <div className="space-y-2" data-testid="skeleton-pipeline">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-6 animate-pulse bg-muted/10" />
          ))}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2 border-b border-border pb-2 text-[10px]">
            <div>
              <div className="uppercase tracking-widest text-muted-foreground">
                LAST EVENT
              </div>
              <div
                className="font-bold text-primary truncate"
                data-testid="text-pipeline-last-event"
              >
                {lastEvent ?? "—"}
              </div>
            </div>
            <div>
              <div className="uppercase tracking-widest text-muted-foreground">
                LAST CYCLE
              </div>
              <div className="font-bold tabular-nums">
                {fmtTime(latest?.started_at)}
              </div>
            </div>
            <div>
              <div className="uppercase tracking-widest text-muted-foreground">
                PLACED
              </div>
              <div
                className={`font-bold tabular-nums ${
                  (latest?.trades_placed ?? 0) > 0
                    ? "text-win"
                    : "text-muted-foreground"
                }`}
              >
                {latest?.trades_placed ?? 0}
              </div>
            </div>
          </div>
          <ol className="mt-3 space-y-1.5 text-[11px]">
            {cycles.length === 0 ? (
              <li className="text-center text-muted-foreground">
                NO_CYCLES_YET
              </li>
            ) : (
              cycles.map((c) => {
                const hasError = !!c.errors;
                const stateClass = hasError
                  ? "text-loss"
                  : c.completed_at
                    ? "text-success"
                    : "text-muted-foreground";
                return (
                  <li
                    key={c.id}
                    data-testid={`row-pipeline-cycle-${c.id}`}
                    className="flex items-center justify-between gap-2 border-b border-border/30 pb-1 last:border-b-0"
                  >
                    <span className="text-muted-foreground tabular-nums">
                      {fmtTime(c.started_at)}
                    </span>
                    <span className="text-primary uppercase tracking-widest">
                      {c.vertical ?? "—"}
                    </span>
                    <span className="tabular-nums">
                      {fmtDuration(c.duration_seconds)}
                    </span>
                    <span className={`uppercase tracking-widest ${stateClass}`}>
                      {hasError ? "ERR" : c.completed_at ? "DONE" : "RUN"}
                    </span>
                  </li>
                );
              })
            )}
          </ol>
        </>
      )}
    </GlassCard>
  );
}
