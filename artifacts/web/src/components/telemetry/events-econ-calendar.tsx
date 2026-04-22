import { useMemo } from "react";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";

type CalendarItem = {
  source: string;
  signal_id: string;
  release_type: string;
  release_at: string | null;
  consensus: number | null;
  previous: number | null;
  model_prob: number | null;
  kalshi_mid: number | null;
  edge_bps: number | null;
  seconds_until_release: number | null;
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

function fmtBps(n: number | null): string {
  if (n === null || Number.isNaN(n)) return "—";
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(1)} bps`;
}

function fmtNum(n: number | null): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toString();
}

export default function EventsEconCalendarPanel() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const { data, isLoading, error } = useQuery<CalendarResponse>({
    queryKey: ["events-econ", "calendar"],
    queryFn: () =>
      apiFetch<CalendarResponse>(
        "/api/events-econ/calendar?within_hours=168&limit=20",
        { getToken: token },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const items = data?.items ?? [];

  return (
    <section className="border border-border rounded p-3 bg-card">
      <header className="flex items-baseline justify-between mb-2">
        <h2 className="text-sm font-bold uppercase tracking-widest text-primary">
          Macro Calendar
        </h2>
        <span className="text-[10px] text-muted-foreground uppercase">
          next 7d · cpi/ppi/gdp/fomc/nfp
        </span>
      </header>
      {isLoading && (
        <p className="text-xs text-muted-foreground">Loading…</p>
      )}
      {error && (
        <p className="text-xs text-loss">Failed to load macro calendar.</p>
      )}
      {!isLoading && !error && items.length === 0 && (
        <p className="text-xs text-muted-foreground">
          No upcoming macro releases in the next 7 days.
        </p>
      )}
      {items.length > 0 && (
        <ul className="text-xs font-mono divide-y divide-border">
          <li className="grid grid-cols-[6rem_1fr_5rem_5rem_5rem_5rem] gap-2 py-1 text-[10px] uppercase text-muted-foreground">
            <span>Type</span>
            <span>Consensus / Prev</span>
            <span className="text-right">Model</span>
            <span className="text-right">Kalshi</span>
            <span className="text-right">Edge</span>
            <span className="text-right">In</span>
          </li>
          {items.map((it) => (
            <li
              key={`${it.source}/${it.signal_id}`}
              className="grid grid-cols-[6rem_1fr_5rem_5rem_5rem_5rem] gap-2 py-1 items-center"
            >
              <span className="font-bold">
                {it.release_type}
                {it.imminent && (
                  <span className="ml-1 text-[9px] text-warn">●LIVE</span>
                )}
              </span>
              <span className="text-muted-foreground truncate">
                cons={fmtNum(it.consensus)} / prev={fmtNum(it.previous)}
              </span>
              <span className="text-right">{fmtPct(it.model_prob)}</span>
              <span className="text-right">{fmtPct(it.kalshi_mid)}</span>
              <span
                className={
                  "text-right " +
                  (it.edge_bps === null
                    ? ""
                    : it.edge_bps >= 0
                      ? "text-info"
                      : "text-loss")
                }
              >
                {fmtBps(it.edge_bps)}
              </span>
              <span
                className={
                  "text-right " +
                  (it.imminent ? "text-warn font-bold" : "text-muted-foreground")
                }
              >
                {fmtCountdown(it.seconds_until_release)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
