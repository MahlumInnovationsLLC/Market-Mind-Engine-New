import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@clerk/react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { AdminAuditLogEntry } from "@workspace/api-client-react";
import { apiFetch } from "@/lib/api";

const PROMOTE_ACTION = "autoresearch_promote";
const REJECT_ACTION = "autoresearch_reject";

// Operator-selectable chart windows. 14d remains the default to match
// the original behaviour from task #419; 7d focuses on recent activity
// and 30d helps spot longer-term trends (task #568).
const WINDOW_OPTIONS = [7, 14, 30] as const;
type WindowDays = (typeof WINDOW_OPTIONS)[number];
const DEFAULT_WINDOW: WindowDays = 14;
const STORAGE_KEY = "axiom.autoresearch-throughput-window";

// Per-action fetch cap.  The original constant (500 rows over 14 days)
// allowed roughly 36 decisions/day per action — comfortably above the
// observed throughput.  We preserve that headroom across all windows
// by scaling with the selected range, with a floor matching the
// original cap so short windows never regress.
function fetchLimitForWindow(windowDays: WindowDays): number {
  return Math.max(500, windowDays * 50);
}

function isWindowDays(value: unknown): value is WindowDays {
  return (
    typeof value === "number" &&
    (WINDOW_OPTIONS as readonly number[]).includes(value)
  );
}

function loadStoredWindow(): WindowDays {
  if (typeof window === "undefined") return DEFAULT_WINDOW;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_WINDOW;
    const parsed = Number(raw);
    return isWindowDays(parsed) ? parsed : DEFAULT_WINDOW;
  } catch {
    return DEFAULT_WINDOW;
  }
}

type AuditPage = {
  items: AdminAuditLogEntry[];
};

function dayKey(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function shortLabel(d: Date): string {
  return d.toLocaleDateString([], { month: "short", day: "2-digit" });
}

function bucketByDay(
  entries: AdminAuditLogEntry[],
  windowStart: Date,
): Map<string, { promoted: number; rejected: number }> {
  const buckets = new Map<string, { promoted: number; rejected: number }>();
  for (const entry of entries) {
    if (!entry.created_at) continue;
    const ts = new Date(entry.created_at);
    if (Number.isNaN(ts.getTime())) continue;
    if (ts.getTime() < windowStart.getTime()) continue;
    const key = dayKey(ts);
    const slot = buckets.get(key) ?? { promoted: 0, rejected: 0 };
    if (entry.action === PROMOTE_ACTION) slot.promoted += 1;
    else if (entry.action === REJECT_ACTION) slot.rejected += 1;
    buckets.set(key, slot);
  }
  return buckets;
}

/**
 * Compact stacked bar chart of AutoResearch promote vs reject counts
 * over an operator-selectable window (7 / 14 / 30 days).  Reads
 * straight from the existing `admin_audit_log` rows the AutoResearch
 * panel already writes — we issue one filtered query per action so we
 * don't have to scan unrelated audit rows.  Helps operators eyeball
 * whether the discovery pipeline is actually producing useful signal
 * day-over-day (task #419, window toggle from task #568).
 */
export default function AutoResearchThroughputChart() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  // Window toggle, hydrated synchronously from localStorage so a
  // refresh keeps the operator's choice without a flash of the
  // default.  Persisted on change.
  const [windowDays, setWindowDays] = useState<WindowDays>(() =>
    loadStoredWindow(),
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(STORAGE_KEY, String(windowDays));
    } catch {
      // Quota exceeded / private mode — silently ignore; the
      // selection still works for the current session.
    }
  }, [windowDays]);

  const fetchLimit = fetchLimitForWindow(windowDays);

  const promoteQuery = useQuery<AuditPage>({
    queryKey: ["admin", "audit-log", "autoresearch", PROMOTE_ACTION, fetchLimit],
    queryFn: () =>
      apiFetch<AuditPage>(
        `/api/admin/audit-log?action=${PROMOTE_ACTION}&limit=${fetchLimit}`,
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 60_000,
  });
  const rejectQuery = useQuery<AuditPage>({
    queryKey: ["admin", "audit-log", "autoresearch", REJECT_ACTION, fetchLimit],
    queryFn: () =>
      apiFetch<AuditPage>(
        `/api/admin/audit-log?action=${REJECT_ACTION}&limit=${fetchLimit}`,
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 60_000,
  });

  const isLoading = promoteQuery.isLoading || rejectQuery.isLoading;
  const isError = promoteQuery.isError || rejectQuery.isError;

  const { series, totals } = useMemo(() => {
    // Anchor the window at today (local) and walk back N-1 days so the
    // chart always renders a fixed-width strip even when there is zero
    // throughput on some days.
    const now = new Date();
    const windowStart = new Date(now);
    windowStart.setHours(0, 0, 0, 0);
    windowStart.setDate(windowStart.getDate() - (windowDays - 1));

    const merged = [
      ...(promoteQuery.data?.items ?? []),
      ...(rejectQuery.data?.items ?? []),
    ];
    const buckets = bucketByDay(merged, windowStart);

    const out: Array<{
      key: string;
      label: string;
      promoted: number;
      rejected: number;
    }> = [];
    let totalPromoted = 0;
    let totalRejected = 0;
    for (let i = 0; i < windowDays; i += 1) {
      const d = new Date(windowStart);
      d.setDate(windowStart.getDate() + i);
      const key = dayKey(d);
      const slot = buckets.get(key) ?? { promoted: 0, rejected: 0 };
      totalPromoted += slot.promoted;
      totalRejected += slot.rejected;
      out.push({
        key,
        label: shortLabel(d),
        promoted: slot.promoted,
        rejected: slot.rejected,
      });
    }
    return { series: out, totals: { promoted: totalPromoted, rejected: totalRejected } };
  }, [promoteQuery.data, rejectQuery.data, windowDays]);

  const hasAny = totals.promoted > 0 || totals.rejected > 0;

  // Per-day averages alongside the totals so operators can compare
  // activity across windows without doing the math in their head
  // (task #635).  `windowDays` is always one of WINDOW_OPTIONS so the
  // divisor is never zero, but guard anyway and let days with no
  // throughput contribute a clean 0 to the average.
  const perDayDivisor = windowDays > 0 ? windowDays : 1;
  const promotedPerDay = totals.promoted / perDayDivisor;
  const rejectedPerDay = totals.rejected / perDayDivisor;
  const formatPerDay = (value: number) =>
    Number.isFinite(value) ? value.toFixed(1) : "0.0";

  return (
    <div
      className="mb-3 border border-border bg-card/40 p-3"
      data-testid="card-autoresearch-throughput"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            THROUGHPUT
          </div>
          <div className="text-xs text-foreground">
            Promote vs reject · last {windowDays} days
          </div>
        </div>
        <div className="flex items-center gap-3 text-[10px] uppercase tracking-widest">
          <div
            className="flex border border-border"
            role="group"
            aria-label="Throughput chart window"
            data-testid="group-autoresearch-throughput-window"
          >
            {WINDOW_OPTIONS.map((opt) => {
              const active = opt === windowDays;
              return (
                <button
                  key={opt}
                  type="button"
                  onClick={() => setWindowDays(opt)}
                  aria-pressed={active}
                  className={`px-2 py-0.5 text-[10px] uppercase tracking-widest transition-colors ${
                    active
                      ? "bg-foreground text-background"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                  data-testid={`button-autoresearch-throughput-window-${opt}d`}
                >
                  {opt}D
                </button>
              );
            })}
          </div>
          <span className="flex items-center gap-1 text-success">
            <span className="inline-block h-2 w-2 bg-success" />
            <span data-testid="text-autoresearch-throughput-promoted">
              {totals.promoted} PROMOTED
            </span>
            <span
              className="text-muted-foreground"
              data-testid="text-autoresearch-throughput-promoted-per-day"
            >
              ({formatPerDay(promotedPerDay)}/DAY)
            </span>
          </span>
          <span className="flex items-center gap-1 text-destructive">
            <span className="inline-block h-2 w-2 bg-destructive" />
            <span data-testid="text-autoresearch-throughput-rejected">
              {totals.rejected} REJECTED
            </span>
            <span
              className="text-muted-foreground"
              data-testid="text-autoresearch-throughput-rejected-per-day"
            >
              ({formatPerDay(rejectedPerDay)}/DAY)
            </span>
          </span>
        </div>
      </div>
      {isLoading ? (
        <div
          className="h-24 animate-pulse bg-muted/10"
          data-testid="skeleton-autoresearch-throughput"
        />
      ) : isError ? (
        <div
          className="border border-destructive/40 p-3 text-center text-[10px] uppercase tracking-widest text-destructive"
          data-testid="text-autoresearch-throughput-error"
        >
          ERR_AUTORESEARCH_THROUGHPUT_LOAD
        </div>
      ) : !hasAny ? (
        <div
          className="border border-border bg-card/60 p-3 text-center text-[10px] uppercase tracking-widest text-muted-foreground"
          data-testid="text-autoresearch-throughput-empty"
        >
          NO_DECISIONS_IN_WINDOW
        </div>
      ) : (
        <div className="h-24 w-full" data-testid="chart-autoresearch-throughput">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={series}
              margin={{ top: 4, right: 4, bottom: 0, left: -24 }}
            >
              <CartesianGrid
                stroke="hsl(var(--border))"
                strokeDasharray="2 4"
                vertical={false}
              />
              <XAxis
                dataKey="label"
                stroke="hsl(var(--muted-foreground))"
                tick={{ fontSize: 9 }}
                tickLine={false}
                axisLine={false}
                interval="preserveStartEnd"
              />
              <YAxis
                stroke="hsl(var(--muted-foreground))"
                tick={{ fontSize: 9 }}
                tickLine={false}
                axisLine={false}
                allowDecimals={false}
                width={28}
              />
              <Tooltip
                cursor={{ fill: "hsl(var(--muted) / 0.2)" }}
                contentStyle={{
                  background: "hsl(var(--popover))",
                  border: "1px solid hsl(var(--border))",
                  fontSize: 11,
                }}
                labelStyle={{ color: "hsl(var(--foreground))" }}
                formatter={(value: number, name: string) => [
                  value,
                  name === "promoted" ? "Promoted" : "Rejected",
                ]}
              />
              <Bar
                dataKey="promoted"
                stackId="decisions"
                fill="hsl(var(--success))"
                isAnimationActive={false}
              />
              <Bar
                dataKey="rejected"
                stackId="decisions"
                fill="hsl(var(--destructive))"
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
