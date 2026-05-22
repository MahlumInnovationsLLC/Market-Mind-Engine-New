import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useSearch } from "wouter";
import { useAuth } from "@clerk/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { apiFetch, formatErrorMessage } from "@/lib/api";

type LiveGameItem = {
  market_id: string;
  game_id: string;
  league: string;
  is_live: boolean;
  action: string | null;
  home_score: number | null;
  away_score: number | null;
  period: string | number | null;
  time_remaining: string | null;
  game_progress: number | null;
  pre_game_prob: number | null;
  updated_prob: number | null;
  market_price: number | null;
  current_edge: number | null;
  last_update: string | null;
  swarm: {
    id: number;
    matchup: string | null;
    market_type: string | null;
    recommended_action: string | null;
    consensus_probability: number | null;
    agreement_rate: number | null;
    approved: boolean | null;
    confidence_band_low: number | null;
    confidence_band_high: number | null;
    created_at: string | null;
  } | null;
  tick: {
    close_cents: number | null;
    mean_cents: number | null;
    bucket_ts: string | null;
    fetched_at: string | null;
  } | null;
};

type LiveGamesResponse = {
  items: LiveGameItem[];
  is_live_now_count: number;
  window_seconds: number;
  fetched_at: string;
};

type UpcomingGameItem = {
  market_id: string;
  league: string;
  home_team: string;
  away_team: string;
  scheduled: string | null;
  status: string | null;
  venue: string | null;
  intangibles_count: number;
};

type UpcomingGamesResponse = {
  items: UpcomingGameItem[];
  count: number;
  window_hours: number;
  fetched_at: string;
};

type AskResponse = {
  market_id: string;
  answer: string;
  model: string;
  provider: string;
  context_summary: {
    kind?: "live" | "upcoming";
    has_data: boolean;
    timeline_rows?: number;
    ticks_rows?: number;
    has_swarm_debate?: boolean;
    intangibles_rows?: number;
    has_schedule?: boolean;
    has_player_status?: boolean;
  };
};

type ModelRow = {
  provider: string;
  model_id: string;
  label: string;
  available: boolean;
};

type ModelsResponse = {
  models: ModelRow[];
  default_model_id: string | null;
};

type SelectedMarket = {
  market_id: string;
  label: string;
  kind: "live" | "upcoming";
};

function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

function fmtCents(c: number | null | undefined): string {
  if (c === null || c === undefined) return "—";
  return `${c}¢`;
}

function fmtScore(home: number | null, away: number | null): string {
  if (home === null && away === null) return "—";
  return `${home ?? "?"} – ${away ?? "?"}`;
}

function fmtAge(iso: string | null): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "—";
  const ageSec = Math.max(0, (Date.now() - ts) / 1000);
  if (ageSec < 60) return `${Math.round(ageSec)}s ago`;
  if (ageSec < 3600) return `${Math.round(ageSec / 60)}m ago`;
  if (ageSec < 86400) return `${Math.round(ageSec / 3600)}h ago`;
  return `${Math.round(ageSec / 86400)}d ago`;
}

// Format an ISO timestamp as "in 3h 42m" / "starting now" / "started 12m ago"
// — used by the upcoming-games strip so users see scheduled-time at a glance.
function fmtCountdown(iso: string | null): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "—";
  const deltaSec = (ts - Date.now()) / 1000;
  if (Math.abs(deltaSec) < 90) return "starting now";
  if (deltaSec < 0) {
    const absSec = -deltaSec;
    if (absSec < 3600) return `started ${Math.round(absSec / 60)}m ago`;
    return `started ${Math.round(absSec / 3600)}h ago`;
  }
  if (deltaSec < 3600) return `in ${Math.round(deltaSec / 60)}m`;
  const hours = Math.floor(deltaSec / 3600);
  const mins = Math.round((deltaSec - hours * 3600) / 60);
  if (hours < 24) return `in ${hours}h${mins ? ` ${mins}m` : ""}`;
  const days = Math.floor(hours / 24);
  return `in ${days}d ${hours - days * 24}h`;
}

export default function LiveGamesPage() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  // Deep-link filter: when the dashboard's Live-Now tile links here with
  // `?league=nba`, pre-filter both the live and upcoming lists to that
  // sport so operators land in context. Case-insensitive — the tile emits
  // lowercase ("nba") but the live-games payload may use uppercase leagues.
  const search = useSearch();
  const [, setLocation] = useLocation();
  const initialLeague = useMemo(
    () => new URLSearchParams(search).get("league") ?? "",
    // Only seed once on mount; further URL changes shouldn't clobber a
    // user-driven selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [leagueFilter, setLeagueFilter] = useState<string>(initialLeague);
  const clearLeagueFilter = useCallback(() => {
    setLeagueFilter("");
    const next = new URLSearchParams(search);
    next.delete("league");
    const qs = next.toString();
    setLocation(qs ? `/live-games?${qs}` : "/live-games", { replace: true });
  }, [search, setLocation]);

  const { data, isLoading, error, refetch, isFetching } = useQuery<LiveGamesResponse>({
    queryKey: ["live-games", "list"],
    queryFn: () =>
      apiFetch<LiveGamesResponse>("/api/live-games?limit=50", {
        getToken: tokenFn,
      }),
    refetchInterval: 10_000,
    enabled: !!isSignedIn,
  });

  // Schedule-driven, refresh slowly (60s) — the schedule cache only
  // moves when sportradar's daily schedule feed reruns.
  const {
    data: upcomingData,
    isLoading: upcomingLoading,
    error: upcomingError,
  } = useQuery<UpcomingGamesResponse>({
    queryKey: ["upcoming-games", "list"],
    queryFn: () =>
      apiFetch<UpcomingGamesResponse>(
        "/api/upcoming-games?window_hours=24&limit=50",
        { getToken: tokenFn },
      ),
    refetchInterval: 60_000,
    enabled: !!isSignedIn,
  });

  const [selected, setSelected] = useState<SelectedMarket | null>(null);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<AskResponse | null>(null);
  const [chosenModel, setChosenModel] = useState<string>("");

  const { data: modelsData } = useQuery<ModelsResponse>({
    queryKey: ["live-games", "models"],
    queryFn: () =>
      apiFetch<ModelsResponse>("/api/live-games/models", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    staleTime: 60_000,
  });

  // Adopt the server-suggested default the first time models load.
  // Doing this in useEffect avoids the render-phase setState anti-pattern
  // (would otherwise fire on every render until the state caught up).
  useEffect(() => {
    if (!chosenModel && modelsData?.default_model_id) {
      setChosenModel(modelsData.default_model_id);
    }
  }, [chosenModel, modelsData?.default_model_id]);

  const askMutation = useMutation({
    mutationFn: async (vars: {
      market_id: string;
      question: string;
      model: string;
    }) =>
      apiFetch<AskResponse>(
        `/api/live-games/${encodeURIComponent(vars.market_id)}/ask`,
        {
          method: "POST",
          body: { question: vars.question, model: vars.model || undefined },
          getToken: tokenFn,
        },
      ),
    onSuccess: (resp) => setAnswer(resp),
  });

  const onAsk = useCallback(() => {
    if (!selected || !question.trim()) return;
    setAnswer(null);
    askMutation.mutate({
      market_id: selected.market_id,
      question: question.trim(),
      model: chosenModel,
    });
  }, [selected, question, chosenModel, askMutation]);

  const selectLive = useCallback(
    (it: LiveGameItem) =>
      setSelected({
        market_id: it.market_id,
        label: it.swarm?.matchup ?? it.market_id,
        kind: "live",
      }),
    [],
  );
  const selectUpcoming = useCallback(
    (it: UpcomingGameItem) =>
      setSelected({
        market_id: it.market_id,
        label: `${it.away_team} @ ${it.home_team}`,
        kind: "upcoming",
      }),
    [],
  );

  const items = data?.items ?? [];
  const liveNowCount = data?.is_live_now_count ?? 0;

  const normalizedFilter = leagueFilter.trim().toLowerCase();
  const matchesLeague = useCallback(
    (league: string) =>
      !normalizedFilter || league.toLowerCase() === normalizedFilter,
    [normalizedFilter],
  );

  const grouped = useMemo(() => {
    const m = new Map<string, LiveGameItem[]>();
    for (const it of items) {
      if (!matchesLeague(it.league)) continue;
      const arr = m.get(it.league) ?? [];
      arr.push(it);
      m.set(it.league, arr);
    }
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [items, matchesLeague]);

  const upcomingItems = upcomingData?.items ?? [];
  const upcomingGrouped = useMemo(() => {
    const m = new Map<string, UpcomingGameItem[]>();
    for (const it of upcomingItems) {
      if (!matchesLeague(it.league)) continue;
      const arr = m.get(it.league) ?? [];
      arr.push(it);
      m.set(it.league, arr);
    }
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [upcomingItems, matchesLeague]);

  return (
    <div className="space-y-5 font-mono" data-testid="page-live-games">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              LIVE GAMES
            </div>
            <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
              In-progress markets · Mid-game signals
            </h1>
            <p className="mt-1 text-xs text-muted-foreground">
              {liveNowCount > 0
                ? `${liveNowCount} market${liveNowCount === 1 ? "" : "s"} live right now · auto-refreshes every 10s`
                : "No markets currently live · showing recent in-game activity (last 6h)"}
            </p>
            {leagueFilter ? (
              <div
                data-testid="live-games-league-filter"
                className="mt-2 inline-flex items-center gap-2 self-start rounded-full border border-accent-cyan/40 bg-accent-cyan/10 px-3 py-1 text-[11px] uppercase tracking-wider text-accent-cyan"
              >
                <span>Filtered: {leagueFilter}</span>
                <button
                  type="button"
                  onClick={clearLeagueFilter}
                  data-testid="live-games-clear-league-filter"
                  className="rounded text-accent-cyan hover:text-foreground focus:outline-none"
                  aria-label="Clear league filter"
                >
                  ✕
                </button>
              </div>
            ) : null}
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span data-testid="live-games-fetched-at">
              Updated {fmtAge(data?.fetched_at ?? null)}
            </span>
            {isFetching && <span className="text-accent-cyan">↻</span>}
          </div>
        </div>
      </header>

      <GlassCard className="p-4" data-testid="live-games-chatbot">
        <SectionHeader
          title="Ask the analyst"
          subtitle={
            selected
              ? `Grounded in ${selected.kind === "upcoming" ? "pre-game" : "live"} data for ${selected.label}`
              : "Click a market card below to ground the chatbot in that game's data"
          }
        />
        <div className="mt-3 flex flex-col gap-2">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={
              selected
                ? selected.kind === "upcoming"
                  ? "e.g. Who's hurt? What does the model think the fair value is? How has the line moved?"
                  : "e.g. What's the model's edge right now? Is the swarm approving the trade?"
                : "Select a game first, then ask about injuries, intangibles, the spread, or in-game bet sizing."
            }
            disabled={!selected || askMutation.isPending}
            rows={3}
            data-testid="live-games-chatbot-input"
            className="rounded-md border border-border bg-background/60 p-2 text-xs text-foreground placeholder:text-muted-foreground focus:border-accent-cyan focus:outline-none disabled:opacity-50"
          />
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
              <span>
                {selected
                  ? `${selected.kind === "upcoming" ? "Upcoming" : "Live"}: ${selected.market_id}`
                  : "No market selected"}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <label
                htmlFor="live-games-model"
                className="text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                Model
              </label>
              <select
                id="live-games-model"
                value={chosenModel}
                onChange={(e) => setChosenModel(e.target.value)}
                data-testid="live-games-chatbot-model"
                className="rounded-md border border-border bg-background/60 px-2 py-1 text-[11px] text-foreground focus:border-accent-cyan focus:outline-none"
              >
                {(modelsData?.models ?? []).map((m) => (
                  <option
                    key={m.model_id}
                    value={m.model_id}
                    disabled={!m.available}
                  >
                    {m.label}
                    {m.available ? "" : " (no API key)"}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={onAsk}
                disabled={
                  !selected || !question.trim() || askMutation.isPending
                }
                data-testid="live-games-chatbot-ask"
                className="rounded-md border border-accent-cyan/40 bg-accent-cyan/10 px-3 py-1 text-xs font-semibold uppercase tracking-wider text-accent-cyan hover:bg-accent-cyan/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {askMutation.isPending ? "Thinking…" : "Ask"}
              </button>
            </div>
          </div>
          {askMutation.isError && (
            <div
              role="alert"
              data-testid="live-games-chatbot-error"
              className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive"
            >
              {formatErrorMessage(askMutation.error)}
            </div>
          )}
          {answer && (
            <div
              data-testid="live-games-chatbot-answer"
              className="rounded-md border border-border bg-background/40 p-3 text-xs leading-relaxed text-foreground whitespace-pre-wrap"
            >
              {answer.answer}
              <div className="mt-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                {answer.provider}/{answer.model} ·{" "}
                {answer.context_summary.kind === "upcoming"
                  ? `schedule=${answer.context_summary.has_schedule ? "yes" : "no"} · player_status=${answer.context_summary.has_player_status ? "yes" : "no"} · intangibles_rows=${answer.context_summary.intangibles_rows ?? 0} · ticks_rows=${answer.context_summary.ticks_rows ?? 0} · debate=${answer.context_summary.has_swarm_debate ? "yes" : "no"}`
                  : `timeline_rows=${answer.context_summary.timeline_rows ?? 0} · ticks_rows=${answer.context_summary.ticks_rows ?? 0} · swarm=${answer.context_summary.has_swarm_debate ? "yes" : "no"}`}
                {chosenModel && answer.model !== chosenModel && (
                  <span className="ml-1 text-warning">
                    (fell back from {chosenModel})
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      </GlassCard>

      {error && (
        <GlassCard className="p-4" data-testid="live-games-error">
          <div className="text-sm text-destructive">
            Failed to load live games: {formatErrorMessage(error)}
          </div>
          <button
            type="button"
            onClick={() => refetch()}
            className="mt-2 rounded-md border border-border px-3 py-1 text-xs uppercase tracking-wider hover:bg-background/60"
          >
            Retry
          </button>
        </GlassCard>
      )}

      {isLoading && (
        <GlassCard className="p-4" data-testid="live-games-loading">
          <div className="text-xs text-muted-foreground">Loading live games…</div>
        </GlassCard>
      )}

      {!isLoading && grouped.length === 0 && (
        <GlassCard className="p-4" data-testid="live-games-empty">
          <SectionHeader
            title="No recent in-game activity"
            subtitle="The mid-game signals pipeline hasn't logged any market ticks in the last 6 hours."
          />
          <p className="mt-3 text-xs text-muted-foreground">
            When games are live, this view will populate with the most recent tick per
            market — score, period, model probability, current edge, and the latest
            swarm debate verdict. The chatbot above stays usable; pick a (past) market
            from the recent feed below or an upcoming matchup from "Today's Games" to
            ask grounded questions.
          </p>
        </GlassCard>
      )}

      {grouped.map(([league, leagueItems]) => (
        <GlassCard
          key={league}
          className="p-4"
          data-testid={`live-games-league-${league}`}
        >
          <SectionHeader
            title={league}
            subtitle={`${leagueItems.length} market${leagueItems.length === 1 ? "" : "s"} · ${leagueItems.filter((i) => i.is_live).length} live now`}
          />
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
            {leagueItems.map((it) => {
              const active = selected?.market_id === it.market_id;
              return (
                <button
                  key={it.market_id}
                  type="button"
                  onClick={() => selectLive(it)}
                  data-testid={`live-game-card-${it.market_id}`}
                  className={`flex flex-col gap-2 rounded-md border p-3 text-left transition ${
                    active
                      ? "border-accent-cyan bg-accent-cyan/10"
                      : "border-border bg-background/40 hover:border-accent-cyan/50"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      {it.is_live && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-success/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-success">
                          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
                          LIVE
                        </span>
                      )}
                      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        {it.action ?? "—"}
                      </span>
                    </div>
                    <span className="text-[10px] text-muted-foreground">
                      {fmtAge(it.last_update)}
                    </span>
                  </div>
                  <div className="text-xs font-semibold text-foreground">
                    {it.swarm?.matchup ?? it.market_id}
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-[11px]">
                    <div>
                      <div className="text-muted-foreground">Score</div>
                      <div className="font-mono text-foreground">
                        {fmtScore(it.home_score, it.away_score)}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">
                        Period · Time
                      </div>
                      <div className="font-mono text-foreground">
                        {it.period ?? "—"} · {it.time_remaining ?? "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">Model prob</div>
                      <div className="font-mono text-foreground">
                        {pct(it.updated_prob)}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">Market</div>
                      <div className="font-mono text-foreground">
                        {it.market_price !== null
                          ? pct(it.market_price)
                          : fmtCents(it.tick?.close_cents ?? null)}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">Edge</div>
                      <div
                        className={`font-mono ${(it.current_edge ?? 0) > 0 ? "text-success" : "text-destructive"}`}
                      >
                        {pct(it.current_edge)}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">Swarm</div>
                      <div className="font-mono text-foreground">
                        {it.swarm
                          ? `${it.swarm.recommended_action ?? "?"} (${pct(it.swarm.consensus_probability)})`
                          : "—"}
                      </div>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </GlassCard>
      ))}

      <header className="flex flex-col gap-2 pt-2">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              TODAY'S GAMES
            </div>
            <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
              Upcoming matchups · Pre-game intel
            </h1>
            <p className="mt-1 text-xs text-muted-foreground">
              {upcomingData?.count
                ? `${upcomingData.count} game${upcomingData.count === 1 ? "" : "s"} scheduled in the next ${upcomingData.window_hours}h · click any card to chat with injuries, intangibles, and recent line ticks`
                : "No games scheduled in the next 24h."}
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span data-testid="upcoming-games-fetched-at">
              Updated {fmtAge(upcomingData?.fetched_at ?? null)}
            </span>
          </div>
        </div>
      </header>

      {upcomingError && (
        <GlassCard className="p-4" data-testid="upcoming-games-error">
          <div className="text-sm text-destructive">
            Failed to load upcoming games: {formatErrorMessage(upcomingError)}
          </div>
        </GlassCard>
      )}

      {upcomingLoading && !upcomingData && (
        <GlassCard className="p-4" data-testid="upcoming-games-loading">
          <div className="text-xs text-muted-foreground">
            Loading upcoming games…
          </div>
        </GlassCard>
      )}

      {!upcomingLoading && upcomingGrouped.length === 0 && !upcomingError && (
        <GlassCard className="p-4" data-testid="upcoming-games-empty">
          <SectionHeader
            title="No upcoming games in the next 24h"
            subtitle="The schedule cache hasn't surfaced any matchups in the lookahead window."
          />
        </GlassCard>
      )}

      {upcomingGrouped.map(([league, leagueItems]) => (
        <GlassCard
          key={`upcoming-${league}`}
          className="p-4"
          data-testid={`upcoming-games-league-${league}`}
        >
          <SectionHeader
            title={`${league} · upcoming`}
            subtitle={`${leagueItems.length} game${leagueItems.length === 1 ? "" : "s"} in the next ${upcomingData?.window_hours ?? 24}h`}
          />
          <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
            {leagueItems.map((it) => {
              const active = selected?.market_id === it.market_id;
              return (
                <button
                  key={it.market_id}
                  type="button"
                  onClick={() => selectUpcoming(it)}
                  data-testid={`upcoming-game-card-${it.market_id}`}
                  className={`flex flex-col gap-2 rounded-md border p-3 text-left transition ${
                    active
                      ? "border-accent-cyan bg-accent-cyan/10"
                      : "border-border bg-background/40 hover:border-accent-cyan/50"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="inline-flex items-center gap-1 rounded-full bg-accent-cyan/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-accent-cyan">
                      UPCOMING
                    </span>
                    <span className="text-[10px] text-muted-foreground">
                      {fmtCountdown(it.scheduled)}
                    </span>
                  </div>
                  <div className="text-xs font-semibold text-foreground">
                    {it.away_team} @ {it.home_team}
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-[11px]">
                    <div>
                      <div className="text-muted-foreground">Scheduled</div>
                      <div className="font-mono text-foreground">
                        {it.scheduled
                          ? new Date(it.scheduled).toLocaleString(undefined, {
                              hour: "2-digit",
                              minute: "2-digit",
                              month: "short",
                              day: "numeric",
                            })
                          : "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">Status</div>
                      <div className="font-mono text-foreground">
                        {it.status || "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">Venue</div>
                      <div
                        className="font-mono text-foreground truncate"
                        title={it.venue ?? ""}
                      >
                        {it.venue || "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground">Intangibles</div>
                      <div
                        className={`font-mono ${it.intangibles_count > 0 ? "text-accent-cyan" : "text-foreground"}`}
                      >
                        {it.intangibles_count > 0
                          ? `${it.intangibles_count} report${it.intangibles_count === 1 ? "" : "s"}`
                          : "none yet"}
                      </div>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </GlassCard>
      ))}
    </div>
  );
}
