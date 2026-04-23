import { useCallback } from "react";
import { Link } from "wouter";
import { useQueryClient } from "@tanstack/react-query";
import { useGetRecentDebates, getGetRecentDebatesQueryKey } from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";

function fmtPct(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtMoney(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `$${n.toFixed(3)}`;
}

function fmtLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function SwarmDebates() {
  const params = { limit: 10 };
  const queryClient = useQueryClient();
  const { data, isLoading } = useGetRecentDebates(params, {
    query: {
      refetchInterval: 60_000,
      queryKey: getGetRecentDebatesQueryKey(params),
    },
  });

  const onLlmEvent = useCallback(
    (ev: Record<string, unknown>) => {
      const t = typeof ev.type === "string" ? ev.type : null;
      // Refresh on debate completion / gatekeeper verdict events.
      if (t === "debate_complete" || t === "gatekeeper_verdict" || t === "moderator_consensus") {
        queryClient.invalidateQueries({ queryKey: getGetRecentDebatesQueryKey(params) });
      }
    },
    [queryClient],
  );
  const llm = useWebSocket({ path: "/api/ws/llm-logs", onMessage: onLlmEvent, bufferSize: 50 });

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-72 bg-card" />;
  }

  const items = data?.items ?? [];
  const live = llm.status === "open";

  return (
    <div className="border border-border bg-card flex flex-col">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <div className="flex items-center gap-2">
          <span
            className={`w-1.5 h-1.5 rounded-full ${live ? "bg-success animate-pulse" : "bg-muted-foreground"}`}
          />
          <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
            SWARM_DEBATES
          </h2>
        </div>
        <span className="text-xs text-muted-foreground">CONSENSUS_VERDICTS</span>
      </div>
      <div className="overflow-auto divide-y divide-border/50">
        {items.length === 0 ? (
          <div className="p-4 text-center text-muted-foreground text-xs">NO_DEBATES_FOUND</div>
        ) : (
          items.map((d) => {
            const verdict = (d.recommended_action ?? "").toUpperCase();
            const verdictClass =
              verdict === "PASS"
                ? "text-muted-foreground border-muted-foreground/40"
                : verdict.includes("BUY") || verdict.includes("YES")
                  ? "text-win border-win/50"
                  : verdict.includes("SELL") || verdict.includes("NO")
                    ? "text-loss border-loss/50"
                    : "text-primary border-primary/50";
            return (
              <div key={d.id} className="p-3 hover:bg-muted/20 transition-colors text-xs">
                <div className="flex items-start justify-between gap-2 mb-2">
                  <div className="min-w-0 flex-1">
                    <div className="text-foreground truncate" title={d.matchup ?? ""}>
                      {d.matchup ?? "—"}
                    </div>
                    <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
                      {d.league ?? "—"} · {d.market_type ?? "—"} · {fmtTime(d.created_at)}
                    </div>
                  </div>
                  <div
                    className={`text-[10px] uppercase border px-2 py-0.5 ${verdictClass} ${d.approved ? "" : "opacity-70"}`}
                  >
                    {verdict || "—"}
                  </div>
                </div>
                {d.redacted ? (
                  <div
                    className="mb-2 border border-primary/40 bg-primary/5 px-2.5 py-2 flex items-center justify-between gap-2"
                    data-testid={`upsell-redacted-${d.id}`}
                  >
                    <div className="min-w-0">
                      <div className="text-[10px] uppercase tracking-wider text-primary font-bold">
                        PRO_ONLY
                      </div>
                      <div className="text-[11px] text-foreground/80 truncate">
                        Upgrade to Pro to see persona reasoning
                      </div>
                    </div>
                    <Link
                      href="/pricing"
                      className="shrink-0 text-[10px] uppercase tracking-widest font-semibold border border-primary/60 text-primary px-2 py-1 hover:bg-primary hover:text-primary-foreground transition-colors"
                      data-testid={`link-upgrade-${d.id}`}
                    >
                      UPGRADE →
                    </Link>
                  </div>
                ) : null}
                <div className="grid grid-cols-4 gap-2 text-[10px]">
                  <div>
                    <div className="text-muted-foreground">CONSENSUS</div>
                    <div className="text-primary font-bold">{fmtPct(d.consensus_probability, 1)}</div>
                  </div>
                  <div>
                    <div className="text-muted-foreground">BAND</div>
                    <div>
                      {fmtPct(d.confidence_band_low)}–{fmtPct(d.confidence_band_high)}
                    </div>
                  </div>
                  <div>
                    <div className="text-muted-foreground">AGREE</div>
                    <div>{fmtPct(d.agreement_rate)}</div>
                  </div>
                  <div className="text-right">
                    <div className="text-muted-foreground">COST · LAT</div>
                    <div>
                      {fmtMoney(d.cost_estimate)} · {fmtLatency(d.latency_ms)}
                    </div>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
