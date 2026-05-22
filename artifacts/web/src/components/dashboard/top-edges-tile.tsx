import { useState } from "react";
import {
  getGetDashboardTopEdgesQueryKey,
  useGetDashboardTopEdges,
  useSnoozeDashboardCandidate,
  useFireDashboardCandidate,
} from "@workspace/api-client-react";
import { useQueryClient } from "@tanstack/react-query";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function fmtPct(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${Number(n).toFixed(digits)}%`;
}
function fmtProb(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return (Number(n) * 100).toFixed(1) + "%";
}
function fmtPrice(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toFixed(2);
}
function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleTimeString();
  } catch {
    return s;
  }
}

export default function TopEdgesTile() {
  const qc = useQueryClient();
  const queryKey = getGetDashboardTopEdgesQueryKey({ limit: 25 });
  const { data, isLoading, error } = useGetDashboardTopEdges(
    { limit: 25 },
    { query: { queryKey, refetchInterval: 30_000, staleTime: 25_000 } },
  );
  const snooze = useSnoozeDashboardCandidate();
  const fire = useFireDashboardCandidate();
  const [pending, setPending] = useState<number | null>(null);

  const refresh = () => qc.invalidateQueries({ queryKey });

  const onSnooze = async (id: number) => {
    setPending(id);
    try {
      await snooze.mutateAsync({
        predictionId: id,
        data: { hours: 1, reason: "operator snooze (1h)" },
      });
    } finally {
      setPending(null);
      refresh();
    }
  };
  const onFire = async (id: number) => {
    setPending(id);
    try {
      await fire.mutateAsync({ predictionId: id });
    } finally {
      setPending(null);
      refresh();
    }
  };

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-top-edges">
      <SectionHeader
        eyebrow="EXECUTION"
        title="Top Edges Today"
        subtitle={
          data?.generated_at
            ? `Pending candidates ranked by EV edge%. Computed at ${fmtTime(data.generated_at)} · cap ${data.limit ?? 25}.`
            : "Pending candidates ranked by EV edge% (cap 25)."
        }
      />
      {error ? (
        <div className="mt-3 text-xs text-destructive" role="alert">
          Failed to load Top Edges.
        </div>
      ) : null}
      {data?.error ? (
        <div className="mt-3 text-xs text-destructive" role="alert">
          {data.error}
        </div>
      ) : null}
      {isLoading && !data ? (
        <div className="mt-3 h-10 w-full animate-pulse rounded bg-muted/50" />
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-[10px] uppercase tracking-wider text-muted-foreground">
              <tr>
                <th className="py-1 pr-2">Matchup</th>
                <th className="py-1 pr-2">Vertical</th>
                <th className="py-1 pr-2" title="Calibrated model probability of YES">Model</th>
                <th className="py-1 pr-2" title="Last Kalshi YES price snapshot">Market</th>
                <th className="py-1 pr-2" title="EV edge expressed as a percentage; sorted descending">Edge%</th>
                <th className="py-1 pr-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).length === 0 ? (
                <tr>
                  <td
                    colSpan={6}
                    className="py-3 text-center text-muted-foreground"
                    data-testid="top-edges-empty"
                  >
                    No pending candidates.
                  </td>
                </tr>
              ) : (
                (data?.items ?? []).map((row) => (
                  <tr
                    key={row.prediction_id}
                    data-testid={`top-edge-row-${row.prediction_id}`}
                    data-halted={row.vertical_halted ? "true" : "false"}
                    className={
                      row.vertical_halted
                        ? "opacity-50 text-muted-foreground"
                        : "text-foreground"
                    }
                  >
                    <td className="py-1 pr-2 font-medium">
                      {row.matchup}
                      {row.league ? (
                        <span className="ml-1 text-[10px] text-muted-foreground">
                          {row.league}
                        </span>
                      ) : null}
                    </td>
                    <td className="py-1 pr-2 uppercase">
                      {row.vertical}
                      {row.vertical_halted ? (
                        <span
                          className="ml-1 rounded bg-muted px-1 text-[9px] uppercase"
                          title="This vertical is currently halted — fire/snooze are disabled."
                        >
                          halted
                        </span>
                      ) : null}
                    </td>
                    <td className="py-1 pr-2 tabular-nums">{fmtProb(row.model_prob)}</td>
                    <td className="py-1 pr-2 tabular-nums">{fmtPrice(row.market_price)}</td>
                    <td className="py-1 pr-2 tabular-nums font-semibold">
                      {fmtPct(row.edge_pct)}
                    </td>
                    <td className="py-1 pr-2">
                      <button
                        type="button"
                        className="mr-1 rounded border border-border px-2 py-0.5 text-[10px] uppercase hover:bg-muted disabled:opacity-40"
                        disabled={
                          row.vertical_halted || pending === row.prediction_id
                        }
                        onClick={() => onFire(row.prediction_id)}
                        data-testid={`fire-${row.prediction_id}`}
                        title="Clear any snooze so the next executor cycle is free to fire this candidate."
                      >
                        Fire
                      </button>
                      <button
                        type="button"
                        className="rounded border border-border px-2 py-0.5 text-[10px] uppercase hover:bg-muted disabled:opacity-40"
                        disabled={
                          row.vertical_halted || pending === row.prediction_id
                        }
                        onClick={() => onSnooze(row.prediction_id)}
                        data-testid={`snooze-${row.prediction_id}`}
                        title="Suppress this candidate from the executor for 1 hour."
                      >
                        Snooze 1h
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}
      {data?.halted_verticals?.length ? (
        <div className="mt-2 text-[10px] text-muted-foreground">
          Halted: {data.halted_verticals.join(", ")}
        </div>
      ) : null}
    </GlassCard>
  );
}
