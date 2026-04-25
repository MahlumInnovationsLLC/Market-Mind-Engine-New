import { useMemo } from "react";
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
const WINDOW_DAYS = 14;
const FETCH_LIMIT = 500;

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
 * Compact stacked bar chart of AutoResearch promote vs reject counts over
 * the last {@link WINDOW_DAYS} days.  Reads straight from the existing
 * `admin_audit_log` rows the AutoResearch panel already writes — we issue
 * one filtered query per action so we don't have to scan unrelated audit
 * rows.  Helps operators eyeball whether the discovery pipeline is
 * actually producing useful signal day-over-day (task #419).
 */
export default function AutoResearchThroughputChart() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const promoteQuery = useQuery<AuditPage>({
    queryKey: ["admin", "audit-log", "autoresearch", PROMOTE_ACTION, FETCH_LIMIT],
    queryFn: () =>
      apiFetch<AuditPage>(
        `/api/admin/audit-log?action=${PROMOTE_ACTION}&limit=${FETCH_LIMIT}`,
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 60_000,
  });
  const rejectQuery = useQuery<AuditPage>({
    queryKey: ["admin", "audit-log", "autoresearch", REJECT_ACTION, FETCH_LIMIT],
    queryFn: () =>
      apiFetch<AuditPage>(
        `/api/admin/audit-log?action=${REJECT_ACTION}&limit=${FETCH_LIMIT}`,
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
    windowStart.setDate(windowStart.getDate() - (WINDOW_DAYS - 1));

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
    for (let i = 0; i < WINDOW_DAYS; i += 1) {
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
  }, [promoteQuery.data, rejectQuery.data]);

  const hasAny = totals.promoted > 0 || totals.rejected > 0;

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
            Promote vs reject · last {WINDOW_DAYS} days
          </div>
        </div>
        <div className="flex items-center gap-3 text-[10px] uppercase tracking-widest">
          <span className="flex items-center gap-1 text-success">
            <span className="inline-block h-2 w-2 bg-success" />
            <span data-testid="text-autoresearch-throughput-promoted">
              {totals.promoted} PROMOTED
            </span>
          </span>
          <span className="flex items-center gap-1 text-destructive">
            <span className="inline-block h-2 w-2 bg-destructive" />
            <span data-testid="text-autoresearch-throughput-rejected">
              {totals.rejected} REJECTED
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
