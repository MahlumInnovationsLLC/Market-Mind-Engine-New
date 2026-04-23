import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface EquityPoint {
  date: string;
  daily_pnl: number;
  cumulative_pnl: number;
  trades: number;
}

function formatUsd(n: number | null | undefined, signed = false): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const formatted =
    abs >= 1000
      ? `$${abs.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
      : `$${abs.toFixed(2)}`;
  if (!signed) return n < 0 ? `-${formatted}` : formatted;
  return n < 0 ? `-${formatted}` : `+${formatted}`;
}

export default function EquityChart({ data }: { data: EquityPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart
        data={data}
        margin={{ top: 10, right: 16, left: 0, bottom: 0 }}
      >
        <defs>
          <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
            <stop
              offset="0%"
              stopColor="hsl(189 94% 55%)"
              stopOpacity={0.45}
            />
            <stop
              offset="100%"
              stopColor="hsl(189 94% 55%)"
              stopOpacity={0}
            />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="2 4" stroke="hsl(220 25% 18%)" />
        <XAxis
          dataKey="date"
          stroke="hsl(215 20% 70%)"
          tick={{ fontSize: 11, fontFamily: "Inter, sans-serif" }}
          tickFormatter={(v: string) => v.slice(5)}
          minTickGap={24}
        />
        <YAxis
          stroke="hsl(215 20% 70%)"
          tick={{ fontSize: 11, fontFamily: "Inter, sans-serif" }}
          tickFormatter={(v: number) => formatUsd(v)}
          width={70}
        />
        <Tooltip
          contentStyle={{
            background: "hsl(222 35% 7%)",
            border: "1px solid hsl(220 25% 18%)",
            borderRadius: 12,
            fontSize: 12,
            fontFamily: "Inter, sans-serif",
          }}
          labelStyle={{ color: "hsl(210 40% 98%)" }}
          formatter={(value: number, name: string) => [
            formatUsd(value, name === "daily_pnl"),
            name === "cumulative_pnl" ? "Cumulative" : "Daily",
          ]}
        />
        <Area
          type="monotone"
          dataKey="cumulative_pnl"
          stroke="hsl(189 94% 55%)"
          strokeWidth={2}
          fill="url(#equityFill)"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
