import {
  getGetVerticalsOverviewQueryKey,
  useGetVerticalsOverview,
  type VerticalsOverviewEntry,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function fmtBps(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${n.toFixed(1)} bps`;
}

function EdgeCell({
  label,
  value,
  tone,
  testId,
}: {
  label: string;
  value: number | null | undefined;
  tone: "muted" | "predicted" | "realized";
  testId: string;
}) {
  const cls =
    tone === "predicted"
      ? "text-primary"
      : tone === "realized"
        ? value !== null && value !== undefined
          ? value > 0
            ? "text-success"
            : value < 0
              ? "text-destructive"
              : "text-foreground"
          : "text-muted-foreground"
        : "text-muted-foreground";
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div className={`text-sm font-bold tabular-nums ${cls}`} data-testid={testId}>
        {fmtBps(value)}
      </div>
    </div>
  );
}

function EdgeBar({ entry }: { entry: VerticalsOverviewEntry }) {
  const pred = entry.edge_predicted_bps ?? null;
  const real = entry.edge_realized_bps ?? null;
  const diff =
    pred !== null && real !== null ? real - pred : null;
  // Bar scaled to ±200 bps for a stable visual baseline; values past
  // that just clamp at 100% width which is fine for a "small multiple".
  const scale = (v: number | null) =>
    v === null ? 0 : Math.min(1, Math.abs(v) / 200);
  return (
    <div
      className="border border-border bg-card/40 p-3"
      data-testid={`card-edge-${entry.vertical}`}
    >
      <div className="mb-2 flex items-baseline justify-between">
        <h3 className="text-[11px] font-bold uppercase tracking-widest text-primary">
          {entry.vertical.toUpperCase()}
        </h3>
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
          N={entry.trades}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <EdgeCell
          label="Predicted"
          value={pred}
          tone="predicted"
          testId={`text-edge-predicted-${entry.vertical}`}
        />
        <EdgeCell
          label="Realized"
          value={real}
          tone="realized"
          testId={`text-edge-realized-${entry.vertical}`}
        />
        <EdgeCell label="Δ" value={diff} tone="realized" testId={`text-edge-delta-${entry.vertical}`} />
      </div>
      <div className="mt-2 grid grid-cols-2 gap-1">
        <div className="h-1.5 w-full bg-muted/20">
          <div
            className="h-full bg-primary/60"
            style={{ width: `${(scale(pred) * 100).toFixed(0)}%` }}
          />
        </div>
        <div className="h-1.5 w-full bg-muted/20">
          <div
            className={`h-full ${
              real === null
                ? "bg-muted"
                : real > 0
                  ? "bg-success/70"
                  : real < 0
                    ? "bg-destructive/70"
                    : "bg-muted"
            }`}
            style={{ width: `${(scale(real) * 100).toFixed(0)}%` }}
          />
        </div>
      </div>
    </div>
  );
}

export default function EdgePanel() {
  const params = { days: 7 };
  const { data, isLoading, isError } = useGetVerticalsOverview(params, {
    query: {
      queryKey: getGetVerticalsOverviewQueryKey(params),
      refetchInterval: 60_000,
      staleTime: 30_000,
    },
  });

  const verticals = (data?.verticals ?? []).filter(
    (v) =>
      v.edge_predicted_bps !== null ||
      v.edge_realized_bps !== null ||
      v.trades > 0,
  );

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-edge">
      <SectionHeader
        eyebrow="EDGE"
        title="Realized vs predicted (bps)"
        subtitle="7d small-multiples · positive realized = model captured edge."
      />
      {isLoading ? (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          <div className="h-28 animate-pulse bg-muted/10" />
          <div className="h-28 animate-pulse bg-muted/10" />
          <div className="h-28 animate-pulse bg-muted/10" />
        </div>
      ) : isError ? (
        <div className="border border-destructive/40 p-4 text-center text-xs uppercase tracking-widest text-destructive">
          ERR_EDGE_LOAD
        </div>
      ) : verticals.length === 0 ? (
        <div className="border border-border bg-card/40 p-4 text-center text-xs uppercase tracking-widest text-muted-foreground">
          NO_EDGE_DATA
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
          {verticals.map((v) => (
            <EdgeBar key={v.vertical} entry={v} />
          ))}
        </div>
      )}
    </GlassCard>
  );
}
