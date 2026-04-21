import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useListModelHealthAlerts,
  getListModelHealthAlertsQueryKey,
  useAcknowledgeModelHealthAlert,
} from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";

function severityClass(severity: string | null | undefined): string {
  switch ((severity ?? "").toLowerCase()) {
    case "critical":
    case "high":
      return "border-loss text-loss";
    case "warn":
    case "warning":
    case "medium":
      return "border-warning text-warning";
    case "info":
    case "low":
      return "border-primary text-primary";
    default:
      return "border-border text-muted-foreground";
  }
}

function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

export default function ModelHealthAlerts() {
  const queryClient = useQueryClient();
  const params = { limit: 25 };
  const queryKey = getListModelHealthAlertsQueryKey(params);

  const { data, isLoading, isError, refetch } = useListModelHealthAlerts(params, {
    query: { queryKey, refetchInterval: 60_000 },
  });

  const onEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      // Drift / model_health alerts are raised inside the settlement
      // pipeline.  We invalidate on any settlement-complete frame as a
      // simple "something happened, refetch" signal.
      if (
        t === "alerts_raised" ||
        t === "market_settle_complete" ||
        t === "cycle_complete"
      ) {
        queryClient.invalidateQueries({ queryKey });
      }
    },
    [queryClient, queryKey],
  );

  const ws = useWebSocket({
    path: "/api/ws/settlement",
    onMessage: onEvent,
    bufferSize: 25,
  });

  // WS-down → poll faster, same pattern as the settlement panel.
  const fastPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (fastPollTimer.current) {
      clearInterval(fastPollTimer.current);
      fastPollTimer.current = null;
    }
    if (ws.status !== "open") {
      fastPollTimer.current = setInterval(() => void refetch(), 15_000);
    }
    return () => {
      if (fastPollTimer.current) {
        clearInterval(fastPollTimer.current);
        fastPollTimer.current = null;
      }
    };
  }, [ws.status, refetch]);

  const ack = useAcknowledgeModelHealthAlert();
  const [pendingId, setPendingId] = useState<number | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const onAck = useCallback(
    (alertId: number) => {
      setPendingId(alertId);
      setErrorMsg(null);
      ack.mutate(
        { data: { alert_id: alertId } },
        {
          onSuccess: () => {
            setPendingId(null);
            queryClient.invalidateQueries({ queryKey });
          },
          onError: (err: unknown) => {
            setPendingId(null);
            const msg =
              err && typeof err === "object" && "message" in err
                ? String((err as { message?: string }).message)
                : "ack failed";
            setErrorMsg(msg);
          },
        },
      );
    },
    [ack, queryClient, queryKey],
  );

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-48 bg-card" />;
  }
  if (isError || !data) {
    return (
      <div className="border border-destructive p-4 text-destructive h-48 flex items-center justify-center text-xs">
        ERR_MODEL_HEALTH_FAIL
      </div>
    );
  }

  const items = data.items ?? [];
  const live = ws.status === "open";

  return (
    <div className="border border-border bg-card flex flex-col">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <div className="flex items-center gap-2">
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              live ? "bg-success animate-pulse" : "bg-muted-foreground"
            }`}
          />
          <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
            MODEL_HEALTH
          </h2>
        </div>
        <span
          className={`text-[10px] uppercase tracking-widest ${
            items.length === 0 ? "text-success" : "text-loss"
          }`}
          data-testid="text-model-alert-count"
        >
          {items.length === 0 ? "OK" : `${items.length} OPEN`}
        </span>
      </div>

      {errorMsg && (
        <div className="px-3 py-1.5 text-[10px] uppercase tracking-widest text-destructive border-b border-destructive/40">
          {errorMsg}
        </div>
      )}

      <div className="overflow-auto max-h-72">
        {items.length === 0 ? (
          <div className="p-4 text-center text-muted-foreground text-xs">
            NO_OPEN_ALERTS
          </div>
        ) : (
          <ul className="divide-y divide-border/50">
            {items.map((a) => {
              const sev = severityClass(a.severity);
              const busy = pendingId === a.id;
              return (
                <li
                  key={a.id}
                  className="p-2 px-3 flex items-start justify-between gap-3"
                  data-testid="row-model-health-alert"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest">
                      <span className={`border px-1.5 py-0.5 ${sev}`}>
                        {(a.severity ?? "—").toUpperCase()}
                      </span>
                      <span className="text-muted-foreground">
                        {(a.alert_type ?? "—").toUpperCase()}
                      </span>
                      <span className="text-primary">
                        {(a.vertical ?? "—").toUpperCase()}
                      </span>
                      <span className="text-muted-foreground ml-auto">
                        {fmtAge(a.age_seconds)} ago
                      </span>
                    </div>
                    <div className="text-xs text-foreground mt-1 break-words">
                      {a.message ?? `${a.metric_name ?? "metric"}: ${a.metric_value ?? "—"}`}
                    </div>
                    {a.metric_name && (
                      <div className="text-[10px] text-muted-foreground mt-0.5">
                        {a.metric_name}: {a.metric_value ?? "—"}
                        {a.threshold !== null && a.threshold !== undefined
                          ? ` (threshold ${a.threshold})`
                          : ""}
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    disabled={busy || ack.isPending}
                    onClick={() => onAck(a.id)}
                    className="border border-border px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-primary hover:border-primary disabled:opacity-50 shrink-0"
                    data-testid={`button-ack-alert-${a.id}`}
                  >
                    {busy ? "ACK…" : "ACK"}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
