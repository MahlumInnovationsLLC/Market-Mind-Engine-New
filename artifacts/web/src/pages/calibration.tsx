import { useMemo, useState } from "react";
import { customFetch } from "@workspace/api-client-react";
import { useQuery } from "@tanstack/react-query";

interface SnapshotRow {
  id: number;
  vertical: string;
  sub_category: string | null;
  ece: number | null;
  mce: number | null;
  brier_score: number | null;
  sample_count: number;
  drift_status: { status?: string | null } | null;
  calibrator_state: Record<string, unknown> | null;
  config: Record<string, unknown> | null;
  created_at: string | null;
}

interface SnapshotsResponse {
  items: SnapshotRow[];
  count: number;
  error?: string;
}

const VERTICALS = [
  "all",
  "weather",
  "elections",
  "events_econ",
  "entertainment",
  "tech_ai",
  "crypto",
  "soccer",
  "nfl",
  "combat",
  "commodities",
];

function fmt(n: number | null | undefined, digits = 4): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function brierClass(b: number | null): string {
  if (b == null) return "text-muted-foreground";
  if (b > 0.25) return "text-destructive";
  if (b > 0.18) return "text-warning";
  return "text-success";
}

export default function CalibrationPage() {
  const [vertical, setVertical] = useState<string>("all");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["calibration-snapshots", vertical],
    queryFn: async () => {
      const qs =
        vertical && vertical !== "all"
          ? `?vertical=${encodeURIComponent(vertical)}`
          : "";
      return customFetch<SnapshotsResponse>(`/api/calibration/snapshots${qs}`);
    },
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const rows = useMemo(() => data?.items ?? [], [data]);
  const rolledBack = rows.filter(
    (r) => (r.drift_status?.status ?? "") === "rolled_back",
  ).length;

  return (
    <div className="space-y-4 font-mono" data-testid="page-calibration">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold tracking-tight">Calibration</h1>
        <p className="text-xs uppercase tracking-widest text-muted-foreground">
          Reliability history · per-snapshot Brier / ECE / MCE · backed by{" "}
          <code className="text-foreground">calibration_snapshots</code>
        </p>
      </header>

      <div className="flex items-center gap-3">
        <label className="text-[10px] uppercase tracking-widest text-muted-foreground">
          Vertical
        </label>
        <select
          value={vertical}
          onChange={(e) => setVertical(e.target.value)}
          className="bg-input border border-border text-foreground rounded-md px-2 py-1 text-xs"
          data-testid="select-calibration-vertical"
        >
          {VERTICALS.map((v) => (
            <option key={v} value={v}>{v}</option>
          ))}
        </select>
        <div className="ml-auto text-[10px] uppercase tracking-widest text-muted-foreground">
          {rows.length} snapshots · {rolledBack} rolled back
        </div>
      </div>

      <div className="border border-border rounded-md overflow-hidden">
        <table className="w-full text-xs" data-testid="table-calibration">
          <thead className="bg-muted/30">
            <tr className="text-left text-[10px] uppercase tracking-widest text-muted-foreground">
              <th className="px-3 py-2">When</th>
              <th className="px-3 py-2">Vertical</th>
              <th className="px-3 py-2 text-right">Samples</th>
              <th className="px-3 py-2 text-right">Brier</th>
              <th className="px-3 py-2 text-right">ECE</th>
              <th className="px-3 py-2 text-right">MCE</th>
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr><td colSpan={7} className="px-3 py-6 text-center text-muted-foreground">Loading…</td></tr>
            )}
            {isError && (
              <tr><td colSpan={7} className="px-3 py-6 text-center text-destructive">
                Failed to load: {String((error as Error)?.message ?? error)}
              </td></tr>
            )}
            {!isLoading && !isError && rows.length === 0 && (
              <tr><td colSpan={7} className="px-3 py-6 text-center text-muted-foreground">
                No snapshots yet — retrain cycles will populate this view.
              </td></tr>
            )}
            {rows.map((r) => {
              const status = r.drift_status?.status ?? null;
              const isRollback = status === "rolled_back";
              return (
                <tr
                  key={r.id}
                  className="border-t border-border/60"
                  data-testid={`row-snapshot-${r.id}`}
                >
                  <td className="px-3 py-2 text-muted-foreground">
                    {r.created_at ? new Date(r.created_at).toLocaleString() : "—"}
                  </td>
                  <td className="px-3 py-2">
                    {r.vertical}
                    {r.sub_category ? (
                      <span className="text-muted-foreground"> · {r.sub_category}</span>
                    ) : null}
                  </td>
                  <td className="px-3 py-2 text-right">{r.sample_count}</td>
                  <td className={`px-3 py-2 text-right ${brierClass(r.brier_score)}`}>
                    {fmt(r.brier_score)}
                  </td>
                  <td className="px-3 py-2 text-right">{fmt(r.ece, 3)}</td>
                  <td className="px-3 py-2 text-right">{fmt(r.mce, 3)}</td>
                  <td className="px-3 py-2">
                    {isRollback ? (
                      <span className="text-destructive uppercase text-[10px] tracking-widest">
                        rolled back
                      </span>
                    ) : status ? (
                      <span className="text-muted-foreground uppercase text-[10px] tracking-widest">
                        {status}
                      </span>
                    ) : (
                      <span className="text-success uppercase text-[10px] tracking-widest">
                        promoted
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
