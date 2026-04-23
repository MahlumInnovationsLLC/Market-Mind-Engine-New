import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

function fmtRemaining(seconds: number): string {
  if (seconds <= 0) return "0s";
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h${Math.ceil((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d`;
}

const SNOOZE_PRESETS: Array<{ label: string; minutes: number }> = [
  { label: "15m", minutes: 15 },
  { label: "1h", minutes: 60 },
  { label: "4h", minutes: 240 },
];

export default function ModelHealthAlerts() {
  const queryClient = useQueryClient();
  // Fetch with ack=true so snoozed (but not yet acknowledged) alerts are
  // included in the response — we display them with a remaining countdown
  // until their snooze window expires.
  const params = { limit: 50, ack: true };
  const queryKey = getListModelHealthAlertsQueryKey(params);

  const { data, isLoading, isError, refetch } = useListModelHealthAlerts(params, {
    query: { queryKey, refetchInterval: 60_000 },
  });

  const onEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
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
  const [openMenuId, setOpenMenuId] = useState<number | null>(null);

  // Tick once per second so the snooze countdown stays fresh and rows
  // re-appear automatically when the window expires.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const onAck = useCallback(
    (alertId: number, snoozeMinutes?: number) => {
      setPendingId(alertId);
      setErrorMsg(null);
      setOpenMenuId(null);
      ack.mutate(
        {
          data: {
            alert_id: alertId,
            ...(snoozeMinutes ? { snooze_minutes: snoozeMinutes } : {}),
          },
        },
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

  // Close any open snooze menu when clicking outside.
  useEffect(() => {
    if (openMenuId === null) return;
    const onDocClick = () => setOpenMenuId(null);
    window.addEventListener("click", onDocClick);
    return () => window.removeEventListener("click", onDocClick);
  }, [openMenuId]);

  const visibleItems = useMemo(() => {
    const all = data?.items ?? [];
    // Hide permanently-acknowledged rows; keep open + snoozed.
    return all.filter((a) => !a.acknowledged);
  }, [data]);

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

  const items = visibleItems;
  const openCount = items.filter((a) => {
    const su = a.snooze_until ? Date.parse(a.snooze_until) : 0;
    return !su || su <= now;
  }).length;
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
            openCount === 0 ? "text-success" : "text-loss"
          }`}
          data-testid="text-model-alert-count"
        >
          {openCount === 0 ? "OK" : `${openCount} OPEN`}
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
              const snoozeUntilMs = a.snooze_until ? Date.parse(a.snooze_until) : 0;
              const snoozedRemainingMs = snoozeUntilMs - now;
              const isSnoozed = snoozedRemainingMs > 0;
              const menuOpen = openMenuId === a.id;
              return (
                <li
                  key={a.id}
                  className="p-2 px-3 flex items-start justify-between gap-3"
                  data-testid="row-model-health-alert"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest flex-wrap">
                      <span className={`border px-1.5 py-0.5 ${sev}`}>
                        {(a.severity ?? "—").toUpperCase()}
                      </span>
                      <span className="text-muted-foreground">
                        {(a.alert_type ?? "—").toUpperCase()}
                      </span>
                      <span className="text-primary">
                        {(a.vertical ?? "—").toUpperCase()}
                      </span>
                      {isSnoozed && (
                        <span
                          className="border border-warning text-warning px-1.5 py-0.5"
                          data-testid={`text-snooze-remaining-${a.id}`}
                        >
                          SNOOZED · {fmtRemaining(snoozedRemainingMs / 1000)}
                        </span>
                      )}
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
                  <div className="relative shrink-0 flex items-stretch gap-1">
                    <button
                      type="button"
                      disabled={busy || ack.isPending || isSnoozed}
                      onClick={() => onAck(a.id)}
                      className="border border-border px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-primary hover:border-primary disabled:opacity-50"
                      data-testid={`button-ack-alert-${a.id}`}
                    >
                      {busy && pendingId === a.id ? "ACK…" : "ACK"}
                    </button>
                    <button
                      type="button"
                      disabled={busy || ack.isPending || isSnoozed}
                      onClick={(e) => {
                        e.stopPropagation();
                        setOpenMenuId(menuOpen ? null : a.id);
                      }}
                      className="border border-border px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-warning hover:border-warning disabled:opacity-50"
                      data-testid={`button-snooze-alert-${a.id}`}
                      aria-haspopup="menu"
                      aria-expanded={menuOpen}
                    >
                      SNOOZE ▾
                    </button>
                    {menuOpen && (
                      <div
                        className="absolute right-0 top-full mt-1 z-10 border border-border bg-card shadow-lg flex flex-col min-w-[7rem]"
                        onClick={(e) => e.stopPropagation()}
                        role="menu"
                        data-testid={`menu-snooze-alert-${a.id}`}
                      >
                        {SNOOZE_PRESETS.map((p) => (
                          <button
                            key={p.minutes}
                            type="button"
                            onClick={() => onAck(a.id, p.minutes)}
                            className="px-3 py-1.5 text-left text-[10px] uppercase tracking-widest text-muted-foreground hover:text-warning hover:bg-muted/30"
                            data-testid={`button-snooze-alert-${a.id}-${p.minutes}m`}
                            role="menuitem"
                          >
                            SNOOZE {p.label}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
