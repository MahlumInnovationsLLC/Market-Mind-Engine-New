import { Link, useRoute } from "wouter";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  getGetTradeDetailQueryKey,
  useGetTradeDetail,
} from "@workspace/api-client-react";

function fmtMoney(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const formatted = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
  }).format(Math.abs(n));
  return n < 0 ? `-${formatted}` : formatted;
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleString([], {
    hour12: false,
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtTickTime(t: string): string {
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return t;
  return d.toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function TradeDetailPage() {
  const [, params] = useRoute("/trade/:ref");
  const ref = params?.ref ?? "";
  const { data, isLoading, isError, error } = useGetTradeDetail(ref, {
    query: {
      enabled: !!ref,
      staleTime: 30_000,
      queryKey: getGetTradeDetailQueryKey(ref),
    },
  });

  if (!ref) {
    return <Empty message="Missing trade reference" />;
  }
  if (isLoading) {
    return (
      <Frame>
        <div className="text-xs uppercase tracking-widest text-muted-foreground">
          loading trade detail…
        </div>
      </Frame>
    );
  }
  if (isError) {
    const status = (error as { status?: number } | null)?.status;
    return (
      <Frame>
        <div className="border border-destructive/40 bg-destructive/5 p-6 text-sm text-destructive">
          {status === 404 ? "Trade not found." : `Error loading trade (HTTP ${status ?? "?"}).`}
        </div>
      </Frame>
    );
  }

  const trade = data?.trade;
  const series = data?.series ?? [];
  if (!trade) return <Empty message="Trade not found" />;

  // Recharts expects raw numeric series. We carry a `tsMs` field for
  // the X axis so the anchor `ReferenceLine` for execution time lines
  // up with the actual sample timestamps.
  const points = series
    .filter((p) => !!p.t)
    .map((p) => ({
      t: p.t!,
      tsMs: new Date(p.t!).getTime(),
      market_price: p.market_price ?? null,
      model_prob: p.model_prob ?? null,
    }))
    .filter((p) => Number.isFinite(p.tsMs))
    .sort((a, b) => a.tsMs - b.tsMs);
  const anchorMs = trade.executed_at ? new Date(trade.executed_at).getTime() : null;
  const anchorValid = anchorMs !== null && Number.isFinite(anchorMs);

  const isLoss = trade.pnl !== null && trade.pnl !== undefined && trade.pnl < 0;
  const isWin = trade.pnl !== null && trade.pnl !== undefined && trade.pnl > 0;
  const pnlClass = isLoss ? "text-loss" : isWin ? "text-win" : "text-muted-foreground";

  return (
    <Frame>
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-3 border-b border-border pb-3">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            TRADE :: #{trade.id}
            {trade.trade_id ? ` · ${trade.trade_id}` : ""}
          </div>
          <h1
            className="mt-1 text-lg font-bold tracking-tight text-primary"
            data-testid="text-trade-title"
          >
            {trade.market_title || trade.market_id || `trade #${trade.id}`}
          </h1>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {trade.exchange ?? "—"} · {trade.trading_mode ?? "—"} ·{" "}
            {trade.vertical ?? "—"} · {trade.league ?? "—"}
          </div>
        </div>
        <Link
          href="/app"
          className="border border-border px-3 py-1 text-[10px] uppercase tracking-widest text-muted-foreground hover:border-primary hover:text-primary"
          data-testid="link-back-dashboard"
        >
          ← dashboard
        </Link>
      </div>

      <dl className="mb-6 grid grid-cols-2 gap-3 text-xs md:grid-cols-4 lg:grid-cols-6">
        <Stat label="Side / Action" value={`${(trade.side ?? "—").toUpperCase()} / ${(trade.action ?? "—").toUpperCase()}`} />
        <Stat label="Quantity" value={trade.quantity?.toString() ?? "—"} />
        <Stat label="Fill price" value={fmtMoney(trade.price)} />
        <Stat label="Model prob @ exec" value={fmtPct(trade.model_prob)} />
        <Stat label="Model edge" value={fmtPct(trade.model_edge)} />
        <Stat
          label="PNL"
          value={fmtMoney(trade.pnl)}
          valueClass={pnlClass + " font-bold"}
        />
        <Stat label="Kelly fraction" value={trade.kelly_fraction !== null && trade.kelly_fraction !== undefined ? trade.kelly_fraction.toFixed(3) : "—"} />
        <Stat label="Executed at" value={fmtTime(trade.executed_at)} />
        <Stat label="Market id" value={trade.market_id ?? "—"} mono />
      </dl>

      <section
        className="border border-border bg-card p-3"
        data-testid="card-comparison-chart"
      >
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
            MARKET_PRICE :: vs :: MODEL_PROBABILITY
          </h2>
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            {points.length} sample{points.length === 1 ? "" : "s"} · anchor at execution
          </span>
        </div>
        {points.length === 0 ? (
          <div
            className="flex h-72 items-center justify-center text-xs text-muted-foreground"
            data-testid="text-no-series"
          >
            NO_TIME_SERIES_AVAILABLE
          </div>
        ) : (
          <div className="h-72 w-full" data-testid="chart-comparison">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={points} margin={{ top: 10, right: 16, bottom: 10, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                <XAxis
                  dataKey="tsMs"
                  type="number"
                  domain={["dataMin", "dataMax"]}
                  scale="time"
                  tickFormatter={(v: number) => fmtTickTime(new Date(v).toISOString())}
                  stroke="hsl(var(--muted-foreground))"
                  fontSize={10}
                />
                <YAxis
                  domain={[0, 1]}
                  tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                  stroke="hsl(var(--muted-foreground))"
                  fontSize={10}
                  width={40}
                />
                <Tooltip
                  labelFormatter={(v) =>
                    fmtTime(new Date(Number(v)).toISOString())
                  }
                  formatter={(value: number, name: string) => [
                    fmtPct(value, 1),
                    name === "market_price" ? "Market price" : "Model prob",
                  ]}
                  contentStyle={{
                    background: "hsl(var(--popover))",
                    border: "1px solid hsl(var(--border))",
                    fontSize: 11,
                  }}
                />
                <Legend
                  wrapperStyle={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 1 }}
                  formatter={(name) =>
                    name === "market_price" ? "Market price" : "Model prob"
                  }
                />
                {anchorValid && (
                  <ReferenceLine
                    x={anchorMs}
                    stroke="hsl(var(--primary))"
                    strokeDasharray="4 2"
                    label={{
                      value: "EXEC",
                      position: "top",
                      fill: "hsl(var(--primary))",
                      fontSize: 10,
                    }}
                  />
                )}
                <Line
                  type="monotone"
                  dataKey="market_price"
                  stroke="hsl(var(--muted-foreground))"
                  strokeWidth={2}
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                />
                <Line
                  type="monotone"
                  dataKey="model_prob"
                  stroke="hsl(var(--primary))"
                  strokeWidth={2}
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </section>
    </Frame>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-[100dvh] bg-background p-4 font-mono text-foreground md:p-6">
      <div className="mx-auto max-w-5xl">{children}</div>
    </div>
  );
}

function Empty({ message }: { message: string }) {
  return (
    <Frame>
      <div className="text-sm text-muted-foreground">{message}</div>
    </Frame>
  );
}

function Stat({
  label,
  value,
  valueClass,
  mono,
}: {
  label: string;
  value: string;
  valueClass?: string;
  mono?: boolean;
}) {
  return (
    <div className="border border-border bg-card p-2">
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div
        className={`mt-1 truncate text-sm ${valueClass ?? "text-foreground"} ${mono ? "font-mono text-xs" : ""}`}
        title={value}
      >
        {value}
      </div>
    </div>
  );
}
