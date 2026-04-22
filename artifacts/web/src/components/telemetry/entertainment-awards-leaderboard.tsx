import { useMemo, useState } from "react";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";

type AwardsItem = {
  source: string;
  signal_id: string;
  signal_type: string;
  title: string;
  event_at: string | null;
  value: number | null;
};

type AwardsResponse = {
  award_type: string;
  items: AwardsItem[];
};

const AWARD_TYPES = ["OSCAR", "EMMY", "GRAMMY"] as const;
type AwardType = (typeof AWARD_TYPES)[number];

function fmtPct(n: number | null): string {
  if (n === null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

export default function EntertainmentAwardsLeaderboardPanel() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const [awardType, setAwardType] = useState<AwardType>("OSCAR");

  const { data, isLoading, error } = useQuery<AwardsResponse>({
    queryKey: ["entertainment", "awards-leaderboard", awardType],
    queryFn: () =>
      apiFetch<AwardsResponse>(
        `/api/entertainment/awards-leaderboard?award_type=${awardType}&limit=15`,
        { getToken: token },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 300_000,
    staleTime: 120_000,
  });

  const items = data?.items ?? [];

  return (
    <section className="border border-border rounded p-3 bg-card">
      <header className="flex items-baseline justify-between mb-2">
        <h2 className="text-sm font-bold uppercase tracking-widest text-primary">
          Awards Race Leaderboard
        </h2>
        <div className="flex items-center gap-1">
          {AWARD_TYPES.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setAwardType(t)}
              className={
                "border px-2 py-0.5 text-[10px] uppercase tracking-widest " +
                (awardType === t
                  ? "border-primary text-primary"
                  : "border-border text-muted-foreground hover:text-primary")
              }
              data-testid={`button-award-${t.toLowerCase()}`}
            >
              {t}
            </button>
          ))}
        </div>
      </header>
      {isLoading && (
        <p className="text-xs text-muted-foreground">Loading…</p>
      )}
      {error && (
        <p className="text-xs text-loss">Failed to load awards leaderboard.</p>
      )}
      {!isLoading && !error && items.length === 0 && (
        <p className="text-xs text-muted-foreground">
          No leading nominees recorded for the next {awardType} cycle yet.
        </p>
      )}
      {items.length > 0 && (
        <ul className="text-xs font-mono divide-y divide-border">
          <li className="grid grid-cols-[2rem_1fr_5rem] gap-2 py-1 text-[10px] uppercase text-muted-foreground">
            <span>#</span>
            <span>Candidate</span>
            <span className="text-right">Implied</span>
          </li>
          {items.map((it, i) => (
            <li
              key={`${it.source}/${it.signal_id}`}
              className="grid grid-cols-[2rem_1fr_5rem] gap-2 py-1 items-center"
            >
              <span className="text-muted-foreground">{i + 1}</span>
              <span className="text-foreground truncate" title={it.title}>
                {it.title}
              </span>
              <span className="text-right text-primary">{fmtPct(it.value)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
