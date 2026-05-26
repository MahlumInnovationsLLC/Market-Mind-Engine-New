import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth } from "@clerk/react";
import { apiFetch } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

type Candidate = { ticker: string; title: string; score: number };
type LinkedPosition = {
  id: number;
  market_id: string;
  side: string | null;
  quantity: number | null;
  avg_price: number | null;
};
type SignalRow = {
  id: number;
  seen_at: string | null;
  vertical: string | null;
  matchup: string | null;
  selection: string | null;
  pick_type: string | null;
  side: string | null;
  avg_odds: number | null;
  ev_pct: number | null;
  edge_pct: number | null;
  status: string | null;
  status_reason: string | null;
  resolved_ticker: string | null;
  resolved_reason: string;
  candidates: Candidate[];
  linked_positions: LinkedPosition[];
};
type Position = {
  id: number;
  market_id: string;
  vertical: string | null;
  side: string | null;
  quantity: number | null;
  avg_price: number | null;
  current_price: number | null;
  opened_at: string | null;
  trading_mode: string | null;
};
type Summary = {
  n_signals: number;
  n_matched: number;
  n_unmatched: number;
  n_open_positions: number;
  n_positions_with_signal: number;
  n_unbacked_positions: number;
};
type Response = {
  generated_at: string;
  limit: number;
  signals: SignalRow[];
  open_positions: Position[];
  unbacked_open_positions: Position[];
  summary: Summary;
  error: string | null;
};

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleTimeString();
  } catch {
    return s;
  }
}

function fmtOdds(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  return v > 0 ? `+${Math.round(v)}` : `${Math.round(v)}`;
}

export default function EdgeTerminalSignalMatchesTile() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useCallback(
    () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const [data, setData] = useState<Response | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"unmatched" | "matched" | "unbacked">(
    "unmatched",
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch<Response>(
        "/api/dashboard/edge-terminal-signal-matches?limit=50",
        { getToken: tokenFn },
      );
      setData(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [tokenFn]);

  useEffect(() => {
    if (!isSignedIn) return;
    void load();
    const id = window.setInterval(load, 30_000);
    return () => window.clearInterval(id);
  }, [load, isSignedIn]);

  const summary = data?.summary;
  const matchedSignals = useMemo(
    () => (data?.signals ?? []).filter((s) => s.resolved_ticker),
    [data],
  );
  const unmatchedSignals = useMemo(
    () => (data?.signals ?? []).filter((s) => !s.resolved_ticker),
    [data],
  );
  const unbacked = data?.unbacked_open_positions ?? [];

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-edge-terminal-signal-matches"
    >
      <SectionHeader
        eyebrow="EDGE TERMINAL ↔ KALSHI"
        title="Signal matching"
        subtitle="Live cross-check of Edge Terminal picks against Kalshi markets and your open positions."
      />

      {loading && !data ? (
        <div className="text-xs text-muted-foreground">Loading…</div>
      ) : error ? (
        <div
          data-testid="edge-terminal-signal-matches-error"
          className="text-xs text-rose-400"
        >
          {error}
        </div>
      ) : data?.error ? (
        <div
          data-testid="edge-terminal-signal-matches-error"
          className="text-xs text-rose-400"
        >
          {data.error}
        </div>
      ) : !data ? null : (
        <>
          <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-5">
            <SummaryCell label="Signals" value={summary?.n_signals ?? 0} />
            <SummaryCell
              label="Matched"
              value={summary?.n_matched ?? 0}
              tone="emerald"
            />
            <SummaryCell
              label="Unmatched"
              value={summary?.n_unmatched ?? 0}
              tone="amber"
            />
            <SummaryCell
              label="Open positions"
              value={summary?.n_open_positions ?? 0}
            />
            <SummaryCell
              label="Pos. w/ signal"
              value={summary?.n_positions_with_signal ?? 0}
              tone="cyan"
            />
          </div>

          <div className="mb-3 flex flex-wrap gap-2 text-xs">
            <TabBtn
              active={view === "unmatched"}
              onClick={() => setView("unmatched")}
              count={unmatchedSignals.length}
              label="Unmatched signals"
            />
            <TabBtn
              active={view === "matched"}
              onClick={() => setView("matched")}
              count={matchedSignals.length}
              label="Matched signals"
            />
            <TabBtn
              active={view === "unbacked"}
              onClick={() => setView("unbacked")}
              count={unbacked.length}
              label="Positions w/o signal"
            />
          </div>

          {view === "unmatched" ? (
            <UnmatchedTable rows={unmatchedSignals} />
          ) : view === "matched" ? (
            <MatchedTable rows={matchedSignals} />
          ) : (
            <UnbackedTable rows={unbacked} />
          )}
        </>
      )}
    </GlassCard>
  );
}

function SummaryCell({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "emerald" | "amber" | "cyan";
}) {
  const cls =
    tone === "emerald"
      ? "text-emerald-300"
      : tone === "amber"
        ? "text-amber-300"
        : tone === "cyan"
          ? "text-accent-cyan"
          : "text-foreground";
  return (
    <div className="rounded border border-border bg-muted/20 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className={`text-lg font-semibold tabular-nums ${cls}`}>{value}</div>
    </div>
  );
}

function TabBtn({
  active,
  onClick,
  count,
  label,
}: {
  active: boolean;
  onClick: () => void;
  count: number;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={`edge-terminal-matches-tab-${label.toLowerCase().replace(/\s+/g, "-")}`}
      className={`rounded border px-2 py-1 ${
        active
          ? "border-accent-cyan/60 bg-accent-cyan/10 text-accent-cyan"
          : "border-border bg-muted/20 text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}{" "}
      <span className="ml-1 rounded bg-muted/40 px-1 text-[10px]">{count}</span>
    </button>
  );
}

function UnmatchedTable({ rows }: { rows: SignalRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        Every recent signal resolved to a Kalshi ticker. Nice.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-left text-xs">
        <thead>
          <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
            <th className="py-1.5 pr-3">When</th>
            <th className="py-1.5 pr-3">Matchup</th>
            <th className="py-1.5 pr-3">Pick</th>
            <th className="py-1.5 pr-3">Why no match</th>
            <th className="py-1.5 pr-3">Top candidates considered</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr
              key={s.id}
              data-testid={`edge-terminal-unmatched-row-${s.id}`}
              className="border-b border-border/30 align-top last:border-0"
            >
              <td className="py-1.5 pr-3 text-muted-foreground">
                {fmtTime(s.seen_at)}
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
                <span className="ml-1 text-[10px] uppercase text-muted-foreground">
                  {s.pick_type}
                </span>
              </td>
              <td className="py-1.5 pr-3 text-amber-300">{s.resolved_reason}</td>
              <td className="py-1.5 pr-3 text-muted-foreground">
                {s.candidates.length === 0 ? (
                  <span className="italic">none</span>
                ) : (
                  <ul className="space-y-0.5">
                    {s.candidates.slice(0, 3).map((c) => (
                      <li key={c.ticker} className="font-mono text-[11px]">
                        {c.ticker}{" "}
                        <span className="text-muted-foreground/70">
                          ({c.score})
                        </span>{" "}
                        <span className="text-foreground/70">{c.title}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MatchedTable({ rows }: { rows: SignalRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        No matched signals in the latest scrape window.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-left text-xs">
        <thead>
          <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
            <th className="py-1.5 pr-3">When</th>
            <th className="py-1.5 pr-3">Matchup</th>
            <th className="py-1.5 pr-3">Pick</th>
            <th className="py-1.5 pr-3 text-right">Odds</th>
            <th className="py-1.5 pr-3">Kalshi ticker</th>
            <th className="py-1.5 pr-3">Open position?</th>
            <th className="py-1.5 pr-3">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr
              key={s.id}
              data-testid={`edge-terminal-matched-row-${s.id}`}
              className="border-b border-border/30 align-top last:border-0"
            >
              <td className="py-1.5 pr-3 text-muted-foreground">
                {fmtTime(s.seen_at)}
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
                {s.selection ?? "—"}{" "}
                <span className="text-[10px] uppercase text-muted-foreground">
                  {s.pick_type}
                </span>
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">
                {fmtOdds(s.avg_odds)}
              </td>
              <td className="py-1.5 pr-3 font-mono text-[11px] text-accent-cyan">
                {s.resolved_ticker}
              </td>
              <td className="py-1.5 pr-3">
                {s.linked_positions.length === 0 ? (
                  <span className="text-muted-foreground">—</span>
                ) : (
                  s.linked_positions.map((p) => (
                    <div key={p.id} className="text-emerald-300">
                      {p.side?.toUpperCase()} × {p.quantity} @{" "}
                      {p.avg_price?.toFixed(2)}
                    </div>
                  ))
                )}
              </td>
              <td className="py-1.5 pr-3 text-muted-foreground">
                {s.status}
                {s.status_reason ? (
                  <div className="text-[10px] text-muted-foreground/70">
                    {s.status_reason}
                  </div>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function UnbackedTable({ rows }: { rows: Position[] }) {
  if (rows.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        Every open Kalshi position has a matching Edge Terminal signal.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-left text-xs">
        <thead>
          <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted-foreground">
            <th className="py-1.5 pr-3">Opened</th>
            <th className="py-1.5 pr-3">Kalshi ticker</th>
            <th className="py-1.5 pr-3">Side</th>
            <th className="py-1.5 pr-3 text-right">Qty</th>
            <th className="py-1.5 pr-3 text-right">Avg price</th>
            <th className="py-1.5 pr-3">Mode</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr
              key={p.id}
              data-testid={`edge-terminal-unbacked-row-${p.id}`}
              className="border-b border-border/30 last:border-0"
            >
              <td className="py-1.5 pr-3 text-muted-foreground">
                {fmtTime(p.opened_at)}
              </td>
              <td className="py-1.5 pr-3 font-mono text-[11px] text-accent-cyan">
                {p.market_id}
              </td>
              <td className="py-1.5 pr-3">{p.side?.toUpperCase()}</td>
              <td className="py-1.5 pr-3 text-right tabular-nums">
                {p.quantity}
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">
                {p.avg_price?.toFixed(2) ?? "—"}
              </td>
              <td className="py-1.5 pr-3 text-muted-foreground">
                {p.trading_mode ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
