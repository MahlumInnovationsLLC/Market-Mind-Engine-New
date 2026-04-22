import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  getGetVerticalPerformanceQueryKey,
  useGetVerticalPerformance,
  type CalibrationBucket,
  type VerticalPerformanceEntry,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function CalibrationCurve({ entry }: { entry: VerticalPerformanceEntry }) {
  const series = useMemo(
    () =>
      (entry.calibration ?? []).map((b: CalibrationBucket) => ({
        bucket: b.bucket,
        observed: b.observed,
        predicted: b.predicted,
        ideal: b.bucket,
        n: b.n,
      })),
    [entry.calibration],
  );
  const hasData = series.some((b) => (b.n ?? 0) > 0);
  return (
    <div
      className="border border-border bg-card/40 p-3"
      data-testid={`card-calibration-${entry.vertical}`}
    >
      <div className="mb-2 flex items-baseline justify-between">
        <h3 className="text-[11px] font-bold uppercase tracking-widest text-primary">
          {entry.vertical.toUpperCase()}
        </h3>
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
          N={entry.trades}
        </span>
      </div>
      <div className="h-40">
        {hasData ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={series}
              margin={{ top: 4, right: 8, bottom: 4, left: -16 }}
            >
              <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="2 2" />
              <XAxis
                dataKey="bucket"
                type="number"
                domain={[0, 1]}
                ticks={[0, 0.25, 0.5, 0.75, 1]}
                tick={{ fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
                stroke="hsl(var(--border))"
              />
              <YAxis
                domain={[0, 1]}
                ticks={[0, 0.25, 0.5, 0.75, 1]}
                tick={{ fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
                stroke="hsl(var(--border))"
              />
              <Tooltip
                contentStyle={{
                  background: "hsl(var(--popover))",
                  border: "1px solid hsl(var(--border))",
                  fontSize: 11,
                }}
                formatter={(v: unknown, name: unknown) =>
                  typeof v === "number"
                    ? [v.toFixed(3), String(name)]
                    : ["—", String(name)]
                }
                labelFormatter={(v: unknown) =>
                  typeof v === "number" ? `bin ${v.toFixed(2)}` : ""
                }
              />
              <Line
                type="linear"
                dataKey="ideal"
                name="ideal"
                stroke="hsl(var(--muted-foreground))"
                strokeDasharray="3 3"
                dot={false}
                strokeWidth={1}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="observed"
                name="observed"
                stroke="hsl(var(--primary))"
                dot={{ r: 2 }}
                strokeWidth={2}
                connectNulls={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-[11px] uppercase tracking-widest text-muted-foreground">
            NO_SETTLED_DATA
          </div>
        )}
      </div>
      <div className="mt-1 text-right text-[10px] uppercase tracking-widest text-muted-foreground">
        Brier: {entry.brier !== null && entry.brier !== undefined ? entry.brier.toFixed(3) : "—"}
      </div>
    </div>
  );
}

export default function CalibrationPanel() {
  const params = { days: 30 };
  const { data, isLoading, isError } = useGetVerticalPerformance(params, {
    query: {
      queryKey: getGetVerticalPerformanceQueryKey(params),
      refetchInterval: 60_000,
      staleTime: 30_000,
    },
  });

  const verticals = data?.verticals ?? [];

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-calibration">
      <SectionHeader
        eyebrow="CALIBRATION"
        title="Reliability per vertical"
        subtitle="30d observed vs predicted probability — diagonal = perfectly calibrated."
      />
      {isLoading ? (
        <div
          className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3"
          data-testid="skeleton-calibration"
        >
          <div className="h-48 animate-pulse bg-muted/10" />
          <div className="h-48 animate-pulse bg-muted/10" />
          <div className="h-48 animate-pulse bg-muted/10" />
        </div>
      ) : isError ? (
        <div className="border border-destructive/40 p-4 text-center text-xs uppercase tracking-widest text-destructive">
          ERR_CALIBRATION_LOAD
        </div>
      ) : verticals.length === 0 ? (
        <div className="border border-border bg-card/40 p-4 text-center text-xs uppercase tracking-widest text-muted-foreground">
          NO_VERTICALS_REPORTING
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {verticals.map((v) => (
            <CalibrationCurve key={v.vertical} entry={v} />
          ))}
        </div>
      )}
    </GlassCard>
  );
}
