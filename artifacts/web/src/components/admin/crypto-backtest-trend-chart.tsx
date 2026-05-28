import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export type CryptoBacktestTrendPoint = {
  idx: number;
  brier: number | null;
  log_loss: number | null;
  accuracy?: number | null;
  completed_at?: string | null;
  n_evaluated?: number | null;
};

// Recharts is heavy (~100kB) and the crypto-backtest trend is the only chart
// on the admin surface. Splitting it into its own chunk keeps recharts out of
// the main admin bundle until an operator actually opens the backtest history.
export default function CryptoBacktestTrendChart({
  series,
}: {
  series: CryptoBacktestTrendPoint[];
}) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={series} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid
          strokeDasharray="3 3"
          stroke="hsl(var(--border))"
          opacity={0.3}
        />
        <XAxis
          dataKey="idx"
          tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
          tickLine={false}
          axisLine={false}
        />
        <YAxis
          tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
          tickLine={false}
          axisLine={false}
          width={36}
          domain={[0, "auto"]}
        />
        <Tooltip
          cursor={{ stroke: "hsl(var(--border))" }}
          contentStyle={{
            background: "hsl(var(--popover))",
            border: "1px solid hsl(var(--border))",
            fontSize: 11,
          }}
          labelFormatter={(_v, payload) => {
            const completed = payload?.[0]?.payload?.completed_at;
            return completed
              ? String(completed).replace("T", " ").replace(/\..*$/, "")
              : "";
          }}
          formatter={(value: number, name: string) => [
            typeof value === "number" ? value.toFixed(4) : value,
            name,
          ]}
        />
        <Line
          type="monotone"
          dataKey="brier"
          name="Brier"
          stroke="hsl(var(--primary))"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="log_loss"
          name="Log loss"
          stroke="hsl(var(--accent-cyan, var(--primary)))"
          strokeWidth={2}
          strokeDasharray="4 2"
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
