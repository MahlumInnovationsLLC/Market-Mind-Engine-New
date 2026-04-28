import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

interface RecentEvent {
  event_id: string;
  league: string;
  name: string;
  status: string;
  fetched_at: string | null;
}

interface RecentEventsResponse {
  events: RecentEvent[];
}

interface TimelineEntry {
  position: number | null;
  name: string;
  probability: number | null;
}

interface TimelineSnapshot {
  fetched_at: string | null;
  entries: TimelineEntry[];
}

interface TimelineResponse {
  event_id: string;
  league: string;
  name: string;
  status: string;
  snapshots: TimelineSnapshot[];
}

function fmtTimestamp(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").replace(/\..*$/, "Z");
}

function fmtProbability(p: number | null | undefined): string {
  if (p == null || !Number.isFinite(p)) return "—";
  return `${(p * 100).toFixed(1)}%`;
}

function leagueTone(league: string): string {
  const l = league.toUpperCase();
  if (l === "PGA") return "border-accent-cyan/60 text-accent-cyan";
  if (l === "F1") return "border-warning/60 text-warning";
  return "border-border text-muted-foreground";
}

export default function TournamentTimelinePanel() {
  const eventsQ = useQuery({
    queryKey: ["dashboard-tournament-leaderboards"],
    queryFn: async () =>
      customFetch<RecentEventsResponse>(
        "/api/dashboard/tournament-leaderboards",
      ),
    staleTime: 60_000,
    refetchInterval: 120_000,
  });

  const recentEvents = eventsQ.data?.events ?? [];
  const [selectedEventId, setSelectedEventId] = useState<string>("");

  const activeEventId = useMemo(() => {
    if (selectedEventId && recentEvents.some((e) => e.event_id === selectedEventId)) {
      return selectedEventId;
    }
    return recentEvents[0]?.event_id ?? "";
  }, [selectedEventId, recentEvents]);

  const timelineQ = useQuery({
    queryKey: ["dashboard-tournament-timeline", activeEventId],
    queryFn: async () =>
      customFetch<TimelineResponse>(
        `/api/dashboard/tournament-leaderboard-timeline?event_id=${encodeURIComponent(activeEventId)}`,
      ),
    enabled: activeEventId !== "",
    staleTime: 60_000,
  });

  const snapshots = timelineQ.data?.snapshots ?? [];
  // Default to the latest snapshot so the panel matches the "current
  // field" view operators are used to before they start scrubbing.
  const [tickIndex, setTickIndex] = useState<number | null>(null);

  const lastIndex = snapshots.length - 1;
  const effectiveIndex =
    snapshots.length === 0
      ? -1
      : tickIndex == null || tickIndex < 0 || tickIndex > lastIndex
        ? lastIndex
        : tickIndex;
  const currentSnapshot =
    effectiveIndex >= 0 ? snapshots[effectiveIndex] : null;

  const activeMeta = recentEvents.find((e) => e.event_id === activeEventId);
  const headlineLeague = (
    timelineQ.data?.league ||
    activeMeta?.league ||
    ""
  ).toUpperCase();
  const headlineName =
    timelineQ.data?.name || activeMeta?.name || activeEventId;

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-tournament-timeline"
    >
      <SectionHeader
        eyebrow="SPORTS · PGA / F1"
        title="Tournament leaderboard timeline"
        subtitle="Scrub through every saved snapshot of a finished tournament to see how the field — and our priced Plackett-Luce probabilities — evolved."
      />

      <div className="flex flex-col gap-3 mb-4 sm:flex-row sm:items-end sm:justify-between">
        <label className="flex flex-col gap-1 text-xs uppercase tracking-widest text-muted-foreground">
          Event
          <select
            className="border border-border bg-card text-foreground text-sm font-mono px-2 py-1 rounded"
            value={activeEventId}
            onChange={(e) => {
              setSelectedEventId(e.target.value);
              setTickIndex(null);
            }}
            disabled={recentEvents.length === 0}
            data-testid="select-tournament-timeline-event"
          >
            {recentEvents.length === 0 && (
              <option value="">No recent events</option>
            )}
            {recentEvents.map((ev) => {
              const label = `${(ev.league || "").toUpperCase() || "—"} · ${ev.name || ev.event_id}`;
              return (
                <option key={ev.event_id} value={ev.event_id}>
                  {label}
                </option>
              );
            })}
          </select>
        </label>
        {headlineLeague || activeEventId ? (
          <div
            className="flex items-baseline gap-2"
            data-testid="text-tournament-timeline-headline"
          >
            {headlineLeague ? (
              <span
                className={`border px-1 py-0.5 text-[9px] font-bold uppercase tracking-widest ${leagueTone(headlineLeague)}`}
              >
                {headlineLeague}
              </span>
            ) : null}
            <span className="text-sm font-bold uppercase tracking-widest text-primary truncate">
              {headlineName}
            </span>
          </div>
        ) : null}
      </div>

      {eventsQ.isLoading && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          Loading events…
        </div>
      )}
      {eventsQ.isError && (
        <div
          className="text-xs text-destructive py-6 text-center"
          data-testid="text-tournament-timeline-events-error"
        >
          Failed to load events:{" "}
          {String((eventsQ.error as Error)?.message ?? eventsQ.error)}
        </div>
      )}
      {eventsQ.data && recentEvents.length === 0 && (
        <div
          className="text-xs text-muted-foreground py-6 text-center border border-dashed border-border rounded-md"
          data-testid="empty-tournament-timeline-events"
        >
          No snapshots recorded yet.
        </div>
      )}

      {activeEventId && (
        <>
          {timelineQ.isLoading && (
            <div className="text-xs text-muted-foreground py-6 text-center">
              Loading timeline…
            </div>
          )}
          {timelineQ.isError && (
            <div
              className="text-xs text-destructive py-6 text-center"
              data-testid="text-tournament-timeline-error"
            >
              Failed to load timeline:{" "}
              {String((timelineQ.error as Error)?.message ?? timelineQ.error)}
            </div>
          )}
          {timelineQ.data && snapshots.length === 0 && (
            <div
              className="text-xs text-muted-foreground py-6 text-center border border-dashed border-border rounded-md"
              data-testid="empty-tournament-timeline"
            >
              No snapshots saved for this event yet.
            </div>
          )}
          {snapshots.length > 0 && (
            <div className="space-y-3">
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground">
                  <span data-testid="text-tournament-timeline-tick-position">
                    Snapshot {effectiveIndex + 1} / {snapshots.length}
                  </span>
                  <span
                    className="text-foreground font-mono"
                    data-testid="text-tournament-timeline-tick-fetched-at"
                    title={currentSnapshot?.fetched_at ?? ""}
                  >
                    {fmtTimestamp(currentSnapshot?.fetched_at)}
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={lastIndex}
                  step={1}
                  value={effectiveIndex}
                  onChange={(e) => setTickIndex(Number(e.target.value))}
                  className="w-full accent-accent-cyan"
                  aria-label="Snapshot timeline scrubber"
                  data-testid="input-tournament-timeline-slider"
                />
                <div className="flex items-center justify-between text-[10px] uppercase tracking-widest text-muted-foreground font-mono">
                  <span>{fmtTimestamp(snapshots[0]?.fetched_at)}</span>
                  <span>{fmtTimestamp(snapshots[lastIndex]?.fetched_at)}</span>
                </div>
              </div>

              {currentSnapshot && currentSnapshot.entries.length === 0 ? (
                <p
                  className="text-xs text-muted-foreground py-2"
                  data-testid="text-tournament-timeline-empty-snapshot"
                >
                  Snapshot recorded but no entries returned.
                </p>
              ) : (
                <ul
                  className="text-xs font-mono divide-y divide-border max-h-96 overflow-y-auto"
                  data-testid="list-tournament-timeline-entries"
                >
                  <li className="grid grid-cols-[3rem_1fr_5rem] gap-2 py-1 text-[10px] uppercase text-muted-foreground sticky top-0 bg-card">
                    <span>POS</span>
                    <span>Name</span>
                    <span className="text-right">PL prob</span>
                  </li>
                  {currentSnapshot?.entries.map((e, i) => (
                    <li
                      key={`${i}/${e.name}`}
                      className="grid grid-cols-[3rem_1fr_5rem] gap-2 py-1 items-center"
                      data-testid={`row-tournament-timeline-entry-${i}`}
                    >
                      <span className="text-muted-foreground tabular-nums">
                        {e.position == null ? "—" : e.position}
                      </span>
                      <span className="text-foreground truncate" title={e.name}>
                        {e.name}
                      </span>
                      <span
                        className="text-right text-accent-cyan tabular-nums"
                        data-testid={`text-tournament-timeline-prob-${i}`}
                      >
                        {fmtProbability(e.probability)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}
    </GlassCard>
  );
}
