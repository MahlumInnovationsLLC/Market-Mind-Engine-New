import { useMemo, useState } from "react";
import {
  getGetSwarmPersonasTodayQueryKey,
  useGetSwarmPersonasToday,
} from "@workspace/api-client-react";

type SortKey =
  | "persona_id"
  | "debates"
  | "brier"
  | "accuracy_pct"
  | "avg_confidence"
  | "leans";

const LOOKBACK_DAYS = 14;

function fmtNum(n: number | null | undefined, digits = 3): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

function todayUtcIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function isoMinusDaysUtc(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export default function SwarmPersonasToday() {
  const today = todayUtcIso();
  const earliest = isoMinusDaysUtc(LOOKBACK_DAYS);
  const [selectedDate, setSelectedDate] = useState<string>(today);

  const isToday = selectedDate === today;
  const params = isToday ? undefined : { date_utc: selectedDate };
  const refetchInterval: number | false = isToday ? 60_000 : false;
  const { data, isLoading } = useGetSwarmPersonasToday(params, {
    query: {
      queryKey: getGetSwarmPersonasTodayQueryKey(params),
      refetchInterval,
    },
  });

  const [sortKey, setSortKey] = useState<SortKey>("brier");
  const [sortDesc, setSortDesc] = useState(false);

  const items = data?.items ?? [];
  const dateUtc = data?.date_utc ?? selectedDate;

  const sorted = useMemo(() => {
    const rows = [...items];
    rows.sort((a, b) => {
      const dir = sortDesc ? -1 : 1;
      const get = (r: typeof a): number | string => {
        switch (sortKey) {
          case "persona_id":
            return r.persona_id ?? "";
          case "debates":
            return r.debates ?? 0;
          case "brier":
            return r.brier ?? 0;
          case "accuracy_pct":
            return r.accuracy_pct ?? 0;
          case "avg_confidence":
            return r.avg_confidence ?? -1;
          case "leans":
            return (r.lean_yes ?? 0) - (r.lean_no ?? 0);
        }
      };
      const va = get(a);
      const vb = get(b);
      if (typeof va === "string" && typeof vb === "string") {
        return va.localeCompare(vb) * dir;
      }
      return ((va as number) - (vb as number)) * dir;
    });
    return rows;
  }, [items, sortKey, sortDesc]);

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDesc((d) => !d);
    } else {
      setSortKey(key);
      // Default to asc for brier (lower = better), desc for everything else.
      setSortDesc(key !== "brier" && key !== "persona_id");
    }
  }

  function arrow(key: SortKey): string {
    if (key !== sortKey) return "";
    return sortDesc ? " ▼" : " ▲";
  }

  return (
    <div className="border border-border bg-card flex flex-col h-[420px]">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0 gap-2">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          PERSONAS_TODAY {dateUtc ? `· ${dateUtc}_UTC` : ""}
        </h2>
        <div className="flex items-center gap-2">
          <input
            type="date"
            value={selectedDate}
            min={earliest}
            max={today}
            onChange={(e) => {
              const v = e.target.value;
              if (!v) return;
              if (v < earliest || v > today) return;
              setSelectedDate(v);
            }}
            className="bg-background border border-border text-xs px-2 py-1 text-foreground font-mono"
            title={`Bounded to ${earliest} … ${today} (UTC)`}
            aria-label="Select UTC date"
          />
          {!isToday ? (
            <button
              type="button"
              onClick={() => setSelectedDate(today)}
              className="text-[10px] uppercase tracking-wider px-2 py-1 border border-border hover:bg-muted/40 text-muted-foreground hover:text-foreground"
            >
              TODAY
            </button>
          ) : null}
          <span className="text-xs text-muted-foreground">
            {sorted.length}_PERSONAS
          </span>
        </div>
      </div>
      <div className="overflow-auto grow">
        {isLoading ? (
          <div className="p-4 animate-pulse h-full bg-card" />
        ) : (
          <table className="w-full text-xs text-left">
            <thead className="sticky top-0 bg-card border-b border-border text-muted-foreground uppercase text-[10px]">
              <tr>
                <th
                  className="p-2 font-normal cursor-pointer select-none hover:text-foreground"
                  onClick={() => toggleSort("persona_id")}
                >
                  PERSONA{arrow("persona_id")}
                </th>
                <th
                  className="p-2 font-normal text-right cursor-pointer select-none hover:text-foreground"
                  onClick={() => toggleSort("debates")}
                >
                  N{arrow("debates")}
                </th>
                <th
                  className="p-2 font-normal text-right cursor-pointer select-none hover:text-foreground"
                  onClick={() => toggleSort("brier")}
                >
                  BRIER{arrow("brier")}
                </th>
                <th
                  className="p-2 font-normal text-right cursor-pointer select-none hover:text-foreground"
                  onClick={() => toggleSort("accuracy_pct")}
                >
                  ACC{arrow("accuracy_pct")}
                </th>
                <th
                  className="p-2 font-normal text-right cursor-pointer select-none hover:text-foreground"
                  onClick={() => toggleSort("avg_confidence")}
                >
                  CONF{arrow("avg_confidence")}
                </th>
                <th
                  className="p-2 font-normal text-right cursor-pointer select-none hover:text-foreground"
                  onClick={() => toggleSort("leans")}
                  title="YES / NO / PASS"
                >
                  LEANS{arrow("leans")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/50">
              {sorted.length === 0 ? (
                <tr>
                  <td colSpan={6} className="p-4 text-center text-muted-foreground">
                    {isToday
                      ? "NO_SETTLED_DEBATES_TODAY"
                      : `NO_SETTLED_DEBATES_ON_${dateUtc}`}
                  </td>
                </tr>
              ) : (
                sorted.map((p, i) => {
                  const brier = typeof p.brier === "number" ? p.brier : null;
                  const brierClass =
                    brier === null
                      ? "text-muted-foreground"
                      : brier < 0.2
                        ? "text-win"
                        : brier > 0.25
                          ? "text-loss"
                          : "text-foreground";
                  return (
                    <tr key={i} className="hover:bg-muted/30 transition-colors">
                      <td
                        className="p-2 text-primary uppercase truncate max-w-[160px]"
                        title={p.persona_id}
                      >
                        {p.persona_id}
                      </td>
                      <td className="p-2 text-right">{p.debates}</td>
                      <td className={`p-2 text-right font-bold ${brierClass}`}>
                        {fmtNum(brier, 3)}
                      </td>
                      <td className="p-2 text-right">{fmtPct(p.accuracy_pct, 1)}</td>
                      <td className="p-2 text-right text-muted-foreground">
                        {fmtNum(p.avg_confidence ?? null, 2)}
                      </td>
                      <td className="p-2 text-right text-muted-foreground">
                        {p.lean_yes}/{p.lean_no}/{p.lean_pass}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
