import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  useGetSettlementStatus,
  getGetSettlementStatusQueryKey,
} from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";

function fmtMoney(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const s = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
  }).format(Math.abs(n));
  return n < 0 ? `-${s}` : n > 0 ? `+${s}` : s;
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

export default function SettlementPanel() {
  const queryClient = useQueryClient();
  const queryKey = getGetSettlementStatusQueryKey();

  // Pure-poll fallback when WS is not open (refetchInterval kicks in
  // any time tanstack query thinks the data is stale).  When the WS
  // is connected we still want the polled snapshot every 60s as a
  // belt-and-braces refresh of the today-summary aggregates.
  const { data, isLoading, isError, refetch } = useGetSettlementStatus({
    query: { queryKey, refetchInterval: 60_000 },
  });

  // Refresh aggregates whenever we see a new settlement event over
  // the WS.  Cheap because the response is small.
  const onEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      if (
        t === "market_settle_complete" ||
        t === "trade_settled" ||
        t === "cycle_complete" ||
        t === "alerts_raised"
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

  // When the socket drops, fall back to a faster poll cadence so the
  // operator still sees fresh data within ~10s.  Restored to the slow
  // 60s cadence as soon as the socket reconnects.
  const fastPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (fastPollTimer.current) {
      clearInterval(fastPollTimer.current);
      fastPollTimer.current = null;
    }
    if (ws.status !== "open") {
      fastPollTimer.current = setInterval(() => void refetch(), 10_000);
    }
    return () => {
      if (fastPollTimer.current) {
        clearInterval(fastPollTimer.current);
        fastPollTimer.current = null;
      }
    };
  }, [ws.status, refetch]);

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-72 bg-card" />;
  }
  if (isError || !data) {
    return (
      <div className="border border-destructive p-4 text-destructive h-72 flex items-center justify-center text-xs">
        ERR_SETTLEMENT_FAIL
      </div>
    );
  }

  const live = ws.status === "open";
  const last = (data.last_cycle ?? {}) as Record<string, unknown>;
  const today = data.today;
  const recents = data.recent_settlements ?? [];

  const lastStarted =
    typeof last["started_at"] === "string" ? (last["started_at"] as string) : null;
  const lastTrades =
    typeof last["trades_settled"] === "number"
      ? (last["trades_settled"] as number)
      : 0;
  const lastMarkets =
    typeof last["markets_settled"] === "number"
      ? (last["markets_settled"] as number)
      : 0;
  const lastAlerts =
    typeof last["alerts_raised"] === "number"
      ? (last["alerts_raised"] as number)
      : 0;
  const lastDuration =
    typeof last["duration_seconds"] === "number"
      ? (last["duration_seconds"] as number)
      : null;
  const lastErrors = Array.isArray(last["errors"]) ? (last["errors"] as string[]) : [];

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
            SETTLEMENT
          </h2>
        </div>
        <span
          className={`text-[10px] uppercase tracking-widest ${
            data.enabled
              ? data.running
                ? "text-success"
                : "text-warning"
              : "text-muted-foreground"
          }`}
          data-testid="text-settlement-scheduler-status"
        >
          {data.enabled
            ? data.running
              ? `LIVE • ${data.interval_seconds ?? 0}s`
              : "STOPPED"
            : "DISABLED"}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 p-3 text-xs border-b border-border/50">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            TODAY
          </div>
          <div
            className="text-lg font-bold text-foreground"
            data-testid="text-settlement-today-trades"
          >
            {today?.trades_settled ?? 0}
            <span className="text-[10px] text-muted-foreground ml-1">trades</span>
          </div>
          <div
            className={`text-xs font-bold ${
              (today?.realized_pnl ?? 0) > 0
                ? "text-win"
                : (today?.realized_pnl ?? 0) < 0
                  ? "text-loss"
                  : "text-muted-foreground"
            }`}
            data-testid="text-settlement-today-pnl"
          >
            {fmtMoney(today?.realized_pnl)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            LAST CYCLE
          </div>
          <div className="text-foreground">
            {lastStarted ? fmtTime(lastStarted) : "—"}
            {lastDuration !== null && (
              <span className="text-muted-foreground"> • {lastDuration.toFixed(1)}s</span>
            )}
          </div>
          <div className="text-muted-foreground text-[10px] uppercase tracking-widest">
            {lastMarkets} mkts • {lastTrades} trades
            {lastAlerts > 0 && (
              <span className="text-loss ml-1">• {lastAlerts} alerts</span>
            )}
          </div>
          {lastErrors.length > 0 && (
            <div className="text-loss text-[10px] truncate" title={lastErrors.join("\n")}>
              ERR: {lastErrors[0]}
            </div>
          )}
        </div>
      </div>

      {today && today.by_vertical && today.by_vertical.length > 0 && (
        <div className="px-3 py-2 border-b border-border/50">
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-1">
            By Vertical (today)
          </div>
          <div className="flex flex-wrap gap-2 text-[10px]">
            {today.by_vertical.map((v, idx) => (
              <span
                key={`${v.vertical ?? "unk"}-${idx}`}
                className="border border-border px-2 py-0.5 uppercase tracking-widest"
              >
                {v.vertical ?? "—"}{" "}
                <span className="text-muted-foreground">{v.trades_settled}</span>{" "}
                <span
                  className={
                    v.realized_pnl > 0
                      ? "text-win"
                      : v.realized_pnl < 0
                        ? "text-loss"
                        : "text-muted-foreground"
                  }
                >
                  {fmtMoney(v.realized_pnl)}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-col">
        <div className="px-3 py-1.5 text-[10px] uppercase tracking-widest text-muted-foreground border-b border-border/50">
          Recent Markets Settled
        </div>
        <div className="overflow-auto max-h-56">
          {recents.length === 0 ? (
            <div className="p-3 text-center text-muted-foreground text-xs">
              NO_RECENT_SETTLEMENTS
            </div>
          ) : (
            <ul className="divide-y divide-border/50 text-xs">
              {recents.slice(0, 5).map((r, idx) => {
                const verticals = Array.isArray(r.verticals) ? r.verticals : [];
                const trades =
                  typeof r.trades_settled === "number" ? r.trades_settled : 0;
                const alerts =
                  typeof r.alerts_raised === "number" ? r.alerts_raised : 0;
                const outcome = r.outcome_yes === 1 ? "YES" : r.outcome_yes === 0 ? "NO" : "—";
                return (
                  <li
                    key={`${r.market_id ?? "m"}-${idx}`}
                    className="p-2 px-3 flex items-center justify-between gap-2 hover:bg-muted/30"
                    data-testid="row-settlement-recent"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-foreground" title={r.market_id ?? ""}>
                        {r.market_id ?? "—"}
                      </div>
                      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                        {verticals.join(", ") || "—"}
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <div
                        className={`text-[10px] uppercase tracking-widest font-bold ${
                          outcome === "YES" ? "text-win" : outcome === "NO" ? "text-loss" : "text-muted-foreground"
                        }`}
                      >
                        {outcome}
                      </div>
                      <div className="text-[10px] text-muted-foreground">
                        {trades} trades
                        {alerts > 0 && <span className="text-loss"> • {alerts} alerts</span>}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
