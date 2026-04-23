import { useMemo, useState } from "react";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  getGetVerticalPerformanceQueryKey,
  useGetVerticalPerformance,
} from "@workspace/api-client-react";
import type {
  CalibrationBucket,
  PitBucket,
  VerticalDailyPoint,
  VerticalPerformanceEntry,
} from "@workspace/api-client-react";

type RangeKey = "7" | "30" | "90";

const RANGE_DAYS: Record<RangeKey, number> = { "7": 7, "30": 30, "90": 90 };

function fmtCurrency(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtNum(n: number | null | undefined, digits = 3): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(digits);
}

function Sparkline({
  vertical,
  metric,
  data,
  dataKey,
  stroke,
  formatValue,
}: {
  vertical: string;
  metric: "pnl" | "brier";
  data: VerticalDailyPoint[];
  dataKey: "cumulative_pnl" | "brier";
  stroke: string;
  formatValue: (n: number | null | undefined) => string;
}) {
  const series = useMemo(
    () =>
      data.map((d) => ({
        date: d.date,
        cumulative_pnl: d.cumulative_pnl,
        brier: d.brier,
      })),
    [data],
  );
  // For P&L, render as long as we have any points so a flat-zero
  // (break-even) curve still shows up. Brier requires at least one
  // settled-day value so an all-null series stays in the empty state.
  const hasData =
    dataKey === "cumulative_pnl"
      ? series.length > 0
      : series.some((d) => d.brier !== null && d.brier !== undefined);
  return (
    <div
      className="h-[60px]"
      data-testid={`sparkline-${metric}-${vertical}`}
    >
      {hasData ? (
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={series}
            margin={{ top: 4, right: 2, bottom: 0, left: 2 }}
          >
            <YAxis hide domain={["auto", "auto"]} />
            <XAxis hide dataKey="date" />
            {dataKey === "cumulative_pnl" ? (
              <ReferenceLine
                y={0}
                stroke="hsl(var(--muted-foreground))"
                strokeDasharray="2 2"
              />
            ) : null}
            <Tooltip
              contentStyle={{
                background: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                fontSize: 11,
              }}
              formatter={(v) => [
                formatValue(typeof v === "number" ? v : null),
                metric.toUpperCase(),
              ]}
              labelFormatter={(v) => String(v)}
            />
            <Line
              type="monotone"
              dataKey={dataKey}
              stroke={stroke}
              strokeWidth={1.5}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-full flex items-center justify-center text-[10px] uppercase tracking-widest text-muted-foreground">
          NO_DATA
        </div>
      )}
    </div>
  );
}

function VerticalCard({ entry }: { entry: VerticalPerformanceEntry }) {
  const isLoss = entry.pnl < 0;
  const isWin = entry.pnl > 0;
  const label = (entry.vertical || "—").toUpperCase();
  return (
    <div
      className="border border-border bg-card p-3"
      data-testid={`card-vertical-${entry.vertical}`}
    >
      <div className="flex justify-between items-baseline mb-2">
        <h3 className="text-xs uppercase tracking-widest font-bold text-primary">
          {label}
        </h3>
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
          N={entry.trades}
        </span>
      </div>
      <div
        className={`text-2xl font-bold ${
          isLoss ? "text-loss" : isWin ? "text-win" : "text-foreground"
        }`}
        data-testid={`text-vertical-pnl-${entry.vertical}`}
      >
        {fmtCurrency(entry.pnl)}
      </div>
      <div className="mb-2">
        <div className="flex items-center justify-between text-[9px] uppercase tracking-widest text-muted-foreground mt-1 mb-0.5">
          <span>CUM PNL</span>
        </div>
        <Sparkline
          vertical={entry.vertical}
          metric="pnl"
          data={entry.daily ?? []}
          dataKey="cumulative_pnl"
          stroke={
            isLoss
              ? "hsl(var(--loss))"
              : isWin
                ? "hsl(var(--win))"
                : "hsl(var(--primary))"
          }
          formatValue={fmtCurrency}
        />
        <div className="flex items-center justify-between text-[9px] uppercase tracking-widest text-muted-foreground mt-1 mb-0.5">
          <span>DAILY BRIER</span>
        </div>
        <Sparkline
          vertical={entry.vertical}
          metric="brier"
          data={entry.daily ?? []}
          dataKey="brier"
          stroke="hsl(var(--primary))"
          formatValue={(n) => fmtNum(n ?? null)}
        />
      </div>
      <div className="grid grid-cols-3 gap-2 text-[11px]">
        <div>
          <div className="text-muted-foreground uppercase tracking-widest text-[9px]">
            WIN%
          </div>
          <div className="font-bold">{fmtPct(entry.win_rate ?? null)}</div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-widest text-[9px]">
            TRADES
          </div>
          <div className="font-bold">{entry.trades}</div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-widest text-[9px]">
            BRIER
          </div>
          <div className="font-bold">{fmtNum(entry.brier ?? null)}</div>
        </div>
      </div>
    </div>
  );
}

function CalibrationChart({
  vertical,
  data,
}: {
  vertical: string;
  data: CalibrationBucket[];
}) {
  // Recharts ignores nulls for `connectNulls=false` — we keep gaps where
  // no samples landed in a bin so operators don't read in noise.
  const series = useMemo(
    () =>
      data.map((b) => ({
        bucket: b.bucket,
        predicted: b.predicted,
        observed: b.observed,
        n: b.n,
      })),
    [data],
  );
  const hasData = data.some((b) => (b.n ?? 0) > 0);
  return (
    <div
      className="border border-border bg-card p-3"
      data-testid={`chart-calibration-${vertical}`}
    >
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2">
        CALIBRATION · {vertical.toUpperCase()}
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
                  background: "hsl(var(--card))",
                  border: "1px solid hsl(var(--border))",
                  fontSize: 11,
                }}
                formatter={(v: number | null, name: string) =>
                  v === null || v === undefined
                    ? ["—", name]
                    : [v.toFixed(3), name]
                }
                labelFormatter={(v: number) => `bin ${v.toFixed(2)}`}
              />
              <ReferenceLine
                segment={[
                  { x: 0, y: 0 },
                  { x: 1, y: 1 },
                ]}
                stroke="hsl(var(--muted-foreground))"
                strokeDasharray="3 3"
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
          <div className="h-full flex items-center justify-center text-[11px] text-muted-foreground">
            NO_SETTLED_DATA
          </div>
        )}
      </div>
    </div>
  );
}

function PitChart({
  vertical,
  data,
}: {
  vertical: string;
  data: PitBucket[];
}) {
  const series = useMemo(
    () => data.map((b) => ({ bucket: b.bucket, count: b.count })),
    [data],
  );
  const total = data.reduce((acc, b) => acc + b.count, 0);
  const expected = total > 0 ? total / data.length : 0;
  return (
    <div
      className="border border-border bg-card p-3"
      data-testid={`chart-pit-${vertical}`}
    >
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground mb-2">
        PIT · {vertical.toUpperCase()}
      </div>
      <div className="h-40">
        {total > 0 ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
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
                tick={{ fontSize: 9, fill: "hsl(var(--muted-foreground))" }}
                stroke="hsl(var(--border))"
                allowDecimals={false}
              />
              <Tooltip
                contentStyle={{
                  background: "hsl(var(--card))",
                  border: "1px solid hsl(var(--border))",
                  fontSize: 11,
                }}
                labelFormatter={(v: number) => `bin ${v.toFixed(2)}`}
              />
              <ReferenceLine
                y={expected}
                stroke="hsl(var(--muted-foreground))"
                strokeDasharray="3 3"
              />
              <Bar
                dataKey="count"
                fill="hsl(var(--primary))"
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-full flex items-center justify-center text-[11px] text-muted-foreground">
            NO_SETTLED_DATA
          </div>
        )}
      </div>
    </div>
  );
}

export default function VerticalPerformance() {
  const [range, setRange] = useState<RangeKey>("30");
  const days = RANGE_DAYS[range];
  const params = { days };
  const { matches } = useVerticalFilter();
  const { data, isLoading } = useGetVerticalPerformance(params, {
    query: {
      queryKey: getGetVerticalPerformanceQueryKey(params),
      refetchInterval: 60_000,
      staleTime: 30_000,
    },
  });

  const verticals = (data?.verticals ?? []).filter((v) => matches(v.vertical));

  return (
    <div className="border border-border bg-card">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          VERTICAL_PERFORMANCE
        </h2>
        <div
          className="flex items-center gap-1"
          role="group"
          aria-label="time range"
        >
          {(["7", "30", "90"] as RangeKey[]).map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setRange(r)}
              data-testid={`button-range-${r}`}
              className={`px-2 py-0.5 text-[10px] uppercase tracking-widest border transition-colors ${
                range === r
                  ? "border-primary text-primary bg-primary/10"
                  : "border-border text-muted-foreground hover:text-primary hover:border-primary"
              }`}
            >
              {r}D
            </button>
          ))}
        </div>
      </div>

      <div className="p-3 space-y-3">
        {isLoading && verticals.length === 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div className="h-28 bg-muted/10 animate-pulse" />
            <div className="h-28 bg-muted/10 animate-pulse" />
            <div className="h-28 bg-muted/10 animate-pulse" />
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {verticals.map((v) => (
              <VerticalCard key={v.vertical} entry={v} />
            ))}
          </div>
        )}

        {verticals.map((v) => (
          <div
            key={`charts-${v.vertical}`}
            className="grid grid-cols-1 md:grid-cols-2 gap-3"
          >
            <CalibrationChart vertical={v.vertical} data={v.calibration} />
            <PitChart vertical={v.vertical} data={v.pit} />
          </div>
        ))}
      </div>
    </div>
  );
}
