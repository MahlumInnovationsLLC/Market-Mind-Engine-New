import { useMemo } from "react";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { apiFetch } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

type TournamentReplayItem = {
  id: number;
  event_id: string;
  league: string;
  event_name: string;
  winner: string | null;
  n_snapshots: number;
  accuracy: number;
  brier_score: number;
  log_loss: number;
  started_at: string | null;
  completed_at: string | null;
};

type TournamentReplaysResponse = {
  items: TournamentReplayItem[];
};

const TREND_LIMIT = 20;

function fmtMetric(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtCompletedAt(raw: string | null): string {
  if (!raw) return "—";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  return d.toLocaleString([], {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export default function TournamentReplaysPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const { data, isLoading, error, refetch, isFetching } =
    useQuery<TournamentReplaysResponse>({
      queryKey: ["dashboard", "tournament-backtest-recent"] as const,
      queryFn: () =>
        apiFetch<TournamentReplaysResponse>(
          "/api/dashboard/tournament-backtest/recent?limit=50",
          { getToken: tokenFn },
        ),
      enabled: !!isSignedIn,
      refetchOnWindowFocus: false,
      staleTime: 60_000,
    });

  const items = data?.items ?? [];

  // Sparkline data: oldest -> newest, capped at TREND_LIMIT so the
  // chart stays compact when many replays accumulate.
  const trend = useMemo(() => {
    const recent = items.slice(0, TREND_LIMIT);
    return [...recent].reverse().map((r, i) => ({
      i,
      label: fmtCompletedAt(r.completed_at),
      accuracy: r.accuracy,
      brier: r.brier_score,
    }));
  }, [items]);

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-tournament-replays"
    >
      <SectionHeader
        eyebrow="ORCHESTRATOR"
        title="Recent tournament replays"
        subtitle="Persisted Sportradar tournament backtests — accuracy, Brier and log-loss across every replayed event so mid-event quote drift is visible at a glance."
        actions={
          <button
            type="button"
            onClick={() => refetch()}
            disabled={isFetching}
            className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground disabled:opacity-50"
            data-testid="button-tournament-replays-refresh"
          >
            {isFetching ? "Refreshing…" : "Refresh"}
          </button>
        }
      />

      {error && (
        <div
          className="mt-3 border border-loss/60 bg-loss/10 p-2 text-xs text-loss"
          data-testid="tournament-replays-error"
        >
          Failed to load tournament replays:{" "}
          {error instanceof Error ? error.message : String(error)}
        </div>
      )}

      {isLoading && !data && (
        <div
          className="mt-3 h-32 animate-pulse bg-muted/10"
          data-testid="tournament-replays-loading"
        />
      )}

      {!isLoading && !error && items.length === 0 && (
        <div
          className="mt-3 py-6 text-center text-[11px] text-muted-foreground"
          data-testid="tournament-replays-empty"
        >
          No tournament replays have been persisted yet. Run a replay from
          the admin tools to see drift here.
        </div>
      )}

      {items.length > 0 && (
        <div className="mt-3 space-y-3">
          <div
            className="h-28 w-full border border-border bg-card/40 p-1"
            data-testid="tournament-replays-trend"
          >
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={trend}
                margin={{ top: 6, right: 8, bottom: 0, left: 8 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="hsl(var(--border))"
                  opacity={0.3}
                />
                <XAxis dataKey="i" hide />
                <YAxis
                  yAxisId="acc"
                  domain={[0, 1]}
                  tick={{ fontSize: 9 }}
                  width={24}
                  stroke="hsl(var(--muted-foreground))"
                />
                <YAxis
                  yAxisId="brier"
                  orientation="right"
                  domain={[0, 1]}
                  tick={{ fontSize: 9 }}
                  width={24}
                  stroke="hsl(var(--muted-foreground))"
                />
                <Tooltip
                  cursor={{ stroke: "hsl(var(--border))" }}
                  contentStyle={{
                    background: "hsl(var(--popover))",
                    border: "1px solid hsl(var(--border))",
                    fontSize: 11,
                  }}
                  labelFormatter={(_, payload) =>
                    payload?.[0]?.payload?.label ?? ""
                  }
                  formatter={(v: number, name: string) => [
                    typeof v === "number" ? v.toFixed(4) : v,
                    name === "accuracy" ? "Accuracy" : "Brier",
                  ]}
                />
                <Line
                  yAxisId="acc"
                  type="monotone"
                  dataKey="accuracy"
                  stroke="hsl(var(--success))"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
                <Line
                  yAxisId="brier"
                  type="monotone"
                  dataKey="brier"
                  stroke="hsl(var(--destructive))"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="flex flex-wrap gap-3 text-[10px] uppercase tracking-widest text-muted-foreground">
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden
                className="inline-block h-0.5 w-3 bg-success"
              />
              Accuracy (top-pick = winner)
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden
                className="inline-block h-0.5 w-3 bg-destructive"
              />
              Brier (lower is better)
            </span>
          </div>

          <div className="border border-border bg-background overflow-x-auto">
            <table
              className="w-full text-xs"
              data-testid="tournament-replays-table"
            >
              <thead className="bg-muted/20 text-[10px] uppercase tracking-widest text-muted-foreground">
                <tr>
                  <th className="px-2 py-1.5 text-left">Event</th>
                  <th className="px-2 py-1.5 text-left">League</th>
                  <th className="px-2 py-1.5 text-left">Winner</th>
                  <th className="px-2 py-1.5 text-right">Snaps</th>
                  <th className="px-2 py-1.5 text-right">Accuracy</th>
                  <th className="px-2 py-1.5 text-right">Brier</th>
                  <th className="px-2 py-1.5 text-right">Log-loss</th>
                  <th className="px-2 py-1.5 text-right">Completed</th>
                </tr>
              </thead>
              <tbody>
                {items.map((r, idx) => (
                  <tr
                    key={r.id}
                    className="border-t border-border"
                    data-testid={`tournament-replays-row-${idx}`}
                  >
                    <td
                      className="px-2 py-1.5 font-mono text-[11px] text-foreground"
                      title={r.event_id}
                    >
                      {r.event_name || r.event_id}
                    </td>
                    <td className="px-2 py-1.5 text-muted-foreground">
                      {r.league || "—"}
                    </td>
                    <td className="px-2 py-1.5 text-foreground">
                      {r.winner || "—"}
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono">
                      {r.n_snapshots.toLocaleString()}
                    </td>
                    <td
                      className="px-2 py-1.5 text-right font-mono"
                      data-testid={`tournament-replays-accuracy-${idx}`}
                    >
                      {fmtPct(r.accuracy)}
                    </td>
                    <td
                      className="px-2 py-1.5 text-right font-mono"
                      data-testid={`tournament-replays-brier-${idx}`}
                    >
                      {fmtMetric(r.brier_score)}
                    </td>
                    <td
                      className="px-2 py-1.5 text-right font-mono"
                      data-testid={`tournament-replays-log-loss-${idx}`}
                    >
                      {fmtMetric(r.log_loss)}
                    </td>
                    <td className="px-2 py-1.5 text-right text-muted-foreground">
                      {fmtCompletedAt(r.completed_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </GlassCard>
  );
}
