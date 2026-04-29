import { useMemo } from "react";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";

type PersonaWeight = {
  persona: string;
  league: string;
  market_type: string;
  weight: number;
};

type LlmModeState = {
  mode: string;
  ollama_model_fast: string;
  ollama_model_reasoning: string;
  ollama_model_backup: string;
};

type RuntimeSettings = {
  aggression_mode: string;
  thresholds: {
    min_edge_pp: number;
    kelly_fraction_cap: number;
    min_agreement_rate: number;
    max_confidence_band: number;
  };
  available_modes: string[];
  updated_at: string | null;
  updated_by: string | null;
  persona_weights: PersonaWeight[];
  llm_mode?: LlmModeState;
};

const LLM_MODE_COLOR: Record<string, string> = {
  cloud: "text-foreground",
  local: "text-info",
  hybrid: "text-warning",
};

const MODE_COLOR: Record<string, string> = {
  conservative: "text-info",
  balanced: "text-foreground",
  aggressive: "text-loss",
};

function fmtPct(n: number, digits = 0): string {
  return `${(n * 100).toFixed(digits)}%`;
}

export default function RuntimeSettingsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const { data, isLoading } = useQuery<RuntimeSettings>({
    queryKey: ["dashboard", "runtime-settings"],
    queryFn: () =>
      apiFetch<RuntimeSettings>("/api/dashboard/runtime-settings", {
        getToken: token,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  if (isLoading || !data) {
    return (
      <div
        className="border border-border bg-card h-72 animate-pulse"
        data-testid="panel-runtime-settings"
      />
    );
  }

  const mode = data.aggression_mode;
  const modeClass = MODE_COLOR[mode] ?? "text-foreground";

  // Group weights by league for compact rendering.
  const grouped = new Map<string, PersonaWeight[]>();
  for (const w of data.persona_weights) {
    const arr = grouped.get(w.league) ?? [];
    arr.push(w);
    grouped.set(w.league, arr);
  }
  const leagues = Array.from(grouped.keys()).sort();

  return (
    <div
      className="border border-border bg-card flex flex-col"
      data-testid="panel-runtime-settings"
    >
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          AGGRESSION_MODE & PERSONA_WEIGHTS
        </h2>
        <span
          className={`text-xs uppercase font-bold ${modeClass}`}
          data-testid="text-aggression-mode"
        >
          {mode}
        </span>
      </div>
      {data.llm_mode ? (
        <div
          className="border-b border-border px-3 py-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] uppercase tracking-widest"
          data-testid="row-llm-mode"
        >
          <span className="text-muted-foreground">LLM_MODE</span>
          <span
            className={`font-bold ${LLM_MODE_COLOR[data.llm_mode.mode] ?? "text-foreground"}`}
            data-testid="text-llm-mode"
          >
            {data.llm_mode.mode}
          </span>
          <span className="text-muted-foreground">
            FAST <span className="text-foreground normal-case">{data.llm_mode.ollama_model_fast}</span>
          </span>
          <span className="text-muted-foreground">
            REASON <span className="text-foreground normal-case">{data.llm_mode.ollama_model_reasoning}</span>
          </span>
          <span className="text-muted-foreground">
            BACKUP <span className="text-foreground normal-case">{data.llm_mode.ollama_model_backup}</span>
          </span>
        </div>
      ) : null}
      <div className="p-3 grid grid-cols-2 md:grid-cols-4 gap-3 text-xs border-b border-border">
        <Stat label="MIN_EDGE" value={`${data.thresholds.min_edge_pp.toFixed(1)}pp`} />
        <Stat
          label="KELLY_CAP"
          value={fmtPct(data.thresholds.kelly_fraction_cap, 0)}
        />
        <Stat
          label="MIN_AGREE"
          value={fmtPct(data.thresholds.min_agreement_rate, 0)}
        />
        <Stat
          label="MAX_BAND"
          value={fmtPct(data.thresholds.max_confidence_band, 0)}
        />
      </div>
      <div
        className="overflow-auto max-h-72"
        data-testid="list-persona-weights"
      >
        {leagues.length === 0 ? (
          <div className="p-4 text-center text-muted-foreground text-xs">
            NO_PER_LEAGUE_WEIGHTS_YET
          </div>
        ) : (
          <table className="w-full text-xs text-left">
            <thead className="sticky top-0 bg-card border-b border-border text-muted-foreground uppercase text-[10px]">
              <tr>
                <th className="p-2 font-normal">LEAGUE</th>
                <th className="p-2 font-normal">PERSONA</th>
                <th className="p-2 font-normal">MARKET</th>
                <th className="p-2 font-normal text-right">WEIGHT</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/50">
              {leagues.map((league) =>
                grouped
                  .get(league)!
                  .sort((a, b) => b.weight - a.weight)
                  .map((w) => (
                    <tr
                      key={`${w.league}-${w.persona}-${w.market_type}`}
                      className="hover:bg-muted/30"
                      data-testid={`row-persona-weight-${w.league}-${w.persona}`}
                    >
                      <td className="p-2 text-muted-foreground uppercase">{w.league}</td>
                      <td className="p-2 text-primary uppercase">{w.persona}</td>
                      <td className="p-2 text-muted-foreground">{w.market_type}</td>
                      <td className="p-2 text-right font-bold">
                        {w.weight.toFixed(3)}
                      </td>
                    </tr>
                  )),
              )}
            </tbody>
          </table>
        )}
      </div>
      {data.updated_at ? (
        <div className="border-t border-border px-3 py-1.5 text-[10px] uppercase tracking-widest text-muted-foreground flex justify-between">
          <span>updated {data.updated_at}</span>
          {data.updated_by ? <span>by {data.updated_by}</span> : null}
        </div>
      ) : null}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
      <div className="text-sm font-bold text-foreground">{value}</div>
    </div>
  );
}
