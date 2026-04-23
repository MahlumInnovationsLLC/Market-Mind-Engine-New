import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

interface CurvePoint {
  p_raw: number;
  p_cal: number;
}

interface LeagueItem {
  league: string;
  type: "isotonic" | "platt" | null;
  sample_count: number;
  fit_at: string | null;
  brier_before: number | null;
  brier_after: number | null;
  curve: CurvePoint[];
  knots: { x: number[]; y: number[] } | null;
}

interface PerLeagueResponse {
  items: LeagueItem[];
  supported: string[];
  fitted_count?: number;
  error?: string;
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

function deltaTone(before: number | null, after: number | null): string {
  if (before == null || after == null) return "text-muted-foreground";
  const d = after - before;
  if (d <= -0.005) return "text-success";
  if (d >= 0.005) return "text-destructive";
  return "text-muted-foreground";
}

function CalibrationCurve({ item }: { item: LeagueItem }) {
  const W = 180;
  const H = 140;
  const PAD = 18;
  const innerW = W - PAD * 2;
  const innerH = H - PAD * 2;
  const px = (v: number) => PAD + Math.max(0, Math.min(1, v)) * innerW;
  const py = (v: number) => PAD + (1 - Math.max(0, Math.min(1, v))) * innerH;

  const curve = item.curve ?? [];
  const pathD = curve.length
    ? curve
        .map((pt, i) => `${i === 0 ? "M" : "L"} ${px(pt.p_raw)} ${py(pt.p_cal)}`)
        .join(" ")
    : null;

  const knots = item.knots;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" className="text-foreground">
      <rect
        x={PAD} y={PAD} width={innerW} height={innerH}
        fill="none" stroke="currentColor" strokeOpacity={0.15}
      />
      <line
        x1={px(0)} y1={py(0)} x2={px(1)} y2={py(1)}
        stroke="currentColor" strokeOpacity={0.3} strokeDasharray="3 3"
      />
      {pathD && (
        <path
          d={pathD}
          fill="none"
          stroke="hsl(var(--primary))"
          strokeWidth={1.6}
        />
      )}
      {knots &&
        knots.x.map((xv, i) => (
          <circle
            key={i}
            cx={px(xv)}
            cy={py(knots.y[i])}
            r={1.8}
            fill="hsl(var(--primary))"
          />
        ))}
      <text x={PAD} y={H - 4} fontSize="8" fill="currentColor" opacity={0.55}>
        raw
      </text>
      <text x={4} y={PAD + 6} fontSize="8" fill="currentColor" opacity={0.55}>
        cal
      </text>
    </svg>
  );
}

function LeagueTile({ item }: { item: LeagueItem }) {
  const fitted = item.type !== null;
  const delta =
    item.brier_before != null && item.brier_after != null
      ? item.brier_after - item.brier_before
      : null;
  return (
    <div
      className="border border-border rounded-md p-3 bg-card/40 flex flex-col gap-1"
      data-testid={`tile-calibration-${item.league}`}
    >
      <div className="flex items-baseline justify-between">
        <div className="text-xs font-semibold tracking-wider">
          {item.league}
        </div>
        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {item.type ?? "no fit"}
        </div>
      </div>
      {fitted ? (
        <>
          <CalibrationCurve item={item} />
          <div className="grid grid-cols-2 gap-x-2 text-[10px] text-muted-foreground">
            <div>
              before:{" "}
              <span className="text-foreground">{fmt(item.brier_before)}</span>
            </div>
            <div>
              after:{" "}
              <span className="text-foreground">{fmt(item.brier_after)}</span>
            </div>
            <div>
              n: <span className="text-foreground">{item.sample_count}</span>
            </div>
            <div className={deltaTone(item.brier_before, item.brier_after)}>
              Δ: {delta == null ? "—" : (delta >= 0 ? "+" : "") + delta.toFixed(4)}
            </div>
          </div>
          <div className="text-[10px] text-muted-foreground">
            fit: {fmtTime(item.fit_at)}
          </div>
        </>
      ) : (
        <div className="flex h-[140px] items-center justify-center text-[10px] text-muted-foreground text-center">
          No fitted calibrator yet — needs ≥10 settled trades.
        </div>
      )}
    </div>
  );
}

export default function PerLeagueCalibration() {
  const q = useQuery({
    queryKey: ["dashboard-calibration-per-league"],
    queryFn: async () =>
      customFetch<PerLeagueResponse>("/api/dashboard/calibration/per-league"),
    staleTime: 60_000,
    refetchInterval: 120_000,
  });

  const items = useMemo(() => q.data?.items ?? [], [q.data]);
  const fittedCount =
    q.data?.fitted_count ?? items.filter((i) => i.type !== null).length;

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-per-league-calibration"
    >
      <SectionHeader
        eyebrow="MODELS"
        title="Per-league calibration"
        subtitle={
          q.data?.error
            ? `Failed to load: ${q.data.error}`
            : `${fittedCount}/${items.length} leagues fitted · raw→calibrated curve + Brier delta.`
        }
      />
      {q.isLoading && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          Loading calibrators…
        </div>
      )}
      {q.isError && (
        <div className="text-xs text-destructive py-6 text-center">
          Failed to load: {String((q.error as Error)?.message ?? q.error)}
        </div>
      )}
      {!q.isLoading && !q.isError && items.length === 0 && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          No calibrator snapshots yet — the nightly refit will populate this view.
        </div>
      )}
      {!q.isLoading && !q.isError && items.length > 0 && (
        <div
          className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3"
          data-testid="grid-per-league-calibration"
        >
          {items.map((it) => (
            <LeagueTile key={it.league} item={it} />
          ))}
        </div>
      )}
    </GlassCard>
  );
}
