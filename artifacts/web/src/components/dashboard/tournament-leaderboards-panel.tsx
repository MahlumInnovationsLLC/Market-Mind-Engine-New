import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

interface LeaderboardEntry {
  position: number | null;
  name: string;
}

interface TournamentLeaderboardEvent {
  event_id: string;
  league: string;
  name: string;
  status: string;
  fetched_at: string | null;
  entries: LeaderboardEntry[];
}

interface TournamentLeaderboardsResponse {
  events: TournamentLeaderboardEvent[];
}

function fmtRelativePast(iso: string | null | undefined): string {
  if (!iso) return "unknown time";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return iso;
  const delta = Math.max(0, Date.now() - t);
  const sec = Math.round(delta / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 48) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}

function leagueTone(league: string): string {
  const l = league.toUpperCase();
  if (l === "PGA") return "border-accent-cyan/60 text-accent-cyan";
  if (l === "F1") return "border-warning/60 text-warning";
  return "border-border text-muted-foreground";
}

function EventCard({ event: ev }: { event: TournamentLeaderboardEvent }) {
  const headline = ev.name || ev.event_id;
  const league = (ev.league || "").toUpperCase();
  return (
    <div
      className="border border-border rounded p-3 bg-card"
      data-testid={`card-tournament-leaderboard-${ev.event_id}`}
    >
      <header className="flex items-baseline justify-between gap-2 mb-2">
        <div className="flex items-baseline gap-2 min-w-0">
          {league ? (
            <span
              className={`border px-1 py-0.5 text-[9px] font-bold uppercase tracking-widest ${leagueTone(league)}`}
              data-testid={`badge-tournament-league-${ev.event_id}`}
            >
              {league}
            </span>
          ) : null}
          <h3
            className="truncate text-sm font-bold uppercase tracking-widest text-primary"
            title={headline}
            data-testid={`text-tournament-name-${ev.event_id}`}
          >
            {headline}
          </h3>
        </div>
        <span
          className="shrink-0 text-[10px] uppercase tracking-widest text-muted-foreground"
          title={ev.fetched_at ?? ""}
          data-testid={`badge-tournament-fetched-${ev.event_id}`}
        >
          fetched {fmtRelativePast(ev.fetched_at)}
        </span>
      </header>
      {ev.entries.length === 0 ? (
        <p
          className="text-xs text-muted-foreground py-2"
          data-testid={`text-tournament-empty-${ev.event_id}`}
        >
          Snapshot recorded but no entries returned.
        </p>
      ) : (
        <ul className="text-xs font-mono divide-y divide-border max-h-72 overflow-y-auto">
          <li className="grid grid-cols-[3rem_1fr] gap-2 py-1 text-[10px] uppercase text-muted-foreground">
            <span>POS</span>
            <span>Name</span>
          </li>
          {ev.entries.map((e, i) => (
            <li
              key={`${ev.event_id}/${i}/${e.name}`}
              className="grid grid-cols-[3rem_1fr] gap-2 py-1 items-center"
              data-testid={`row-tournament-entry-${ev.event_id}-${i}`}
            >
              <span className="text-muted-foreground tabular-nums">
                {e.position == null ? "—" : e.position}
              </span>
              <span className="text-foreground truncate" title={e.name}>
                {e.name}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function TournamentLeaderboardsPanel() {
  const q = useQuery({
    queryKey: ["dashboard-tournament-leaderboards"],
    queryFn: async () =>
      customFetch<TournamentLeaderboardsResponse>(
        "/api/dashboard/tournament-leaderboards",
      ),
    staleTime: 60_000,
    refetchInterval: 120_000,
  });

  const events = q.data?.events ?? [];

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-tournament-leaderboards"
    >
      <SectionHeader
        eyebrow="SPORTS · PGA / F1"
        title="Tournament leaderboards"
        subtitle={
          q.data
            ? `Most recent saved snapshot per event the orchestrator priced off — ${events.length} event${events.length === 1 ? "" : "s"} in the last 7 days.`
            : "Most recent saved leaderboard snapshot per PGA / F1 event."
        }
      />
      {q.isLoading && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          Loading tournament snapshots…
        </div>
      )}
      {q.isError && (
        <div
          className="text-xs text-destructive py-6 text-center"
          data-testid="text-tournament-leaderboards-error"
        >
          Failed to load: {String((q.error as Error)?.message ?? q.error)}
        </div>
      )}
      {q.data && events.length === 0 && (
        <div
          className="text-xs text-muted-foreground py-6 text-center border border-dashed border-border rounded-md"
          data-testid="empty-tournament-leaderboards"
        >
          No snapshots recorded yet.
        </div>
      )}
      {events.length > 0 && (
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          {events.map((ev) => (
            <EventCard key={ev.event_id} event={ev} />
          ))}
        </div>
      )}
    </GlassCard>
  );
}
