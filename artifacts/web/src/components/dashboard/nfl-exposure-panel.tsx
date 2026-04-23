import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

interface ExposureBucket {
  open_usd: number;
  cap_pct: number | null;
  cap_usd: number | null;
  headroom_usd: number | null;
  utilization: number | null;
  warn: boolean;
}

interface PerMatchupBucket extends ExposureBucket {
  matchup: string;
}

interface NflExposureResponse {
  league: string;
  bankroll_usd: number | null;
  warn_threshold: number;
  per_league: ExposureBucket;
  sunday_window: ExposureBucket;
  per_matchup: PerMatchupBucket[];
}

function fmtUsd(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `$${n.toFixed(2)}`;
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function utilTone(b: ExposureBucket): string {
  if (b.utilization == null) return "text-muted-foreground";
  if (b.utilization >= 1) return "text-destructive";
  if (b.warn) return "text-warning";
  return "text-success";
}

function UtilBar({ b }: { b: ExposureBucket }) {
  const u = b.utilization == null ? 0 : Math.max(0, Math.min(1, b.utilization));
  const overflow = b.utilization != null && b.utilization > 1;
  const colorClass = overflow
    ? "bg-destructive"
    : b.warn
      ? "bg-warning"
      : "bg-success";
  return (
    <div className="h-1.5 w-full rounded-full bg-border/40 overflow-hidden">
      <div
        className={`h-full ${colorClass}`}
        style={{ width: `${u * 100}%` }}
      />
    </div>
  );
}

function BucketRow({
  label,
  bucket,
  testId,
}: {
  label: string;
  bucket: ExposureBucket;
  testId: string;
}) {
  return (
    <div
      data-testid={testId}
      data-warn={bucket.warn ? "true" : "false"}
      className="flex flex-col gap-1 border border-border rounded-md bg-card/40 p-3"
    >
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-xs font-semibold uppercase tracking-wider">
          {label}
        </div>
        <div className={`text-[10px] uppercase tracking-widest ${utilTone(bucket)}`}>
          {fmtPct(bucket.utilization)} of cap
        </div>
      </div>
      <UtilBar b={bucket} />
      <div className="grid grid-cols-3 gap-2 text-[10px] text-muted-foreground">
        <div>
          open:{" "}
          <span className="text-foreground">{fmtUsd(bucket.open_usd)}</span>
        </div>
        <div>
          cap:{" "}
          <span className="text-foreground">
            {fmtUsd(bucket.cap_usd)}
            {bucket.cap_pct != null ? ` (${fmtPct(bucket.cap_pct, 2)})` : ""}
          </span>
        </div>
        <div>
          room:{" "}
          <span className="text-foreground">{fmtUsd(bucket.headroom_usd)}</span>
        </div>
      </div>
    </div>
  );
}

export default function NflExposurePanel() {
  const q = useQuery({
    queryKey: ["dashboard-nfl-exposure"],
    queryFn: async () =>
      customFetch<NflExposureResponse>("/api/dashboard/nfl-exposure"),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });

  const data = q.data;
  const matchups = data?.per_matchup ?? [];

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-nfl-exposure">
      <SectionHeader
        eyebrow="RISK"
        title="NFL exposure"
        subtitle={
          data
            ? `Bankroll ${fmtUsd(data.bankroll_usd)} · per-league ${fmtPct(data.per_league.cap_pct, 2)} / per-game ${fmtPct(data.per_matchup[0]?.cap_pct ?? null, 2)} / Sunday ${fmtPct(data.sunday_window.cap_pct, 2)}.`
            : "Per-league + per-game + Sunday-window cap headroom."
        }
      />
      {q.isLoading && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          Loading exposure…
        </div>
      )}
      {q.isError && (
        <div className="text-xs text-destructive py-6 text-center">
          Failed to load: {String((q.error as Error)?.message ?? q.error)}
        </div>
      )}
      {data && (
        <div className="space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <BucketRow
              label="Per-league (NFL)"
              bucket={data.per_league}
              testId="nfl-exposure-per-league"
            />
            <BucketRow
              label="Sunday window"
              bucket={data.sunday_window}
              testId="nfl-exposure-sunday-window"
            />
          </div>
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground mb-2">
              Per active matchup
            </div>
            {matchups.length === 0 ? (
              <div className="text-xs text-muted-foreground py-3 text-center border border-dashed border-border rounded-md">
                No open NFL matchups.
              </div>
            ) : (
              <div
                className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2"
                data-testid="nfl-exposure-matchups"
              >
                {matchups.map((m) => (
                  <BucketRow
                    key={m.matchup}
                    label={m.matchup}
                    bucket={m}
                    testId={`nfl-exposure-matchup-${m.matchup}`}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </GlassCard>
  );
}
