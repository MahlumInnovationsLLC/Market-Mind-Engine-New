import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

interface V1BacktestGateRun {
  id: number;
  run_at: string;
  overall_verdict: string;
  parity_gate_status: Record<string, unknown>;
  per_league_brier: Record<string, number>;
  regressed_leagues: string[];
  report_path: string | null;
  duration_seconds: number | null;
  notes: string | null;
}

interface V1BacktestGateResponse {
  latest: V1BacktestGateRun | null;
  history: V1BacktestGateRun[];
  error?: string | null;
}

function fmt(n: number | null | undefined, digits = 4): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

function verdictTone(verdict: string, regressed: string[]): {
  label: string;
  cls: string;
  dotCls: string;
} {
  const v = (verdict || "").toUpperCase();
  if (regressed.length > 0 || v === "FAIL" || v === "ERROR") {
    return {
      label: regressed.length > 0 ? "RED — regressed leagues" : v,
      cls: "text-destructive",
      dotCls: "bg-destructive",
    };
  }
  if (v === "PASS") {
    return { label: "PASS", cls: "text-success", dotCls: "bg-success" };
  }
  if (v.startsWith("DEGRADED")) {
    return {
      label: v.replace(/_/g, " "),
      cls: "text-amber-500",
      dotCls: "bg-amber-500",
    };
  }
  return {
    label: v || "—",
    cls: "text-muted-foreground",
    dotCls: "bg-muted-foreground/40",
  };
}

function HistoryDot({ row }: { row: V1BacktestGateRun }) {
  const tone = verdictTone(row.overall_verdict, row.regressed_leagues);
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${tone.dotCls}`}
      title={`${fmtTime(row.run_at)} · ${tone.label} · regressed: ${
        row.regressed_leagues.length === 0
          ? "(none)"
          : row.regressed_leagues.join(", ")
      }`}
      data-testid={`v1-backtest-gate-history-${row.id}`}
    />
  );
}

export default function V1BacktestGateTile() {
  const q = useQuery({
    queryKey: ["dashboard-v1-backtest-gate"],
    queryFn: async () =>
      customFetch<V1BacktestGateResponse>(
        "/api/dashboard/v1-backtest-gate?history_limit=8",
      ),
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000,
  });

  const latest = q.data?.latest ?? null;
  const history = q.data?.history ?? [];

  const tone = useMemo(
    () =>
      latest
        ? verdictTone(latest.overall_verdict, latest.regressed_leagues)
        : verdictTone("", []),
    [latest],
  );

  const sortedLeagues = useMemo(() => {
    if (!latest) return [] as Array<[string, number]>;
    return Object.entries(latest.per_league_brier ?? {}).sort(([a], [b]) =>
      a.localeCompare(b),
    );
  }, [latest]);

  const regressedSet = useMemo(
    () => new Set(latest?.regressed_leagues ?? []),
    [latest],
  );

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-v1-backtest-gate"
    >
      <SectionHeader
        eyebrow="V1 backtest gate"
        title="Weekly parity vs V1 archive"
        subtitle={
          q.data?.error
            ? `Failed to load: ${q.data.error}`
            : latest
              ? `Last run ${fmtTime(latest.run_at)} · ${
                  Object.keys(latest.per_league_brier ?? {}).length
                } leagues scored`
              : "No runs yet — the weekly cron will populate this tile."
        }
      />

      {q.isLoading && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          Loading V1 backtest gate…
        </div>
      )}
      {q.isError && (
        <div className="text-xs text-destructive py-6 text-center">
          Failed to load: {String((q.error as Error)?.message ?? q.error)}
        </div>
      )}

      {!q.isLoading && !q.isError && latest && (
        <div className="flex flex-col gap-3">
          <div className="flex items-baseline justify-between gap-3">
            <div className="flex items-center gap-2">
              <span
                className={`inline-block w-2.5 h-2.5 rounded-full ${tone.dotCls}`}
                data-testid="v1-backtest-gate-verdict-dot"
              />
              <span
                className={`text-sm font-semibold ${tone.cls}`}
                data-testid="v1-backtest-gate-verdict"
              >
                {tone.label}
              </span>
            </div>
            <div className="flex items-center gap-1.5">
              {history.length === 0 ? (
                <span className="text-[10px] text-muted-foreground">
                  no history
                </span>
              ) : (
                history
                  .slice()
                  .reverse()
                  .map((row) => <HistoryDot key={row.id} row={row} />)
              )}
            </div>
          </div>

          {latest.regressed_leagues.length > 0 && (
            <div
              className="text-[11px] text-destructive border border-destructive/40 rounded-md px-2 py-1.5 bg-destructive/5"
              data-testid="v1-backtest-gate-regressed"
            >
              <div className="font-semibold uppercase tracking-widest text-[9px] mb-0.5">
                Regressed leagues (Brier worse vs prior run)
              </div>
              <div>{latest.regressed_leagues.join(", ")}</div>
            </div>
          )}

          {sortedLeagues.length > 0 && (
            <div
              className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-1.5"
              data-testid="v1-backtest-gate-leagues"
            >
              {sortedLeagues.map(([league, brier]) => {
                const regressed = regressedSet.has(league);
                return (
                  <div
                    key={league}
                    className={`border rounded px-2 py-1 text-[10px] flex flex-col ${
                      regressed
                        ? "border-destructive/60 bg-destructive/10"
                        : "border-border/60 bg-card/40"
                    }`}
                    data-testid={`v1-backtest-gate-league-${league}`}
                  >
                    <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
                      {league}
                    </span>
                    <span
                      className={
                        regressed
                          ? "text-destructive font-semibold"
                          : "text-foreground"
                      }
                    >
                      {fmt(brier)}
                    </span>
                  </div>
                );
              })}
            </div>
          )}

          {latest.report_path && (
            <div className="text-[10px] text-muted-foreground truncate">
              report: {latest.report_path}
            </div>
          )}
        </div>
      )}

      {!q.isLoading && !q.isError && !latest && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          No V1 backtest gate runs yet.
        </div>
      )}
    </GlassCard>
  );
}
