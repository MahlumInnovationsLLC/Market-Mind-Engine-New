import type {
  SupabaseGrowthRetentionLoop,
  SupabaseGrowthRetentionStatus,
} from "@workspace/api-client-react";
import {
  SupabaseGrowthRetentionLoopHealth,
  getGetSupabaseGrowthRetentionStatusQueryKey,
  useGetSupabaseGrowthRetentionStatus,
} from "@workspace/api-client-react";
import { HardDrive } from "lucide-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

function fmtUnix(ts: number | null | undefined): string {
  if (ts === null || ts === undefined) return "—";
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${seconds.toFixed(0)}s ago`;
  const m = seconds / 60;
  if (m < 90) return `${m.toFixed(0)}m ago`;
  const h = m / 60;
  if (h < 48) return `${h.toFixed(1)}h ago`;
  return `${(h / 24).toFixed(1)}d ago`;
}

function fmtNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString();
}

function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  const kb = n / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KiB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MiB`;
  return `${(mb / 1024).toFixed(2)} GiB`;
}

const HEALTH_PILL: Record<
  SupabaseGrowthRetentionLoopHealth,
  { label: string; pill: string }
> = {
  ok: {
    label: "OK",
    pill: "border border-success/60 text-success bg-success/10",
  },
  stale: {
    label: "STALE",
    pill: "border border-warn/60 text-warn bg-warn/10",
  },
  error: {
    label: "ERROR",
    pill: "border border-loss/60 text-loss bg-loss/10",
  },
  disabled: {
    label: "DISABLED",
    pill: "border border-border text-muted-foreground bg-muted/10",
  },
};

export default function SupabaseGrowthRetentionPanel() {
  const { data, isLoading, isError } = useGetSupabaseGrowthRetentionStatus({
    query: {
      queryKey: getGetSupabaseGrowthRetentionStatusQueryKey(),
      refetchInterval: 60_000,
      staleTime: 30_000,
    },
  });

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-supabase-growth-retention"
    >
      <SectionHeader
        eyebrow="STORAGE"
        title="Supabase growth"
        subtitle="Archive-then-delete and delete-only loops that bound the size of the operational + ML tables."
      />

      {isLoading ? (
        <div
          className="mt-3 h-32 animate-pulse border border-border bg-muted/10"
          data-testid="supabase-growth-retention-loading"
        />
      ) : isError || !data ? (
        <div
          className="mt-3 border border-loss/60 bg-loss/10 p-3 text-xs text-loss"
          data-testid="supabase-growth-retention-error"
        >
          ERR_RETENTION_FETCH_FAIL
        </div>
      ) : (
        <SupabaseGrowthRetentionBody data={data} />
      )}
    </GlassCard>
  );
}

function SupabaseGrowthRetentionBody({
  data,
}: {
  data: SupabaseGrowthRetentionStatus;
}) {
  const { counts, total } = data;
  return (
    <div
      className="mt-3 space-y-3 text-xs"
      data-testid="supabase-growth-retention-body"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="px-2 py-0.5 text-[10px] uppercase tracking-widest border border-border text-muted-foreground"
          data-testid="supabase-growth-retention-summary"
        >
          {counts.disabled} disabled / {counts.ok} ok / {counts.stale} stale /{" "}
          {counts.error} error
        </span>
        <span className="px-2 py-0.5 text-[10px] uppercase tracking-widest border border-border text-muted-foreground">
          {total} loops
        </span>
        <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <HardDrive className="h-3 w-3" aria-hidden />
          supabase storage
        </span>
      </div>

      <RetentionLoopSection
        title="Archive-then-delete"
        subtitle="ML / training tables — streamed to Supabase Storage as gzip JSONL before deletion."
        loops={data.archive}
        groupTestId="supabase-growth-retention-archive"
      />
      <RetentionLoopSection
        title="Delete-only"
        subtitle="Operational / audit logs — pure DELETE past the retention horizon."
        loops={data.delete_only}
        groupTestId="supabase-growth-retention-delete-only"
      />
    </div>
  );
}

function RetentionLoopSection({
  title,
  subtitle,
  loops,
  groupTestId,
}: {
  title: string;
  subtitle: string;
  loops: SupabaseGrowthRetentionLoop[];
  groupTestId: string;
}) {
  return (
    <div data-testid={groupTestId}>
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {title}
        </div>
        <div className="text-[10px] text-muted-foreground/70">{subtitle}</div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs" data-testid={`${groupTestId}-table`}>
          <thead>
            <tr className="text-left text-[10px] uppercase tracking-widest text-muted-foreground">
              <th className="py-1 pr-3">Loop</th>
              <th className="py-1 pr-3">Health</th>
              <th className="py-1 pr-3 text-right">Last sweep</th>
              <th className="py-1 pr-3 text-right">Last deleted</th>
              <th className="py-1 pr-3 text-right">Total deleted</th>
              <th className="py-1 pr-3 text-right">Archived</th>
              <th className="py-1 pr-3">Last error</th>
            </tr>
          </thead>
          <tbody>
            {loops.length === 0 ? (
              <tr>
                <td className="py-2 text-muted-foreground" colSpan={7}>
                  No loops registered.
                </td>
              </tr>
            ) : (
              loops.map((loop) => (
                <RetentionLoopRow
                  key={loop.name}
                  loop={loop}
                  groupTestId={groupTestId}
                />
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RetentionLoopRow({
  loop,
  groupTestId,
}: {
  loop: SupabaseGrowthRetentionLoop;
  groupTestId: string;
}) {
  const tone = HEALTH_PILL[loop.health];
  const archivedSummary =
    loop.flavor === "archive"
      ? `${fmtNumber(loop.last_archived_rows)} rows · ${fmtBytes(
          loop.last_bytes_compressed,
        )}`
      : "—";
  return (
    <tr
      className="border-t border-border/50 align-top"
      data-testid={`${groupTestId}-row-${loop.name}`}
    >
      <td className="py-1 pr-3">
        <div className="font-mono text-foreground">{loop.name}</div>
        {loop.table ? (
          <div className="text-[10px] text-muted-foreground">{loop.table}</div>
        ) : null}
      </td>
      <td className="py-1 pr-3">
        <span
          className={`inline-block px-1.5 py-0.5 text-[10px] uppercase tracking-widest ${tone.pill}`}
          data-testid={`${groupTestId}-health-${loop.name}`}
        >
          {tone.label}
        </span>
      </td>
      <td className="py-1 pr-3 text-right tabular-nums">
        <div className="font-mono">{fmtUnix(loop.last_tick_at)}</div>
        <div className="text-[10px] text-muted-foreground">
          {fmtDuration(loop.seconds_since_last_tick)}
        </div>
      </td>
      <td className="py-1 pr-3 text-right tabular-nums">
        {fmtNumber(loop.last_deleted)}
      </td>
      <td className="py-1 pr-3 text-right tabular-nums">
        {fmtNumber(loop.total_deleted)}
      </td>
      <td className="py-1 pr-3 text-right tabular-nums text-muted-foreground">
        {archivedSummary}
      </td>
      <td className="py-1 pr-3">
        {loop.last_error ? (
          <pre
            className="whitespace-pre-wrap break-words font-mono text-[10px] text-loss/90"
            data-testid={`${groupTestId}-error-${loop.name}`}
          >
            {loop.last_error}
          </pre>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </td>
    </tr>
  );
}
