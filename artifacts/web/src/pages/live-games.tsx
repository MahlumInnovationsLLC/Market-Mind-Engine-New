import { useCallback, useMemo, useState } from "react";
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

type AskResponse = {
  market_id: string;
  answer: string;
  model: string;
  context_summary: {
    has_data: boolean;
    timeline_rows: number;
    ticks_rows: number;
    has_swarm_debate: boolean;
  };
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

export default function LiveGamesPage() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const { data, isLoading, error, refetch, isFetching } = useQuery<LiveGamesResponse>({
    queryKey: ["live-games", "list"],
    queryFn: () =>
      apiFetch<LiveGamesResponse>("/api/live-games?limit=50", {
        getToken: tokenFn,
      }),
    refetchInterval: 10_000,
    enabled: !!isSignedIn,
  });

  const [selected, setSelected] = useState<LiveGameItem | null>(null);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<AskResponse | null>(null);

  const askMutation = useMutation({
    mutationFn: async (vars: { market_id: string; question: string }) =>
      apiFetch<AskResponse>(
        `/api/live-games/${encodeURIComponent(vars.market_id)}/ask`,
        {
          method: "POST",
          body: { question: vars.question },
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
    });
  }, [selected, question, askMutation]);

  const items = data?.items ?? [];
  const liveNowCount = data?.is_live_now_count ?? 0;

  const grouped = useMemo(() => {
    const m = new Map<string, LiveGameItem[]>();
    for (const it of items) {
      const arr = m.get(it.league) ?? [];
      arr.push(it);
      m.set(it.league, arr);
    }
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [items]);

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
              ? `Grounded in live data for ${selected.swarm?.matchup ?? selected.market_id}`
              : "Click a market card below to ground the chatbot in that game's live data"
          }
        />
        <div className="mt-3 flex flex-col gap-2">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={
              selected
                ? "e.g. What's the model's edge right now? Is the swarm approving the trade?"
                : "Select a game first, then ask about the spread, who wins, or in-game bet sizing."
            }
            disabled={!selected || askMutation.isPending}
            rows={3}
            data-testid="live-games-chatbot-input"
            className="rounded-md border border-border bg-background/60 p-2 text-xs text-foreground placeholder:text-muted-foreground focus:border-accent-cyan focus:outline-none disabled:opacity-50"
          />
          <div className="flex items-center justify-between gap-2">
            <div className="text-[11px] text-muted-foreground">
              {selected
                ? `Market: ${selected.market_id}`
                : "No market selected"}
            </div>
            <button
              type="button"
              onClick={onAsk}
              disabled={!selected || !question.trim() || askMutation.isPending}
              data-testid="live-games-chatbot-ask"
              className="rounded-md border border-accent-cyan/40 bg-accent-cyan/10 px-3 py-1 text-xs font-semibold uppercase tracking-wider text-accent-cyan hover:bg-accent-cyan/20 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {askMutation.isPending ? "Thinking…" : "Ask"}
            </button>
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
                model={answer.model} · timeline_rows={answer.context_summary.timeline_rows} ·
                ticks_rows={answer.context_summary.ticks_rows} ·
                swarm={answer.context_summary.has_swarm_debate ? "yes" : "no"}
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
            swarm debate verdict. The chatbot above stays usable; just pick a (past)
            market from the recent feed below to ask questions against its history.
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
                  onClick={() => setSelected(it)}
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
    </div>
  );
}
