import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  getGetRecentBankrollEventsQueryKey,
  useGetRecentBankrollEvents,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

const CYAN = "hsl(var(--primary))";
const WIN = "hsl(var(--win, 142 71% 45%))";
const LOSS = "hsl(var(--loss, 0 72% 55%))";

function fmtCurrencyCents(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || Number.isNaN(cents)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(cents / 100);
}

function fmtSignedCents(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || Number.isNaN(cents)) return "—";
  const sign = cents > 0 ? "+" : cents < 0 ? "−" : "";
  return `${sign}${fmtCurrencyCents(Math.abs(cents))}`;
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

function relativeFromUnix(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const ageS = Math.max(0, Math.round(Date.now() / 1000 - seconds));
  if (ageS < 60) return `${ageS}s ago`;
  if (ageS < 3600) return `${Math.round(ageS / 60)}m ago`;
  return `${Math.round(ageS / 3600)}h ago`;
}

const TWENTY_FOUR_HOURS_MS = 24 * 60 * 60 * 1000;

/**
 * Operator panel for the live Kalshi bankroll.
 *
 * Backed by `/api/dashboard/bankroll/recent` — itself the `bankroll_events`
 * append-only feed populated by the continuous balance sync (see
 * `app.bankroll_sync`).  Renders:
 *
 *   * Current balance + last-event timestamp.
 *   * Sparkline of the last 24h of running balance, colour-coded by the
 *     event's `reason` (sync / fill / deposit / withdrawal / manual).
 *   * Per-reason 24h delta breakdown so a big move can be attributed
 *     to a fill vs an exchange-side adjustment at a glance.
 *   * `exchange_sync` freshness badge driven by `bankroll_sync.state()`.
 */
export default function BankrollPanel() {
  const { data, isLoading, isError } = useGetRecentBankrollEvents(
    { limit: 200 },
    {
      query: {
        queryKey: getGetRecentBankrollEventsQueryKey({ limit: 200 }),
        refetchInterval: 30_000,
        staleTime: 15_000,
      },
    },
  );

  const items = useMemo(() => data?.items ?? [], [data]);
  const sync = data?.sync;

  const last24h = useMemo(() => {
    const cutoff = Date.now() - TWENTY_FOUR_HOURS_MS;
    return items.filter((it) => {
      if (!it.occurred_at) return false;
      const t = new Date(it.occurred_at).getTime();
      return Number.isFinite(t) && t >= cutoff;
    });
  }, [items]);

  const series = useMemo(
    () =>
      last24h.map((it) => ({
        ts: it.occurred_at,
        balance: (it.balance_cents ?? 0) / 100,
        delta: (it.delta_cents ?? 0) / 100,
        reason: it.reason,
      })),
    [last24h],
  );

  const reasonTotals = useMemo(() => {
    const acc: Record<string, number> = {};
    for (const it of last24h) {
      const k = it.reason || "unknown";
      acc[k] = (acc[k] ?? 0) + (it.delta_cents ?? 0);
    }
    return Object.entries(acc).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  }, [last24h]);

  const totalDelta = reasonTotals.reduce((s, [, v]) => s + v, 0);

  // Sync-freshness badge: stale if the last tick is older than 3x the
  // configured interval (default interval is 60s, so 3 minutes by default).
  const interval = sync?.interval_seconds ?? 60;
  const lastTick = sync?.last_tick_at ?? null;
  const ageS =
    lastTick !== null && lastTick !== undefined
      ? Math.max(0, Date.now() / 1000 - lastTick)
      : null;
  let syncTone: "fresh" | "stale" | "down" = "down";
  if (sync?.running) {
    if (ageS === null) syncTone = "stale";
    else if (ageS <= interval * 3) syncTone = "fresh";
    else syncTone = "stale";
  }
  const syncLabel =
    syncTone === "fresh"
      ? "FRESH"
      : syncTone === "stale"
        ? "STALE"
        : "STOPPED";
  const syncToneClass =
    syncTone === "fresh"
      ? "border-win/60 bg-win/10 text-win"
      : syncTone === "stale"
        ? "border-warning/60 bg-warning/10 text-warning"
        : "border-loss/60 bg-loss/10 text-loss";

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-bankroll-panel"
    >
      <SectionHeader
        eyebrow="EXCHANGE"
        title="Live Kalshi balance"
        subtitle="Balance sync feed — last 24h."
      />

      {isLoading ? (
        <div
          className="h-48 animate-pulse bg-muted/10"
          data-testid="skeleton-bankroll-panel"
          aria-busy="true"
        />
      ) : isError ? (
        <div
          className="flex h-48 items-center justify-center text-[11px] uppercase tracking-widest text-muted-foreground"
          data-testid="bankroll-panel-error"
        >
          BANKROLL_UNAVAILABLE
        </div>
      ) : (
        <div className="space-y-3 font-mono">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Current balance
              </div>
              <div
                className="text-2xl font-bold tabular-nums text-foreground"
                data-testid="bankroll-current-balance"
              >
                {fmtCurrencyCents(data?.current_balance_cents)}
              </div>
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Last event {fmtTime(data?.last_event_at)} ·{" "}
                <span
                  className={
                    totalDelta > 0
                      ? "text-win"
                      : totalDelta < 0
                        ? "text-loss"
                        : ""
                  }
                  data-testid="bankroll-24h-delta"
                >
                  24h {fmtSignedCents(totalDelta)}
                </span>
              </div>
            </div>
            <div
              className={`inline-flex items-center gap-2 border px-2 py-1 text-[10px] uppercase tracking-widest ${syncToneClass}`}
              data-testid="bankroll-sync-indicator"
              data-sync-tone={syncTone}
              title={
                sync?.last_error
                  ? `Last error: ${sync.last_error}`
                  : `Interval ${interval}s`
              }
            >
              <span className="font-bold">SYNC</span>
              <span>{syncLabel}</span>
              <span className="text-muted-foreground">
                · {relativeFromUnix(lastTick)}
              </span>
            </div>
          </div>

          <div className="h-40" data-testid="chart-bankroll-sparkline">
            {series.length === 0 ? (
              <div className="flex h-full items-center justify-center text-[11px] uppercase tracking-widest text-muted-foreground">
                NO_RECENT_EVENTS
              </div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  data={series}
                  margin={{ top: 8, right: 12, bottom: 8, left: -16 }}
                >
                  <defs>
                    <linearGradient id="bankroll-bal" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={CYAN} stopOpacity={0.45} />
                      <stop offset="100%" stopColor={CYAN} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid
                    stroke="hsl(var(--border))"
                    strokeDasharray="2 4"
                  />
                  <XAxis
                    dataKey="ts"
                    tickFormatter={fmtTime}
                    tick={{
                      fontSize: 10,
                      fill: "hsl(var(--muted-foreground))",
                    }}
                    stroke="hsl(var(--border))"
                    minTickGap={32}
                  />
                  <YAxis
                    tickFormatter={(v: number) =>
                      new Intl.NumberFormat("en-US", {
                        style: "currency",
                        currency: "USD",
                        maximumFractionDigits: 0,
                      }).format(v)
                    }
                    domain={["auto", "auto"]}
                    tick={{
                      fontSize: 10,
                      fill: "hsl(var(--muted-foreground))",
                    }}
                    stroke="hsl(var(--border))"
                    width={64}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "hsl(var(--card))",
                      border: "1px solid hsl(var(--border))",
                      fontSize: 11,
                    }}
                    formatter={(_value, _name, ctx) => {
                      const p = (ctx as { payload?: { balance?: number; delta?: number; reason?: string } } | undefined)
                        ?.payload;
                      if (!p) return ["—", ""];
                      const bal = p.balance ?? 0;
                      const delta = p.delta ?? 0;
                      return [
                        `${new Intl.NumberFormat("en-US", {
                          style: "currency",
                          currency: "USD",
                        }).format(bal)} (${
                          delta >= 0 ? "+" : "−"
                        }${new Intl.NumberFormat("en-US", {
                          style: "currency",
                          currency: "USD",
                        }).format(Math.abs(delta))})`,
                        p.reason ?? "",
                      ];
                    }}
                    labelFormatter={(v: string) => fmtTime(v)}
                  />
                  <ReferenceLine
                    y={0}
                    stroke="hsl(var(--muted-foreground))"
                    strokeDasharray="3 3"
                  />
                  <Area
                    type="monotone"
                    dataKey="balance"
                    name="Balance"
                    stroke={CYAN}
                    strokeWidth={2}
                    fill="url(#bankroll-bal)"
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </div>

          {reasonTotals.length > 0 ? (
            <div
              className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3"
              data-testid="bankroll-reason-breakdown"
            >
              {reasonTotals.map(([reason, deltaCents]) => {
                const tone =
                  deltaCents > 0
                    ? "text-win"
                    : deltaCents < 0
                      ? "text-loss"
                      : "text-muted-foreground";
                return (
                  <div
                    key={reason}
                    className="border border-border/40 bg-card/40 px-2 py-1"
                    data-testid={`bankroll-reason-${reason}`}
                  >
                    <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                      {reason}
                    </div>
                    <div
                      className={`tabular-nums ${tone}`}
                      style={{
                        // Suppress unused-vars lint: WIN/LOSS reserved for future per-bar tinting.
                        ["--win" as string]: WIN,
                        ["--loss" as string]: LOSS,
                      }}
                    >
                      {fmtSignedCents(deltaCents)}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      )}
    </GlassCard>
  );
}
