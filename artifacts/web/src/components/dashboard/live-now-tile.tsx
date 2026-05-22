import { useLocation } from "wouter";
import {
  getGetDashboardLiveNowQueryKey,
  useGetDashboardLiveNow,
} from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

const SPORT_ORDER = [
  "nba",
  "nhl",
  "mlb",
  "nfl",
  "ncaab",
  "ncaaf",
  "soccer",
] as const;

function fmtSecs(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  if (v < 60) return `${v.toFixed(0)}s`;
  if (v < 3600) return `${(v / 60).toFixed(1)}m`;
  return `${(v / 3600).toFixed(1)}h`;
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleTimeString();
  } catch {
    return s;
  }
}

/** Classify staleness vs the per-sport budget — green / yellow / red.
 *  Returns neutral if the sport has no live games (nothing to grade). */
function toneFor(
  liveGames: number,
  staleness: number | null | undefined,
  budget: number | null | undefined,
): "neutral" | "ok" | "warn" | "bad" {
  if (liveGames <= 0) return "neutral";
  if (staleness === null || staleness === undefined) return "bad";
  if (!budget || budget <= 0) return "neutral";
  if (staleness <= budget) return "ok";
  if (staleness <= 2 * budget) return "warn";
  return "bad";
}

const TONE_CLASS: Record<
  "neutral" | "ok" | "warn" | "bad",
  { row: string; pill: string; label: string }
> = {
  neutral: {
    row: "border-border",
    pill: "border-border text-muted-foreground",
    label: "idle",
  },
  ok: {
    row: "border-border",
    pill: "border-success/40 bg-success/10 text-success",
    label: "fresh",
  },
  warn: {
    row: "border-warning/40 bg-warning/5",
    pill: "border-warning/40 bg-warning/10 text-warning",
    label: "lagging",
  },
  bad: {
    row: "border-destructive/40 bg-destructive/5",
    pill: "border-destructive/40 bg-destructive/10 text-destructive",
    label: "stale",
  },
};

export default function LiveNowTile() {
  const [, setLocation] = useLocation();
  const queryKey = getGetDashboardLiveNowQueryKey();
  const { data, isLoading, error } = useGetDashboardLiveNow({
    query: { queryKey, refetchInterval: 10_000, staleTime: 8_000 },
  });

  const per = data?.per_sport ?? {};
  const sports = [
    ...SPORT_ORDER.filter((s) => s in per),
    ...Object.keys(per).filter(
      (s) => !(SPORT_ORDER as readonly string[]).includes(s),
    ),
  ];

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-live-now">
      <SectionHeader
        eyebrow="LIVE"
        title="Live games now"
        subtitle={
          data
            ? `Per-sport in-game loop snapshot · ${data.totals?.live_games ?? 0} live · ${data.totals?.is_live_predictions ?? 0} is_live predictions · refreshed ${fmtTime(data.fetched_at)}`
            : "Per-sport in-game loop snapshot — counts and feed staleness vs each sport's freshness budget."
        }
      />
      {error ? (
        <div
          role="alert"
          data-testid="live-now-error"
          className="mt-3 text-xs text-destructive"
        >
          Failed to load Live games now.
        </div>
      ) : null}
      {isLoading && !data ? (
        <div className="mt-3 h-24 w-full animate-pulse rounded bg-muted/50" />
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-border text-[10px] uppercase tracking-wider text-muted-foreground">
                <th className="py-1 pr-3">Sport</th>
                <th className="py-1 pr-3 text-right">Live games</th>
                <th className="py-1 pr-3 text-right">is_live preds</th>
                <th className="py-1 pr-3 text-right">Oldest age</th>
                <th className="py-1 pr-3 text-right">Budget</th>
                <th className="py-1 pr-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {sports.map((sport) => {
                const e = per[sport];
                if (!e) return null;
                const tone = toneFor(
                  e.live_games,
                  e.oldest_staleness_seconds ?? null,
                  e.staleness_budget_seconds ?? null,
                );
                const cls = TONE_CLASS[tone];
                const go = () =>
                  setLocation(
                    `/live-games?league=${encodeURIComponent(sport)}`,
                  );
                return (
                  <tr
                    key={sport}
                    data-testid={`live-now-row-${sport}`}
                    data-tone={tone}
                    role="link"
                    tabIndex={0}
                    onClick={go}
                    onKeyDown={(ev) => {
                      if (ev.key === "Enter" || ev.key === " ") {
                        ev.preventDefault();
                        go();
                      }
                    }}
                    title={`Show live ${sport.toUpperCase()} games`}
                    className={`cursor-pointer border-b transition-colors hover:bg-accent-cyan/5 focus:bg-accent-cyan/10 focus:outline-none ${cls.row}`}
                  >
                    <td className="py-1.5 pr-3 font-semibold uppercase text-accent-cyan underline-offset-2 hover:underline">
                      {sport}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono tabular-nums">
                      {e.live_games}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono tabular-nums">
                      {e.is_live_predictions}
                    </td>
                    <td
                      data-testid={`live-now-staleness-${sport}`}
                      className="py-1.5 pr-3 text-right font-mono tabular-nums"
                    >
                      {fmtSecs(e.oldest_staleness_seconds ?? null)}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono tabular-nums text-muted-foreground">
                      {fmtSecs(e.staleness_budget_seconds ?? null)}
                    </td>
                    <td className="py-1.5 pr-3">
                      <span
                        data-testid={`live-now-tone-${sport}`}
                        className={`rounded border px-2 py-0.5 text-[10px] uppercase tracking-wider ${cls.pill}`}
                      >
                        {cls.label}
                      </span>
                      {e.error ? (
                        <span
                          className="ml-2 text-[10px] text-destructive"
                          title={e.error}
                        >
                          err
                        </span>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
              {sports.length === 0 ? (
                <tr>
                  <td
                    colSpan={6}
                    className="py-3 text-center text-muted-foreground"
                  >
                    No sports configured for the in-game loop.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      )}
    </GlassCard>
  );
}
