import { Link } from "wouter";
import {
  getGetMyAccessQueryKey,
  getGetVerticalsOverviewQueryKey,
  useGetMyAccess,
  useGetVerticalsOverview,
  type VerticalsOverviewEntry,
} from "@workspace/api-client-react";

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

function fmtBps(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${n.toFixed(1)} bps`;
}

function fmt(n: number | null | undefined, digits = 3): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(digits);
}

function VerticalCard({ entry }: { entry: VerticalsOverviewEntry }) {
  const isLoss = entry.pnl < 0;
  const isWin = entry.pnl > 0;
  return (
    <div
      className={`border ${entry.hidden ? "border-dashed opacity-60" : "border-border"} bg-card p-4`}
      data-testid={`card-vertical-overview-${entry.vertical}`}
    >
      <div className="flex justify-between items-baseline mb-3">
        <h3 className="text-sm uppercase tracking-widest font-bold text-primary">
          {entry.vertical.toUpperCase()}
          {entry.pinned ? <span className="ml-1 text-primary">★</span> : null}
          {entry.hidden ? (
            <span className="ml-2 text-[10px] text-muted-foreground">(hidden)</span>
          ) : null}
        </h3>
        <Link
          href={`/?verticals=${encodeURIComponent(entry.vertical)}`}
          className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-primary"
          data-testid={`link-vertical-drill-${entry.vertical}`}
        >
          Drill in →
        </Link>
      </div>

      {entry.description && (
        <p className="text-xs text-muted-foreground mb-3">{entry.description}</p>
      )}

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <div className="text-muted-foreground uppercase tracking-widest text-[10px]">
            7d P&amp;L
          </div>
          <div
            className={`font-bold text-base ${
              isWin ? "text-green-500" : isLoss ? "text-destructive" : "text-foreground"
            }`}
            data-testid={`text-pnl-${entry.vertical}`}
          >
            {fmtCurrency(entry.pnl)}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-widest text-[10px]">
            Trades / Win-rate
          </div>
          <div className="font-bold text-base text-foreground">
            {entry.trades} / {fmtPct(entry.win_rate)}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-widest text-[10px]">
            24h Brier
          </div>
          <div className="font-bold text-foreground">{fmt(entry.brier)}</div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-widest text-[10px]">
            Sharpe (7d, ann.)
          </div>
          <div className="font-bold text-foreground">{fmt(entry.sharpe, 2)}</div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-widest text-[10px]">
            Open positions
          </div>
          <div className="font-bold text-foreground">{entry.open_positions}</div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-widest text-[10px]">
            Edge pred / real
          </div>
          <div className="font-bold text-foreground">
            {fmtBps(entry.edge_predicted_bps)} / {fmtBps(entry.edge_realized_bps)}
          </div>
        </div>
      </div>

      <div className="mt-3 pt-3 border-t border-border text-[10px] uppercase tracking-widest text-muted-foreground">
        Last cycle:{" "}
        {entry.last_cycle ? (
          <span className="text-foreground">
            #{entry.last_cycle.id} · {entry.last_cycle.trades_placed ?? 0} trades ·{" "}
            {entry.last_cycle.completed_at ?? entry.last_cycle.started_at ?? "—"}
          </span>
        ) : (
          <span>—</span>
        )}
      </div>
    </div>
  );
}

export default function VerticalsPage() {
  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });
  const isAdmin = !!access?.is_admin;
  const { data, isLoading, error } = useGetVerticalsOverview(
    { days: 7 },
    {
      query: {
        queryKey: getGetVerticalsOverviewQueryKey({ days: 7 }),
        staleTime: 30_000,
      },
    },
  );

  const all = data?.verticals ?? [];
  const visible = isAdmin ? all : all.filter((e) => !e.hidden);

  return (
    <div className="min-h-[100dvh] bg-background text-foreground p-4 md:p-6 font-mono">
      <header className="mb-6 flex items-end justify-between border-b border-border pb-4">
        <div>
          <h1 className="text-xl md:text-2xl font-bold tracking-tight text-primary uppercase">
            Verticals · Overview
          </h1>
          <p className="text-muted-foreground text-xs uppercase tracking-widest mt-1">
            Per-vertical 7-day performance card · {data?.verticals?.length ?? 0} registered
          </p>
        </div>
        <Link
          href="/"
          className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
          data-testid="link-back-dashboard"
        >
          ← Console
        </Link>
      </header>

      {isLoading && (
        <div className="text-xs text-muted-foreground uppercase tracking-widest">
          Loading overview…
        </div>
      )}
      {error && (
        <div className="text-xs text-destructive uppercase tracking-widest">
          Failed to load overview.
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {visible.map((entry) => (
          <VerticalCard key={entry.vertical} entry={entry} />
        ))}
      </div>
    </div>
  );
}
