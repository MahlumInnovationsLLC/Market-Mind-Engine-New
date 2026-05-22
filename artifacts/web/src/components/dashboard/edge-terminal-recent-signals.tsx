import {
  getGetDashboardEdgeTerminalSignalsQueryKey,
  useGetDashboardEdgeTerminalSignals,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function fmtAmericanOdds(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  return v > 0 ? `+${Math.round(v)}` : `${Math.round(v)}`;
}

function fmtEvPct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
}

function fmtPickType(s: string | null | undefined): string {
  if (!s) return "—";
  return s.replace(/_/g, " ").toUpperCase();
}

function safeHttpUrl(s: string | null | undefined): string | null {
  if (!s || typeof s !== "string") return null;
  try {
    const u = new URL(s);
    if (u.protocol === "http:" || u.protocol === "https:") return u.toString();
    return null;
  } catch {
    return null;
  }
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleTimeString();
  } catch {
    return s;
  }
}

function dispositionTone(d: string | null | undefined): string {
  switch ((d || "").toLowerCase()) {
    case "placed":
      return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    case "skipped_unresolved":
    case "skipped_gate":
      return "border-amber-500/40 bg-amber-500/10 text-amber-300";
    case "errored":
      return "border-rose-500/40 bg-rose-500/10 text-rose-300";
    case "pending":
      return "border-sky-500/40 bg-sky-500/10 text-sky-300";
    default:
      return "border-border bg-muted/30 text-muted-foreground";
  }
}

export default function EdgeTerminalRecentSignalsTile() {
  const key = getGetDashboardEdgeTerminalSignalsQueryKey({ limit: 25 });
  const { data, isLoading, error } = useGetDashboardEdgeTerminalSignals(
    { limit: 25 },
    {
      query: { queryKey: key, refetchInterval: 30_000, staleTime: 25_000 },
    },
  );

  const items = data?.items ?? [];

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-edge-terminal-recent-signals"
    >
      <SectionHeader
        eyebrow="EDGE TERMINAL"
        title="Recent signals"
        subtitle="Latest scraped picks with pick type, average odds and source EV%."
      />
      {isLoading ? (
        <div className="text-xs text-muted-foreground">Loading…</div>
      ) : error ? (
        <div
          data-testid="edge-terminal-recent-signals-error"
          className="text-xs text-rose-400"
        >
          Failed to load.
        </div>
      ) : data?.error ? (
        <div
          data-testid="edge-terminal-recent-signals-error"
          className="text-xs text-rose-400"
        >
          {data.error}
        </div>
      ) : items.length === 0 ? (
        <div className="text-xs text-muted-foreground">No signals yet.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-left text-xs">
            <thead>
              <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
                <th className="py-1.5 pr-3">When</th>
                <th className="py-1.5 pr-3">Matchup</th>
                <th className="py-1.5 pr-3">Selection</th>
                <th className="py-1.5 pr-3">Pick</th>
                <th className="py-1.5 pr-3 text-right">Avg odds</th>
                <th className="py-1.5 pr-3 text-right">EV%</th>
                <th className="py-1.5 pr-3">Market</th>
                <th className="py-1.5 pr-3">Status</th>
                <th className="py-1.5 pr-3 text-right">Src</th>
              </tr>
            </thead>
            <tbody data-testid="edge-terminal-recent-signals-tbody">
              {items.map((s) => {
                const ev = typeof s.ev_pct === "number" ? s.ev_pct : null;
                const evColor =
                  ev === null
                    ? "text-muted-foreground"
                    : ev >= 0
                      ? "text-emerald-400"
                      : "text-rose-400";
                return (
                  <tr
                    key={s.id}
                    data-testid={`edge-terminal-recent-signal-row-${s.id}`}
                    className="border-b border-border/30 last:border-0"
                  >
                    <td className="py-1.5 pr-3 text-muted-foreground">
                      {fmtTime(s.scraped_at)}
                    </td>
                    <td className="py-1.5 pr-3 text-foreground">
                      {s.matchup ?? "—"}
                      {s.vertical ? (
                        <span className="ml-1 text-[10px] uppercase text-muted-foreground">
                          {s.vertical}
                        </span>
                      ) : null}
                    </td>
                    <td className="py-1.5 pr-3 text-foreground">
                      {s.selection ?? "—"}
                    </td>
                    <td className="py-1.5 pr-3">
                      <span
                        data-testid={`edge-terminal-pick-type-${s.id}`}
                        className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-foreground"
                      >
                        {fmtPickType(s.pick_type)}
                      </span>
                    </td>
                    <td
                      data-testid={`edge-terminal-avg-odds-${s.id}`}
                      className="py-1.5 pr-3 text-right tabular-nums text-foreground"
                    >
                      {fmtAmericanOdds(s.avg_odds)}
                    </td>
                    <td
                      data-testid={`edge-terminal-ev-pct-${s.id}`}
                      className={`py-1.5 pr-3 text-right tabular-nums ${evColor}`}
                    >
                      {fmtEvPct(s.ev_pct)}
                    </td>
                    <td className="py-1.5 pr-3 text-muted-foreground">
                      {s.ticker ?? "—"}
                      {s.kalshi_market_id ? (
                        <span
                          data-testid={`edge-terminal-market-id-${s.id}`}
                          title={s.kalshi_market_id}
                          className="ml-1 cursor-help text-[10px] text-muted-foreground/60"
                        >
                          ⓘ
                        </span>
                      ) : null}
                    </td>
                    <td className="py-1.5 pr-3">
                      <span
                        className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${dispositionTone(s.disposition)}`}
                      >
                        {s.disposition ?? "—"}
                      </span>
                    </td>
                    <td className="py-1.5 pr-3 text-right">
                      {(() => {
                        const href = safeHttpUrl(s.source_screenshot_url);
                        return href ? (
                          <a
                            data-testid={`edge-terminal-screenshot-${s.id}`}
                            href={href}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="text-accent-cyan hover:underline"
                            aria-label="Open source screenshot"
                          >
                            🔗
                          </a>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        );
                      })()}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </GlassCard>
  );
}
