import { useCallback, useEffect, useMemo, useState } from "react";
import { useWebSocket } from "@/hooks/use-websocket";

interface RetrainEntry {
  key: string;
  vertical: string;
  receivedAt: number;
  triggeredBy: "alert" | "batch" | string;
  rowsConsumed: number;
  brierBefore: number | null;
  brierAfter: number | null;
  rolledBack: boolean;
  ok: boolean;
  snapshotId: number | null;
}

interface SkipEntry {
  vertical: string;
  unprocessed: number;
  minBatch: number;
  activeAlert: boolean;
  receivedAt: number;
}

interface CycleSummary {
  startedAt: string | null;
  completedAt: string | null;
  durationSeconds: number | null;
  verticalsExamined: number;
  verticalsRetrained: number;
  rowsProcessed: number;
  rollbacks: number;
}

const MAX_RETRAINS = 12;

function pickStr(o: Record<string, unknown>, k: string): string | null {
  const v = o[k];
  return typeof v === "string" ? v : null;
}
function pickNum(o: Record<string, unknown>, k: string): number | null {
  const v = o[k];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function pickBool(o: Record<string, unknown>, k: string): boolean {
  return o[k] === true;
}

function fmtAge(ms: number): string {
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function fmtBrier(v: number | null): string {
  if (v === null) return "—";
  return v.toFixed(4);
}

function fmtDelta(before: number | null, after: number | null): {
  text: string;
  cls: string;
} {
  if (before === null || after === null) {
    return { text: "—", cls: "text-muted-foreground" };
  }
  const d = after - before;
  const sign = d <= 0 ? "" : "+";
  const cls =
    d < -1e-4
      ? "text-win"
      : d > 1e-4
        ? "text-loss"
        : "text-muted-foreground";
  return { text: `${sign}${d.toFixed(4)}`, cls };
}

export default function RecentRetrains() {
  const [retrains, setRetrains] = useState<RetrainEntry[]>([]);
  const [skips, setSkips] = useState<Record<string, SkipEntry>>({});
  const [cycle, setCycle] = useState<CycleSummary | null>(null);
  // Tick once per second so the relative-age labels stay live.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => (n + 1) % 1_000_000), 1000);
    return () => clearInterval(id);
  }, []);

  const onEvent = useCallback((ev: Record<string, unknown>) => {
    const t = pickStr(ev, "type");
    if (!t) return;
    const now = Date.now();

    // Backend emits both `retrain_rolled_back` and `retrain_complete`
    // for rolled-back attempts (the latter carries `rolled_back: true,
    // ok: false`). Listen only to `retrain_complete` so each attempt
    // appears exactly once.
    if (t === "retrain_complete") {
      const vertical = pickStr(ev, "vertical") ?? "—";
      const snapshotId = pickNum(ev, "snapshot_id");
      const entry: RetrainEntry = {
        key:
          snapshotId !== null
            ? `snap-${snapshotId}`
            : `${vertical}-${now}-${Math.random().toString(36).slice(2, 6)}`,
        vertical,
        receivedAt: now,
        triggeredBy: pickStr(ev, "triggered_by") ?? "—",
        rowsConsumed: pickNum(ev, "rows_consumed") ?? 0,
        brierBefore: pickNum(ev, "brier_before"),
        brierAfter: pickNum(ev, "brier_after"),
        rolledBack: pickBool(ev, "rolled_back"),
        ok: pickBool(ev, "ok"),
        snapshotId,
      };
      setRetrains((prev) => {
        const filtered = prev.filter((p) => p.key !== entry.key);
        return [entry, ...filtered].slice(0, MAX_RETRAINS);
      });
      // A successful retrain for a vertical clears any prior skip note.
      setSkips((prev) => {
        if (!(vertical in prev)) return prev;
        const next = { ...prev };
        delete next[vertical];
        return next;
      });
      return;
    }

    if (t === "retrain_skipped") {
      const vertical = pickStr(ev, "vertical") ?? "—";
      setSkips((prev) => ({
        ...prev,
        [vertical]: {
          vertical,
          unprocessed: pickNum(ev, "unprocessed") ?? 0,
          minBatch: pickNum(ev, "min_batch") ?? 0,
          activeAlert: pickBool(ev, "active_alert"),
          receivedAt: now,
        },
      }));
      return;
    }

    if (t === "retrain_cycle_complete") {
      setCycle({
        startedAt: pickStr(ev, "started_at"),
        completedAt: pickStr(ev, "completed_at"),
        durationSeconds: pickNum(ev, "duration_seconds"),
        verticalsExamined: pickNum(ev, "verticals_examined") ?? 0,
        verticalsRetrained: pickNum(ev, "verticals_retrained") ?? 0,
        rowsProcessed: pickNum(ev, "rows_processed") ?? 0,
        rollbacks: pickNum(ev, "rollbacks") ?? 0,
      });
    }
  }, []);

  const ws = useWebSocket({
    path: "/api/ws/settlement",
    onMessage: onEvent,
    bufferSize: 25,
  });

  const live = ws.status === "open";
  const skipList = useMemo(
    () =>
      Object.values(skips).sort((a, b) => b.receivedAt - a.receivedAt),
    [skips],
  );

  return (
    <div
      className="border border-border bg-card flex flex-col"
      data-testid="card-recent-retrains"
    >
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <div className="flex items-center gap-2">
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              live ? "bg-success animate-pulse" : "bg-muted-foreground"
            }`}
          />
          <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
            RECENT_RETRAINS
          </h2>
        </div>
        {cycle && (
          <span
            className="text-[10px] uppercase tracking-widest text-muted-foreground"
            data-testid="text-retrain-cycle-summary"
          >
            CYCLE · {cycle.verticalsRetrained}/{cycle.verticalsExamined} fit
            {cycle.rollbacks > 0 && (
              <span className="text-loss"> · {cycle.rollbacks} rolled back</span>
            )}
            {cycle.durationSeconds !== null && (
              <span> · {cycle.durationSeconds.toFixed(1)}s</span>
            )}
          </span>
        )}
      </div>

      <div className="overflow-auto max-h-72">
        {retrains.length === 0 ? (
          <div className="p-4 text-center text-muted-foreground text-xs">
            NO_RETRAINS_OBSERVED
          </div>
        ) : (
          <ul className="divide-y divide-border/50 text-xs">
            {retrains.map((r) => {
              const delta = fmtDelta(r.brierBefore, r.brierAfter);
              const triggerCls =
                r.triggeredBy === "alert"
                  ? "border-warning text-warning"
                  : "border-border text-muted-foreground";
              const statusCls = r.rolledBack
                ? "border-loss text-loss"
                : r.ok
                  ? "border-success text-success"
                  : "border-border text-muted-foreground";
              const statusText = r.rolledBack
                ? "ROLLED BACK"
                : r.ok
                  ? "PROMOTED"
                  : "FAILED";
              return (
                <li
                  key={r.key}
                  className="p-2 px-3 flex items-start justify-between gap-3"
                  data-testid="row-retrain"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest">
                      <span className="text-primary font-bold">
                        {r.vertical.toUpperCase()}
                      </span>
                      <span
                        className={`border px-1.5 py-0.5 ${triggerCls}`}
                        data-testid="badge-retrain-trigger"
                      >
                        {r.triggeredBy === "alert"
                          ? "DRIFT ALERT"
                          : r.triggeredBy === "batch"
                            ? "BATCH"
                            : (r.triggeredBy ?? "—").toUpperCase()}
                      </span>
                      <span className={`border px-1.5 py-0.5 ${statusCls}`}>
                        {statusText}
                      </span>
                      <span className="text-muted-foreground ml-auto">
                        {fmtAge(r.receivedAt)} ago
                      </span>
                    </div>
                    <div className="mt-1 text-foreground">
                      {r.rowsConsumed.toLocaleString()} rows ·{" "}
                      <span className="text-muted-foreground">
                        Brier {fmtBrier(r.brierBefore)} →{" "}
                        {fmtBrier(r.brierAfter)}
                      </span>{" "}
                      <span
                        className={delta.cls}
                        data-testid="text-retrain-delta"
                      >
                        ({delta.text})
                      </span>
                    </div>
                    {r.snapshotId !== null && (
                      <div className="text-[10px] text-muted-foreground mt-0.5">
                        snapshot #{r.snapshotId}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {skipList.length > 0 && (
        <div className="border-t border-border/50 px-3 py-2">
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-1">
            Buffer pressure (skipped this cycle)
          </div>
          <div className="flex flex-wrap gap-2 text-[10px]">
            {skipList.map((s) => (
              <span
                key={s.vertical}
                className={`border px-2 py-0.5 uppercase tracking-widest ${
                  s.activeAlert
                    ? "border-warning text-warning"
                    : "border-border text-muted-foreground"
                }`}
                data-testid="badge-retrain-skip"
              >
                {s.vertical} {s.unprocessed}/{s.minBatch}
                {s.activeAlert && <span className="ml-1">·ALERT</span>}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
