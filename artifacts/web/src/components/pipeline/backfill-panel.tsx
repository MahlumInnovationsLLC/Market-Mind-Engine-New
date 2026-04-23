import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/react";
import {
  getGetMyAccessQueryKey,
  useGetMyAccess,
} from "@workspace/api-client-react";
import { ApiError, apiFetch, formatErrorMessage } from "@/lib/api";

interface BackfillStatus {
  state: "idle" | "running" | "completed" | "failed" | string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number;
  source: "kalshi" | "csv" | string | null;
  markets_total: number;
  markets_processed: number;
  markets_settled: number;
  markets_skipped_unresolved: number;
  markets_failed: number;
  trades_settled: number;
  persona_metrics_updated: number;
  alerts_raised: number;
  current_market: string | null;
  errors: string[];
  last_error: string | null;
}

const STATE_TONE: Record<string, string> = {
  idle: "text-muted-foreground",
  running: "text-primary animate-pulse",
  completed: "text-success",
  failed: "text-destructive",
};

function fmtTime(s: string | null): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}m ${s.toString().padStart(2, "0")}s`;
}

export default function BackfillPanel() {
  const { getToken, isSignedIn } = useAuth();
  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });
  const isAdmin = !!access?.is_admin;

  const [status, setStatus] = useState<BackfillStatus | null>(null);
  const [csvPath, setCsvPath] = useState("");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  const isRunning = status?.state === "running";
  const isRunningRef = useRef(false);
  isRunningRef.current = isRunning;

  const fetchStatus = useCallback(async () => {
    if (!isSignedIn) return;
    try {
      const next = await apiFetch<BackfillStatus>(
        "/api/settlement/backfill-status",
        { getToken },
      );
      setStatus(next);
      setStatusError(null);
    } catch (e) {
      setStatusError(formatErrorMessage(e));
    }
  }, [getToken, isSignedIn]);

  // Initial fetch + polling. Poll every 1.5s while running, every 30s
  // while idle/completed so a fresh load reflects the last run quickly
  // without hammering the API in steady state.
  useEffect(() => {
    if (!isSignedIn) return;
    fetchStatus();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      if (cancelled) return;
      await fetchStatus();
      if (cancelled) return;
      const interval = isRunningRef.current ? 1500 : 30_000;
      timer = setTimeout(tick, interval);
    };
    timer = setTimeout(tick, 1500);
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [fetchStatus, isSignedIn]);

  const start = useCallback(async () => {
    if (!isAdmin || isRunning || starting) return;
    setStarting(true);
    setStartError(null);
    try {
      const trimmed = csvPath.trim();
      const body: Record<string, unknown> = {};
      if (trimmed) body.csv_path = trimmed;
      const res = await apiFetch<{ started: boolean; status: BackfillStatus }>(
        "/api/settlement/backfill",
        { method: "POST", body, getToken },
      );
      if (res?.status) setStatus(res.status);
      // Trigger an immediate refresh so progress shows up right away.
      setTimeout(() => {
        fetchStatus();
      }, 500);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setStartError(
          formatErrorMessage(e, "A backfill is already running."),
        );
        fetchStatus();
      } else {
        setStartError(formatErrorMessage(e));
      }
    } finally {
      setStarting(false);
    }
  }, [csvPath, fetchStatus, getToken, isAdmin, isRunning, starting]);

  const total = status?.markets_total ?? 0;
  const processed = status?.markets_processed ?? 0;
  const pct = total > 0 ? Math.min(100, (processed / total) * 100) : 0;
  const stateLabel = status?.state ?? "idle";
  const stateTone = STATE_TONE[stateLabel] ?? "text-foreground";

  const startDisabled = !isAdmin || isRunning || starting || !isSignedIn;
  const startTitle = !isAdmin
    ? "Admin only"
    : isRunning
      ? "Backfill already running"
      : starting
        ? "Starting…"
        : "Trigger a historical settlement backfill";

  return (
    <div
      className="border border-border bg-card font-mono"
      data-testid="card-backfill-panel"
    >
      <div className="border-b border-border bg-muted/20 px-3 py-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
            Backfill historical settlements
          </h2>
          {!isAdmin && (
            <span className="border border-border px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-muted-foreground">
              ADMIN ONLY
            </span>
          )}
        </div>
        <span
          className={`text-[10px] uppercase tracking-widest ${stateTone}`}
          data-testid="text-backfill-state"
        >
          {stateLabel}
        </span>
      </div>

      <div className="p-3 space-y-3 text-[11px]">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
          <label className="flex-1 flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
              CSV override path (optional)
            </span>
            <input
              type="text"
              value={csvPath}
              onChange={(e) => setCsvPath(e.target.value)}
              placeholder="/path/to/overrides.csv"
              disabled={!isAdmin || isRunning}
              spellCheck={false}
              autoComplete="off"
              className="border border-border bg-background px-2 py-1 text-xs font-mono outline-none focus:border-primary disabled:opacity-50"
              data-testid="input-backfill-csv-path"
            />
          </label>
          <button
            type="button"
            onClick={start}
            disabled={startDisabled}
            title={startTitle}
            className="border border-primary bg-primary/10 text-primary px-3 py-1 text-xs uppercase tracking-widest hover:bg-primary/20 disabled:opacity-40 disabled:cursor-not-allowed"
            data-testid="button-backfill-start"
          >
            {isRunning ? "Running…" : starting ? "Starting…" : "Start"}
          </button>
        </div>

        {startError && (
          <div
            className="text-[10px] text-destructive uppercase tracking-widest"
            data-testid="text-backfill-start-error"
          >
            ERR · {startError}
          </div>
        )}
        {statusError && (
          <div
            className={`text-[10px] uppercase tracking-widest ${status ? "text-warning" : "text-destructive"}`}
            data-testid="text-backfill-status-error"
            title={statusError}
          >
            STATUS · {status ? "STALE" : statusError}
          </div>
        )}

        <div>
          <div className="flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground mb-1">
            <span>
              PROGRESS · {processed}/{total || "—"}
            </span>
            <span data-testid="text-backfill-progress-pct">
              {total > 0 ? `${pct.toFixed(0)}%` : "—"}
            </span>
          </div>
          <div className="h-1.5 w-full bg-border/40 overflow-hidden">
            <div
              className={`h-full ${isRunning ? "bg-primary" : status?.state === "failed" ? "bg-destructive" : "bg-success"}`}
              style={{ width: `${pct}%` }}
              data-testid="bar-backfill-progress"
            />
          </div>
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[10px] uppercase tracking-widest sm:grid-cols-4">
          <div>
            <dt className="text-muted-foreground">Settled</dt>
            <dd className="text-foreground font-bold" data-testid="text-backfill-trades-settled">
              {status?.trades_settled ?? 0}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Personas</dt>
            <dd className="text-foreground font-bold" data-testid="text-backfill-personas-updated">
              {status?.persona_metrics_updated ?? 0}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Unresolved</dt>
            <dd className="text-foreground font-bold" data-testid="text-backfill-unresolved">
              {status?.markets_skipped_unresolved ?? 0}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Failed</dt>
            <dd
              className={`font-bold ${(status?.markets_failed ?? 0) > 0 ? "text-destructive" : "text-foreground"}`}
              data-testid="text-backfill-failed"
            >
              {status?.markets_failed ?? 0}
            </dd>
          </div>
        </dl>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[10px] uppercase tracking-widest sm:grid-cols-3">
          <div>
            <dt className="text-muted-foreground">Source</dt>
            <dd className="text-foreground">{status?.source ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Started</dt>
            <dd className="text-foreground">{fmtTime(status?.started_at ?? null)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">
              {status?.state === "running" ? "Elapsed" : "Duration"}
            </dt>
            <dd className="text-foreground">
              {fmtDuration(status?.duration_seconds ?? 0)}
            </dd>
          </div>
        </dl>

        {status?.current_market && isRunning && (
          <div
            className="text-[10px] uppercase tracking-widest text-muted-foreground truncate"
            title={status.current_market}
            data-testid="text-backfill-current-market"
          >
            CURRENT · <span className="text-primary">{status.current_market}</span>
          </div>
        )}

        {status?.last_error && (
          <div
            className="text-[10px] text-destructive uppercase tracking-widest truncate"
            title={status.last_error}
            data-testid="text-backfill-last-error"
          >
            LAST_ERR · {status.last_error}
          </div>
        )}
      </div>
    </div>
  );
}
