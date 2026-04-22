import { useLocation } from "wouter";
import {
  getGetMyAccessQueryKey,
  getGetVerticalsOverviewQueryKey,
  useGetMyAccess,
  useGetVerticalsOverview,
  type VerticalsOverviewEntry,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

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

interface VerticalCardProps {
  entry: VerticalsOverviewEntry;
  onDrillIn: (vertical: string) => void;
}

function VerticalCard({ entry, onDrillIn }: VerticalCardProps) {
  const isLoss = entry.pnl < 0;
  const isWin = entry.pnl > 0;
  const pnlClass = isWin
    ? "text-success"
    : isLoss
      ? "text-destructive"
      : "text-foreground";

  const onActivate = () => onDrillIn(entry.vertical);

  return (
    <div
      className={`group relative cursor-pointer border bg-card p-4 transition-colors hover:border-primary ${
        entry.hidden ? "border-dashed opacity-60" : "border-border"
      }`}
      data-testid={`card-vertical-overview-${entry.vertical}`}
      role="link"
      tabIndex={0}
      onClick={onActivate}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onActivate();
        }
      }}
    >
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="text-sm font-bold uppercase tracking-widest text-primary">
          {entry.vertical.toUpperCase()}
          {entry.pinned && <span className="ml-1 text-primary">★</span>}
          {entry.hidden && (
            <span className="ml-2 text-[10px] text-muted-foreground">
              (hidden)
            </span>
          )}
        </h3>
        <span
          className="text-[10px] uppercase tracking-widest text-muted-foreground transition-colors group-hover:text-primary"
          data-testid={`link-vertical-drill-${entry.vertical}`}
        >
          Drill in →
        </span>
      </div>

      {entry.description && (
        <p className="mb-3 text-xs text-muted-foreground line-clamp-2">
          {entry.description}
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            7d P&amp;L
          </div>
          <div
            className={`text-base font-bold tabular-nums ${pnlClass}`}
            data-testid={`text-pnl-${entry.vertical}`}
          >
            {fmtCurrency(entry.pnl)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Trades / Win-rate
          </div>
          <div className="text-base font-bold tabular-nums text-foreground">
            {entry.trades} / {fmtPct(entry.win_rate)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            24h Brier
          </div>
          <div className="font-bold tabular-nums">{fmt(entry.brier)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Sharpe (7d, ann.)
          </div>
          <div className="font-bold tabular-nums">{fmt(entry.sharpe, 2)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Open positions
          </div>
          <div className="font-bold tabular-nums">{entry.open_positions}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Edge pred / real
          </div>
          <div className="font-bold tabular-nums">
            {fmtBps(entry.edge_predicted_bps)} / {fmtBps(entry.edge_realized_bps)}
          </div>
        </div>
      </div>

      <div className="mt-3 border-t border-border pt-3 text-[10px] uppercase tracking-widest text-muted-foreground">
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
  const [, setLocation] = useLocation();
  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });
  const isAdmin = !!access?.is_admin;

  const params = { days: 7 };
  const { data, isLoading, error } = useGetVerticalsOverview(params, {
    query: {
      queryKey: getGetVerticalsOverviewQueryKey(params),
      staleTime: 30_000,
      refetchInterval: 60_000,
    },
  });

  const all = data?.verticals ?? [];
  const visible = isAdmin ? all : all.filter((e) => !e.hidden);

  const drillIn = (vertical: string) => {
    setLocation(`/app?selectedVerticals=${encodeURIComponent(vertical)}`);
  };

  return (
    <div className="space-y-5 font-mono" data-testid="page-verticals">
      <header className="flex flex-col gap-1">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
          MARKETS
        </div>
        <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
          Verticals · 7-day overview
        </h1>
        <p className="mt-1 text-xs text-muted-foreground">
          {visible.length} registered · click a card to scope the dashboard.
        </p>
      </header>

      <GlassCard tone="solid" padding="md" data-testid="card-verticals-grid">
        <SectionHeader
          eyebrow="GRID"
          title="Per-vertical performance"
          subtitle="P&L · trades · brier · open positions · edge bps · last cycle."
        />
        {isLoading ? (
          <div
            className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3"
            data-testid="skeleton-verticals"
          >
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="h-56 animate-pulse bg-muted/10" />
            ))}
          </div>
        ) : error ? (
          <div
            className="border border-destructive/40 p-4 text-center text-xs uppercase tracking-widest text-destructive"
            data-testid="text-verticals-error"
          >
            ERR_OVERVIEW_LOAD
          </div>
        ) : visible.length === 0 ? (
          <div className="border border-border bg-card/40 p-4 text-center text-xs uppercase tracking-widest text-muted-foreground">
            NO_VERTICALS_REGISTERED
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {visible.map((entry) => (
              <VerticalCard
                key={entry.vertical}
                entry={entry}
                onDrillIn={drillIn}
              />
            ))}
          </div>
        )}
      </GlassCard>
    </div>
  );
}
