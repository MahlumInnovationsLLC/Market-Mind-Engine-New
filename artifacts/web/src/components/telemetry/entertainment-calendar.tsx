import { useMemo } from "react";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";

type CalendarItem = {
  source: string;
  signal_id: string;
  signal_type: string;
  title: string;
  event_at: string | null;
  value: number | null;
  model_prob: number | null;
  kalshi_mid: number | null;
  edge_bps: number | null;
  seconds_until_event: number | null;
  imminent: boolean;
};

type BackfillStatus = {
  state: string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number;
  days: number;
  rows_by_source: Record<string, number>;
  rows_total: number;
  sources_failed: string[];
  last_error: string | null;
};

type CalendarResponse = {
  items: CalendarItem[];
  backfill: BackfillStatus | null;
};

function fmtCompletedAt(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "never";
  const diffMs = Date.now() - d.getTime();
  const mins = Math.max(0, Math.floor(diffMs / 60_000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 60 * 24) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 60 / 24)}d ago`;
}

function fmtCountdown(seconds: number | null): string {
  if (seconds === null) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function fmtPct(n: number | null, digits = 1): string {
  if (n === null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtValue(it: CalendarItem): string {
  if (it.value === null || Number.isNaN(it.value)) return "—";
  if (it.signal_type === "BOX") return `$${it.value.toFixed(1)}M`;
  if (it.signal_type === "RT") return `${it.value.toFixed(0)}%`;
  if (it.signal_type === "SPOTIFY" || it.signal_type === "STREAM") return `#${it.value}`;
  if (["OSCAR", "EMMY", "GRAMMY"].includes(it.signal_type)) return fmtPct(it.value);
  return it.value.toString();
}

export default function EntertainmentCalendarPanel() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const { data, isLoading, error } = useQuery<CalendarResponse>({
    queryKey: ["entertainment", "calendar"],
    queryFn: () =>
      apiFetch<CalendarResponse>(
        "/api/entertainment/calendar?within_hours=336&limit=20",
        { getToken: token },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 120_000,
    staleTime: 60_000,
  });

  const items = data?.items ?? [];
  const backfill = data?.backfill ?? null;
  const backfillSources = backfill
    ? Object.entries(backfill.rows_by_source)
    : [];

  return (
    <section className="border border-border rounded p-3 bg-card">
      <header className="flex items-baseline justify-between mb-2">
        <h2 className="text-sm font-bold uppercase tracking-widest text-primary">
          Entertainment Calendar
        </h2>
        <span className="text-[10px] text-muted-foreground uppercase">
          next 14d · box-office · awards · charts
        </span>
      </header>
      <div className="mb-2 text-[10px] font-mono text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="uppercase tracking-wider text-primary/70">
          Backfill
        </span>
        {backfill ? (
          <>
            <span title={backfill.completed_at ?? ""}>
              last: {fmtCompletedAt(backfill.completed_at)}
            </span>
            <span>·</span>
            <span>{backfill.days}d window</span>
            <span>·</span>
            <span>{backfill.rows_total.toLocaleString()} rows</span>
            {backfillSources.length > 0 && (
              <>
                <span>·</span>
                {backfillSources.map(([src, n]) => (
                  <span key={src} className="whitespace-nowrap">
                    {src}: {n.toLocaleString()}
                  </span>
                ))}
              </>
            )}
            {backfill.sources_failed.length > 0 && (
              <span className="text-loss">
                failed: {backfill.sources_failed.join(", ")}
              </span>
            )}
          </>
        ) : (
          <span className="text-warn">never run — model on 0.5 fallback</span>
        )}
      </div>
      {isLoading && (
        <p className="text-xs text-muted-foreground">Loading…</p>
      )}
      {error && (
        <p className="text-xs text-loss">Failed to load entertainment calendar.</p>
      )}
      {!isLoading && !error && items.length === 0 && (
        <p className="text-xs text-muted-foreground">
          No upcoming entertainment events in the next 14 days.
        </p>
      )}
      {items.length > 0 && (
        <ul className="text-xs font-mono divide-y divide-border">
          <li className="grid grid-cols-[5rem_1fr_4rem_4rem_4rem_4rem_4rem] gap-2 py-1 text-[10px] uppercase text-muted-foreground">
            <span>Type</span>
            <span>Title</span>
            <span className="text-right">Signal</span>
            <span className="text-right">Model</span>
            <span className="text-right">Mkt</span>
            <span className="text-right">Edge</span>
            <span className="text-right">In</span>
          </li>
          {items.map((it) => {
            const edgeColor =
              it.edge_bps == null
                ? "text-muted-foreground"
                : it.edge_bps > 250
                  ? "text-profit font-bold"
                  : it.edge_bps < -250
                    ? "text-loss font-bold"
                    : "text-muted-foreground";
            return (
              <li
                key={`${it.source}/${it.signal_id}`}
                className="grid grid-cols-[5rem_1fr_4rem_4rem_4rem_4rem_4rem] gap-2 py-1 items-center"
              >
                <span className="font-bold">
                  {it.signal_type}
                  {it.imminent && (
                    <span className="ml-1 text-[9px] text-warn">●LIVE</span>
                  )}
                </span>
                <span className="text-muted-foreground truncate" title={it.title}>
                  {it.title}
                </span>
                <span className="text-right">{fmtValue(it)}</span>
                <span className="text-right">{fmtPct(it.model_prob)}</span>
                <span className="text-right">{fmtPct(it.kalshi_mid)}</span>
                <span className={"text-right " + edgeColor}>
                  {it.edge_bps == null ? "—" : `${it.edge_bps > 0 ? "+" : ""}${it.edge_bps.toFixed(0)}bp`}
                </span>
                <span
                  className={
                    "text-right " +
                    (it.imminent ? "text-warn font-bold" : "text-muted-foreground")
                  }
                >
                  {fmtCountdown(it.seconds_until_event)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
