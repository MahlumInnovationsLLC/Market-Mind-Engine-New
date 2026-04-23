import { useMemo, useState } from "react";
import { useLocation } from "wouter";
import { Bell, AlertTriangle, Check } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useListModelHealthAlerts,
  useAcknowledgeModelHealthAlert,
  getListModelHealthAlertsQueryKey,
  type ModelHealthAlert,
} from "@workspace/api-client-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const ALERTS_PARAMS = { ack: false, limit: 50 } as const;

function severityClass(severity?: string | null): string {
  switch ((severity ?? "").toLowerCase()) {
    case "critical":
      return "text-loss border-loss/40 bg-loss/10";
    case "high":
      return "text-loss border-loss/30 bg-loss/5";
    case "warning":
    case "medium":
      return "text-warning border-warning/30 bg-warning/5";
    default:
      return "text-info border-info/30 bg-info/5";
  }
}

function formatAge(seconds?: number): string {
  if (!seconds || seconds < 0) return "now";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

/**
 * Header bell that opens a dropdown of unread model-health alerts.
 * Each row shows severity + message + age + ack button. Clicking the
 * bell itself just toggles the menu; "View all" jumps to the
 * notifications settings page.
 */
export function AlertsBell() {
  const [, setLocation] = useLocation();
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();

  const alerts = useListModelHealthAlerts(ALERTS_PARAMS, {
    query: {
      queryKey: getListModelHealthAlertsQueryKey(ALERTS_PARAMS),
      staleTime: 30_000,
      refetchInterval: 30_000,
    },
  });

  const ack = useAcknowledgeModelHealthAlert({
    mutation: {
      onSuccess: () => {
        queryClient.invalidateQueries({
          queryKey: getListModelHealthAlertsQueryKey(ALERTS_PARAMS),
        });
      },
    },
  });

  const items: ModelHealthAlert[] = useMemo(
    () => alerts.data?.items ?? [],
    [alerts.data],
  );
  const unread = items.length;

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="relative p-2 text-muted-foreground hover:text-primary rounded-sm"
          data-testid="button-shell-alerts"
          aria-label={unread > 0 ? `${unread} unread alerts` : "Alerts"}
          title={unread > 0 ? `${unread} unread alerts` : "Alerts"}
        >
          <Bell className="w-4 h-4" />
          {unread > 0 ? (
            <span
              className="absolute top-1 right-1 min-w-[14px] h-[14px] px-0.5 rounded-full bg-loss text-[9px] text-loss-foreground font-bold flex items-center justify-center"
              data-testid="badge-shell-alerts"
            >
              {unread > 99 ? "99+" : unread}
            </span>
          ) : null}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-[340px] p-0 font-mono"
        data-testid="dropdown-shell-alerts"
      >
        <div className="flex items-center justify-between px-3 py-2 border-b border-border">
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Alerts
          </div>
          <div className="text-[10px] text-muted-foreground">
            {unread} unread
          </div>
        </div>
        <div className="max-h-[360px] overflow-y-auto">
          {items.length === 0 ? (
            <div
              className="px-3 py-6 text-center text-[11px] text-muted-foreground"
              data-testid="empty-shell-alerts"
            >
              {alerts.isLoading ? "Loading…" : "No unread alerts"}
            </div>
          ) : (
            items.slice(0, 10).map((alert) => (
              <div
                key={alert.id}
                className="px-3 py-2 border-b border-border/40 last:border-b-0"
                data-testid={`row-shell-alert-${alert.id}`}
              >
                <div className="flex items-start gap-2">
                  <AlertTriangle
                    className={`w-3 h-3 mt-0.5 shrink-0 ${
                      severityClass(alert.severity).split(" ")[0]
                    }`}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 text-[9px] uppercase tracking-widest">
                      <span
                        className={`px-1 py-0.5 rounded-sm border ${severityClass(
                          alert.severity,
                        )}`}
                      >
                        {alert.severity ?? "info"}
                      </span>
                      {alert.vertical ? (
                        <span className="text-muted-foreground">
                          {alert.vertical}
                        </span>
                      ) : null}
                      <span className="ml-auto text-muted-foreground">
                        {formatAge(alert.age_seconds)}
                      </span>
                    </div>
                    <div className="mt-1 text-[11px] text-foreground normal-case break-words">
                      {alert.message ?? alert.alert_type ?? "Alert"}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      ack.mutate({ data: { alert_id: alert.id } });
                    }}
                    disabled={ack.isPending}
                    className="shrink-0 p-1 text-muted-foreground hover:text-primary disabled:opacity-40"
                    title="Acknowledge"
                    data-testid={`button-shell-alert-ack-${alert.id}`}
                    aria-label="Acknowledge alert"
                  >
                    <Check className="w-3 h-3" />
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
        <div className="border-t border-border">
          <button
            type="button"
            className="w-full px-3 py-2 text-[10px] uppercase tracking-widest text-muted-foreground hover:text-primary text-left"
            onClick={() => {
              setOpen(false);
              setLocation("/settings/notifications");
            }}
            data-testid="button-shell-alerts-view-all"
          >
            View all notifications →
          </button>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
