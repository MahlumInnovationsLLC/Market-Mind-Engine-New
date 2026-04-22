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
  seconds_until_event: number | null;
  imminent: boolean;
};

type CalendarResponse = {
  items: CalendarItem[];
};

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
          <li className="grid grid-cols-[5rem_1fr_5rem_5rem_5rem] gap-2 py-1 text-[10px] uppercase text-muted-foreground">
            <span>Type</span>
            <span>Title</span>
            <span className="text-right">Signal</span>
            <span className="text-right">Model</span>
            <span className="text-right">In</span>
          </li>
          {items.map((it) => (
            <li
              key={`${it.source}/${it.signal_id}`}
              className="grid grid-cols-[5rem_1fr_5rem_5rem_5rem] gap-2 py-1 items-center"
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
              <span
                className={
                  "text-right " +
                  (it.imminent ? "text-warn font-bold" : "text-muted-foreground")
                }
              >
                {fmtCountdown(it.seconds_until_event)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
