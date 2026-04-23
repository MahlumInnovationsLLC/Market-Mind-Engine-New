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
  "sports",
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

function extractKnots(
  state: Record<string, unknown> | null,
): { x: number[]; y: number[] } | null {
  if (!state) return null;
  const xRaw = (state as { x?: unknown; X?: unknown }).x ??
    (state as { X?: unknown }).X;
  const yRaw = (state as { y?: unknown; Y?: unknown }).y ??
    (state as { Y?: unknown }).Y;
  if (!Array.isArray(xRaw) || !Array.isArray(yRaw)) return null;
  const x = xRaw.map((v) => Number(v)).filter((v) => Number.isFinite(v));
  const y = yRaw.map((v) => Number(v)).filter((v) => Number.isFinite(v));
  if (x.length < 2 || x.length !== y.length) return null;
  return { x, y };
}

function ReliabilityDiagram({ snapshot }: { snapshot: SnapshotRow | null }) {
  const knots = useMemo(
    () => (snapshot ? extractKnots(snapshot.calibrator_state) : null),
    [snapshot],
  );
  const W = 320;
  const H = 220;
  const PAD = 28;
  const innerW = W - PAD * 2;
  const innerH = H - PAD * 2;
  const px = (v: number) => PAD + Math.max(0, Math.min(1, v)) * innerW;
  const py = (v: number) => PAD + (1 - Math.max(0, Math.min(1, v))) * innerH;
  const pathD = knots
    ? knots.x
        .map((xv, i) => `${i === 0 ? "M" : "L"} ${px(xv)} ${py(knots.y[i])}`)
        .join(" ")
    : null;

  return (
    <div className="border border-border rounded-md p-3 bg-card/40">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2">
        Reliability diagram
        {snapshot
          ? ` · ${snapshot.vertical}${snapshot.sub_category ? " · " + snapshot.sub_category : ""}`
          : ""}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" className="text-foreground">
        <rect
          x={PAD}
          y={PAD}
          width={innerW}
          height={innerH}
          fill="none"
          stroke="currentColor"
          strokeOpacity={0.15}
        />
        <line
          x1={px(0)} y1={py(0)} x2={px(1)} y2={py(1)}
          stroke="currentColor" strokeOpacity={0.3} strokeDasharray="4 3"
        />
        {pathD && (
          <path d={pathD} fill="none" stroke="hsl(var(--primary))" strokeWidth={2} />
        )}
        {knots && knots.x.map((xv, i) => (
          <circle
            key={i}
            cx={px(xv)}
            cy={py(knots.y[i])}
            r={2.5}
            fill="hsl(var(--primary))"
          />
        ))}
        <text x={PAD} y={H - 8} fontSize="9" fill="currentColor" opacity={0.6}>
          predicted →
        </text>
        <text
          x={8} y={PAD + 8} fontSize="9" fill="currentColor" opacity={0.6}
        >
          observed
        </text>
      </svg>
      {!knots && (
        <div className="text-[10px] text-muted-foreground text-center pb-2">
          {snapshot
            ? "Snapshot has no isotonic knot points to plot."
            : "Pick a snapshot to render its calibrator."}
        </div>
      )}
    </div>
  );
}

function BrierHistory({ rows }: { rows: SnapshotRow[] }) {
  const series = useMemo(() => {
    return rows
      .filter((r) => r.brier_score != null && r.created_at)
      .slice()
      .sort(
        (a, b) =>
          new Date(a.created_at!).getTime() -
          new Date(b.created_at!).getTime(),
      );
  }, [rows]);

  const W = 640;
  const H = 160;
  const PAD = 28;
  const innerW = W - PAD * 2;
  const innerH = H - PAD * 2;

  if (series.length < 2) {
    return (
      <div className="border border-border rounded-md p-3 bg-card/40">
        <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2">
          Brier history
        </div>
        <div className="text-[10px] text-muted-foreground text-center py-6">
          Need at least two snapshots to draw a history chart.
        </div>
      </div>
    );
  }

  const briers = series.map((r) => r.brier_score!);
  const minB = Math.min(...briers);
  const maxB = Math.max(...briers);
  const span = Math.max(1e-6, maxB - minB);
  const ts = series.map((r) => new Date(r.created_at!).getTime());
  const tMin = Math.min(...ts);
  const tMax = Math.max(...ts);
  const tSpan = Math.max(1, tMax - tMin);

  const px = (t: number) => PAD + ((t - tMin) / tSpan) * innerW;
  const py = (b: number) =>
    PAD + (1 - (b - minB) / span) * innerH;

  const pathD = series
    .map((r, i) => `${i === 0 ? "M" : "L"} ${px(ts[i])} ${py(r.brier_score!)}`)
    .join(" ");

  return (
    <div className="border border-border rounded-md p-3 bg-card/40">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
          Brier history · {series.length} snapshots
        </div>
        <div className="text-[10px] text-muted-foreground">
          min {fmt(minB)} · max {fmt(maxB)}
        </div>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" className="text-foreground">
        <rect
          x={PAD} y={PAD} width={innerW} height={innerH}
          fill="none" stroke="currentColor" strokeOpacity={0.15}
        />
        <path d={pathD} fill="none" stroke="hsl(var(--primary))" strokeWidth={1.5} />
        {series.map((r, i) => {
          const isRollback = (r.drift_status?.status ?? "") === "rolled_back";
          return (
            <circle
              key={r.id}
              cx={px(ts[i])}
              cy={py(r.brier_score!)}
              r={isRollback ? 3.5 : 2}
              fill={isRollback ? "hsl(var(--destructive))" : "hsl(var(--primary))"}
            >
              <title>
                {`#${r.id} · ${r.vertical} · brier=${fmt(r.brier_score)} · ${
                  r.drift_status?.status ?? "promoted"
                }`}
              </title>
            </circle>
          );
        })}
        <text x={PAD} y={H - 8} fontSize="9" fill="currentColor" opacity={0.6}>
          time →
        </text>
        <text x={8} y={PAD + 8} fontSize="9" fill="currentColor" opacity={0.6}>
          brier
        </text>
      </svg>
    </div>
  );
}

export default function CalibrationPage() {
  const [vertical, setVertical] = useState<string>("all");
  const [selectedId, setSelectedId] = useState<number | null>(null);

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
  const selected = useMemo(
    () => rows.find((r) => r.id === selectedId) ?? rows[0] ?? null,
    [rows, selectedId],
  );

  return (
    <div className="space-y-4 font-mono" data-testid="page-calibration">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold tracking-tight">Calibration</h1>
        <p className="text-xs uppercase tracking-widest text-muted-foreground">
          Reliability diagrams · Brier history · backed by{" "}
          <code className="text-foreground">calibration_snapshots</code>
        </p>
      </header>

      <div className="flex items-center gap-3">
        <label className="text-[10px] uppercase tracking-widest text-muted-foreground">
          Vertical
        </label>
        <select
          value={vertical}
          onChange={(e) => {
            setVertical(e.target.value);
            setSelectedId(null);
          }}
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

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_minmax(320px,360px)] gap-4">
        <BrierHistory rows={rows} />
        <ReliabilityDiagram snapshot={selected} />
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
              const isSelected = selected?.id === r.id;
              return (
                <tr
                  key={r.id}
                  className={`border-t border-border/60 cursor-pointer hover:bg-muted/20 ${
                    isSelected ? "bg-muted/30" : ""
                  }`}
                  onClick={() => setSelectedId(r.id)}
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
