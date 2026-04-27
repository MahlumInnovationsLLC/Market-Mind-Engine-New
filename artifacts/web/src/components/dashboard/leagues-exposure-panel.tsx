import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

interface RecentAlert {
  last_sent_at: string | null;
  expires_at: string | null;
  ttl_seconds_remaining: number | null;
  fired_last_cycle: boolean;
  suppressed_last_cycle: boolean;
}

interface ExposureBucket {
  open_usd: number;
  cap_pct: number | null;
  cap_usd: number | null;
  headroom_usd: number | null;
  utilization: number | null;
  warn: boolean;
  recent_alert?: RecentAlert | null;
}

interface PerMatchupBucket extends ExposureBucket {
  matchup: string;
}

interface PerEventBucket extends ExposureBucket {
  event_id: string;
}

interface LeagueEntry {
  league: string;
  per_league: ExposureBucket;
  sunday_window?: ExposureBucket;
  per_event?: PerEventBucket[];
  per_matchup: PerMatchupBucket[];
}

interface RecentWarningsSummary {
  ran_at: string | null;
  threshold: number | null;
  ttl_seconds: number | null;
  fired_count: number;
  suppressed_count: number;
  tracked_buckets: number;
}

interface LeaguesExposureResponse {
  bankroll_usd: number | null;
  warn_threshold: number;
  leagues: LeagueEntry[];
  recent_warnings?: RecentWarningsSummary;
}

function fmtUsd(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `$${n.toFixed(2)}`;
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtAgo(iso: string | null | undefined, now: number): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const sec = Math.max(0, Math.floor((now - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return "—";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const min = Math.floor(seconds / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  const remMin = min % 60;
  return remMin > 0 ? `${hr}h ${remMin}m` : `${hr}h`;
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

function RecentAlertLine({
  alert,
  now,
  testId,
}: {
  alert: RecentAlert | null | undefined;
  now: number;
  testId: string;
}) {
  if (!alert) {
    return (
      <div
        data-testid={`${testId}-recent-alert`}
        data-state="quiet"
        className="text-[10px] text-muted-foreground/70 italic"
      >
        No cap-warn dispatched recently.
      </div>
    );
  }
  const sentAgo = fmtAgo(alert.last_sent_at, now);
  const ttl = fmtDuration(alert.ttl_seconds_remaining);
  const stateBadge = alert.fired_last_cycle
    ? { text: "FIRED THIS CYCLE", tone: "text-warning" }
    : alert.suppressed_last_cycle
      ? { text: "DEDUPE SUPPRESSED", tone: "text-muted-foreground" }
      : { text: "PRIOR PAGE", tone: "text-muted-foreground" };
  return (
    <div
      data-testid={`${testId}-recent-alert`}
      data-state={
        alert.fired_last_cycle
          ? "fired"
          : alert.suppressed_last_cycle
            ? "suppressed"
            : "prior"
      }
      data-last-sent-at={alert.last_sent_at ?? ""}
      className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-muted-foreground border-t border-border/40 pt-1"
    >
      <span className={`uppercase tracking-widest font-semibold ${stateBadge.tone}`}>
        {stateBadge.text}
      </span>
      <span>
        Recently paged:{" "}
        <span className="text-foreground">{sentAgo}</span>
      </span>
      <span>
        dedupe expires in <span className="text-foreground">{ttl}</span>
      </span>
    </div>
  );
}

function BucketRow({
  label,
  bucket,
  testId,
  now,
}: {
  label: string;
  bucket: ExposureBucket;
  testId: string;
  now: number;
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
      <RecentAlertLine alert={bucket.recent_alert} now={now} testId={testId} />
    </div>
  );
}

function LeagueCard({ entry, now }: { entry: LeagueEntry; now: number }) {
  const slug = entry.league.toLowerCase();
  const sub: string[] = [
    `per-league ${fmtPct(entry.per_league.cap_pct, 2)}`,
  ];
  if (entry.per_matchup.length > 0) {
    sub.push(`per-game ${fmtPct(entry.per_matchup[0]?.cap_pct ?? null, 2)}`);
  }
  if (entry.sunday_window) {
    sub.push(`Sunday ${fmtPct(entry.sunday_window.cap_pct, 2)}`);
  }
  if (entry.per_event && entry.per_event.length > 0) {
    sub.push(`per-event ${fmtPct(entry.per_event[0]?.cap_pct ?? null, 2)}`);
  }

  return (
    <div
      className="flex flex-col gap-3 border border-border rounded-md bg-card/30 p-3"
      data-testid={`leagues-exposure-card-${slug}`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-sm font-bold uppercase tracking-wider text-foreground">
          {entry.league}
        </div>
        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {sub.join(" · ")}
        </div>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <BucketRow
          label="Per-league"
          bucket={entry.per_league}
          testId={`leagues-exposure-${slug}-per-league`}
          now={now}
        />
        {entry.sunday_window && (
          <BucketRow
            label="Sunday window"
            bucket={entry.sunday_window}
            testId={`leagues-exposure-${slug}-sunday-window`}
            now={now}
          />
        )}
      </div>
      {entry.per_event && entry.per_event.length > 0 && (
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground mb-2">
            Per active event
          </div>
          <div
            className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2"
            data-testid={`leagues-exposure-${slug}-events`}
          >
            {entry.per_event.map((e) => (
              <BucketRow
                key={e.event_id}
                label={e.event_id}
                bucket={e}
                testId={`leagues-exposure-${slug}-event-${e.event_id}`}
                now={now}
              />
            ))}
          </div>
        </div>
      )}
      <div>
        <div className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground mb-2">
          Per active matchup
        </div>
        {entry.per_matchup.length === 0 ? (
          <div className="text-xs text-muted-foreground py-3 text-center border border-dashed border-border rounded-md">
            No open {entry.league} matchups.
          </div>
        ) : (
          <div
            className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2"
            data-testid={`leagues-exposure-${slug}-matchups`}
          >
            {entry.per_matchup.map((m) => (
              <BucketRow
                key={m.matchup}
                label={m.matchup}
                bucket={m}
                testId={`leagues-exposure-${slug}-matchup-${m.matchup}`}
                now={now}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function RecentWarningsSummaryLine({
  summary,
  now,
}: {
  summary: RecentWarningsSummary | undefined | null;
  now: number;
}) {
  if (!summary || summary.ran_at == null) {
    return (
      <div
        data-testid="leagues-exposure-cap-warn-summary"
        data-state="never-ran"
        className="text-[11px] text-muted-foreground border border-dashed border-border/60 rounded-md p-2"
      >
        Cap-warn dispatcher has not run since process start.
      </div>
    );
  }
  const ago = fmtAgo(summary.ran_at, now);
  const ttl = fmtDuration(summary.ttl_seconds);
  return (
    <div
      data-testid="leagues-exposure-cap-warn-summary"
      data-state="ran"
      data-fired-count={summary.fired_count}
      data-suppressed-count={summary.suppressed_count}
      className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground border border-border/60 rounded-md p-2 bg-card/30"
    >
      <span className="uppercase tracking-widest font-semibold text-foreground">
        Cap-warn dispatcher
      </span>
      <span>
        last ran <span className="text-foreground">{ago}</span>
      </span>
      <span>
        threshold{" "}
        <span className="text-foreground">
          {fmtPct(summary.threshold ?? null, 0)}
        </span>{" "}
        · dedupe TTL{" "}
        <span className="text-foreground">{ttl}</span>
      </span>
      <span data-testid="leagues-exposure-cap-warn-fired">
        fired{" "}
        <span
          className={
            summary.fired_count > 0 ? "text-warning" : "text-foreground"
          }
        >
          {summary.fired_count}
        </span>
      </span>
      <span data-testid="leagues-exposure-cap-warn-suppressed">
        suppressed{" "}
        <span className="text-foreground">{summary.suppressed_count}</span>
      </span>
    </div>
  );
}

function useNow(intervalMs: number): number {
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

export default function LeaguesExposurePanel() {
  const q = useQuery({
    queryKey: ["dashboard-leagues-exposure"],
    queryFn: async () =>
      customFetch<LeaguesExposureResponse>("/api/dashboard/leagues-exposure"),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });

  const data = q.data;
  const leagues = data?.leagues ?? [];
  const now = useNow(15_000);

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-leagues-exposure">
      <SectionHeader
        eyebrow="RISK"
        title="Per-league exposure"
        subtitle={
          data
            ? `Bankroll ${fmtUsd(data.bankroll_usd)} · ${leagues.length} league${leagues.length === 1 ? "" : "s"} with open exposure, sorted by utilization.`
            : "Per-league + per-game cap headroom across every active league."
        }
      />
      {data && (
        <div className="mb-3">
          <RecentWarningsSummaryLine
            summary={data.recent_warnings}
            now={now}
          />
        </div>
      )}
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
      {data && leagues.length === 0 && (
        <div className="text-xs text-muted-foreground py-6 text-center border border-dashed border-border rounded-md">
          No open per-league exposure.
        </div>
      )}
      {data && leagues.length > 0 && (
        <div className="space-y-3">
          {leagues.map((entry) => (
            <LeagueCard key={entry.league} entry={entry} now={now} />
          ))}
        </div>
      )}
    </GlassCard>
  );
}
