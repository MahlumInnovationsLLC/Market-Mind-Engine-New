import { useCallback, useState } from "react";
import { Link } from "wouter";
import { useQueryClient } from "@tanstack/react-query";
import {
  useGetRecentDebates,
  getGetRecentDebatesQueryKey,
  useGetDebateEntrantMarkets,
  getGetDebateEntrantMarketsQueryKey,
  type DebateEntrantMarkets,
  type EntrantOutrightMarket,
} from "@workspace/api-client-react";
import { useWebSocket } from "@/hooks/use-websocket";
import DelayedSignalsBanner from "@/components/delayed-signals-banner";
import {
  PlackettLuceTable,
  extractPlackettLuceSketch,
} from "@/components/telemetry/plackett-luce-table";

// ---------------------------------------------------------------------------
// Per-debate transcript narrowing helpers
// ---------------------------------------------------------------------------
//
// `analyst_verdicts` and `moderator_synthesis` come back as raw jsonb on the
// generated client (the OpenAPI spec types them as `{[k: string]: unknown}`).
// We do our own narrowing here so the click-to-expand transcript view stays
// type-safe without forcing a shared schema change — the mobile detail screen
// (`/research/[id]`) follows the same pattern.

type AnalystVerdictView = {
  persona: string | null;
  provider: string | null;
  model: string | null;
  primaryProvider: string | null;
  primaryModel: string | null;
  fallbackUsed: boolean;
  fallbackReason: string | null;
  probability: number | null;
  confidence: number | null;
  lean: string | null;
  weight: number | null;
  reasoning: string | null;
  latencyMs: number | null;
  error: string | null;
};

type GatekeeperView = {
  model: string | null;
  approved: boolean | null;
  recommendedAction: string | null;
  vetoReason: string | null;
};

type ModeratorSynthesisView = {
  synthesis: string | null;
  model: string | null;
  recommendedAction: string | null;
  gatekeeper: GatekeeperView | null;
};

function asString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function asNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function narrowAnalystVerdicts(raw: unknown): AnalystVerdictView[] {
  if (!Array.isArray(raw)) return [];
  const out: AnalystVerdictView[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const v = entry as Record<string, unknown>;
    out.push({
      persona: asString(v.persona),
      provider: asString(v.provider),
      model: asString(v.model),
      primaryProvider: asString(v.primary_provider),
      primaryModel: asString(v.primary_model),
      fallbackUsed: v.fallback_used === true,
      fallbackReason: asString(v.fallback_reason),
      probability: asNumber(v.probability),
      confidence: asNumber(v.confidence),
      lean: asString(v.lean),
      weight: asNumber(v.weight),
      reasoning: asString(v.reasoning),
      latencyMs: asNumber(v.latency_ms),
      error: asString(v.error),
    });
  }
  return out;
}

function narrowModeratorSynthesis(raw: unknown): ModeratorSynthesisView | null {
  if (!raw || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  let gatekeeper: GatekeeperView | null = null;
  const gkRaw = v.gatekeeper;
  if (gkRaw && typeof gkRaw === "object") {
    const gk = gkRaw as Record<string, unknown>;
    gatekeeper = {
      model: asString(gk.model),
      approved: typeof gk.approved === "boolean" ? gk.approved : null,
      recommendedAction: asString(gk.recommended_action),
      vetoReason: asString(gk.veto_reason),
    };
  }
  return {
    synthesis: asString(v.synthesis),
    model: asString(v.model),
    recommendedAction: asString(v.recommended_action),
    gatekeeper,
  };
}

function leanClass(lean: string | null): string {
  if (!lean) return "text-muted-foreground border-muted-foreground/40";
  const up = lean.toUpperCase();
  if (up === "YES" || up.includes("BUY")) return "text-win border-win/50";
  if (up === "NO" || up.includes("SELL")) return "text-loss border-loss/50";
  return "text-primary border-primary/50";
}

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

type FallbackDetail = {
  persona: string;
  fromProvider: string | null;
  fromModel: string | null;
  toProvider: string | null;
  toModel: string | null;
  reason: string | null;
};

function extractFallbacks(
  verdicts: unknown,
): FallbackDetail[] {
  if (!Array.isArray(verdicts)) return [];
  const out: FallbackDetail[] = [];
  for (const raw of verdicts) {
    if (!raw || typeof raw !== "object") continue;
    const v = raw as Record<string, unknown>;
    if (!v.fallback_used) continue;
    out.push({
      persona: typeof v.persona === "string" ? v.persona : "?",
      fromProvider: typeof v.primary_provider === "string" ? v.primary_provider : null,
      fromModel: typeof v.primary_model === "string" ? v.primary_model : null,
      toProvider: typeof v.provider === "string" ? v.provider : null,
      toModel: typeof v.model === "string" ? v.model : null,
      reason: typeof v.fallback_reason === "string" ? v.fallback_reason : null,
    });
  }
  return out;
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
  // Per-row expanded state for the click-to-reveal transcript.  Single
  // accordion behaviour (only one row open at a time) keeps the panel
  // scannable when several debates are in flight at once.
  const [expandedId, setExpandedId] = useState<number | null>(null);

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
      {data?.delayed ? (
        <DelayedSignalsBanner
          delayMinutes={data.delay_minutes}
          testId="banner-recent-debates-delayed"
        />
      ) : null}
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
            const fallbacks = extractFallbacks(d.analyst_verdicts);
            const sketch = extractPlackettLuceSketch(d.game_context);
            const fallbackTitle = fallbacks.length
              ? fallbacks
                  .map(
                    (f) =>
                      `${f.persona}: ${f.fromProvider ?? "?"}/${f.fromModel ?? "?"} → ${f.toProvider ?? "?"}/${f.toModel ?? "?"}${f.reason ? ` (${f.reason})` : ""}`,
                  )
                  .join("\n")
              : "";
            const verdict = (d.recommended_action ?? "").toUpperCase();
            const verdictClass =
              verdict === "PASS"
                ? "text-muted-foreground border-muted-foreground/40"
                : verdict.includes("BUY") || verdict.includes("YES")
                  ? "text-win border-win/50"
                  : verdict.includes("SELL") || verdict.includes("NO")
                    ? "text-loss border-loss/50"
                    : "text-primary border-primary/50";
            // Redacted rows can't expand — there's nothing to reveal
            // server-side, only the upsell card stays visible.
            const isExpandable = !d.redacted;
            const isExpanded = isExpandable && expandedId === d.id;
            const verdicts = isExpanded
              ? narrowAnalystVerdicts(d.analyst_verdicts)
              : [];
            const synthesis = isExpanded
              ? narrowModeratorSynthesis(d.moderator_synthesis)
              : null;
            return (
              <div
                key={d.id}
                data-focus-id={`debate-${d.id}`}
                className="p-3 hover:bg-muted/20 transition-colors text-xs scroll-mt-24 data-[focus-flash=true]:ring-2 data-[focus-flash=true]:ring-accent-cyan data-[focus-flash=true]:bg-accent-cyan/10"
              >
                {/* Header is a button so the whole row toggles the
                    transcript on click / keyboard activation.  Redacted
                    rows fall through to a non-interactive div so the
                    upsell card below still reads as the primary CTA. */}
                {isExpandable ? (
                  <button
                    type="button"
                    onClick={() =>
                      setExpandedId((cur) => (cur === d.id ? null : d.id))
                    }
                    aria-expanded={isExpanded}
                    aria-controls={`debate-transcript-${d.id}`}
                    data-testid={`debate-toggle-${d.id}`}
                    className="w-full text-left flex items-start justify-between gap-2 mb-2 cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-accent-cyan rounded-sm"
                  >
                    <div className="min-w-0 flex-1">
                      <div
                        className="text-foreground truncate flex items-center gap-1.5"
                        title={d.matchup ?? ""}
                      >
                        <span
                          className={`text-muted-foreground transition-transform inline-block ${
                            isExpanded ? "rotate-90" : ""
                          }`}
                          aria-hidden="true"
                        >
                          ▸
                        </span>
                        <span className="truncate">{d.matchup ?? "—"}</span>
                      </div>
                      <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5 ml-3.5">
                        {d.league ?? "—"} · {d.market_type ?? "—"} · {fmtTime(d.created_at)}
                      </div>
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      {fallbacks.length > 0 && (
                        <span
                          className="text-[9px] uppercase border border-warning/60 text-warning px-1 py-0.5 tracking-widest"
                          data-testid={`debate-fallback-badge-${d.id}`}
                          title={fallbackTitle}
                        >
                          FALLBACK · {fallbacks.length}
                        </span>
                      )}
                      <div
                        className={`text-[10px] uppercase border px-2 py-0.5 ${verdictClass} ${d.approved ? "" : "opacity-70"}`}
                      >
                        {verdict || "—"}
                      </div>
                    </div>
                  </button>
                ) : (
                  <div className="flex items-start justify-between gap-2 mb-2">
                    <div className="min-w-0 flex-1">
                      <div className="text-foreground truncate" title={d.matchup ?? ""}>
                        {d.matchup ?? "—"}
                      </div>
                      <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
                        {d.league ?? "—"} · {d.market_type ?? "—"} · {fmtTime(d.created_at)}
                      </div>
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      {fallbacks.length > 0 && (
                        <span
                          className="text-[9px] uppercase border border-warning/60 text-warning px-1 py-0.5 tracking-widest"
                          data-testid={`debate-fallback-badge-${d.id}`}
                          title={fallbackTitle}
                        >
                          FALLBACK · {fallbacks.length}
                        </span>
                      )}
                      <div
                        className={`text-[10px] uppercase border px-2 py-0.5 ${verdictClass} ${d.approved ? "" : "opacity-70"}`}
                      >
                        {verdict || "—"}
                      </div>
                    </div>
                  </div>
                )}
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
                {sketch ? <PlackettLuceTable testIdSuffix={d.id} sketch={sketch} /> : null}
                {isExpanded ? (
                  <div
                    id={`debate-transcript-${d.id}`}
                    data-testid={`debate-transcript-${d.id}`}
                    className="mt-3 pt-3 border-t border-border/50 flex flex-col gap-3"
                  >
                    {/* Moderator synthesis — the narrative the moderator
                        wrote when collapsing the persona priors into the
                        consensus.  Includes the gatekeeper's veto / approve
                        verdict when one was recorded. */}
                    <section
                      data-testid={`debate-moderator-${d.id}`}
                      className="border border-border/60 bg-muted/10 p-2.5"
                    >
                      <div className="text-[10px] uppercase tracking-widest text-muted-foreground font-bold mb-1.5">
                        MODERATOR_SYNTHESIS
                      </div>
                      {synthesis?.synthesis ? (
                        <p className="text-foreground/90 leading-relaxed whitespace-pre-wrap">
                          {synthesis.synthesis}
                        </p>
                      ) : (
                        <p className="text-muted-foreground italic">
                          No synthesis recorded.
                        </p>
                      )}
                      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2 text-[10px] text-muted-foreground uppercase tracking-wider">
                        <span>
                          MODERATOR · {synthesis?.model ?? "—"}
                        </span>
                        <span className="text-primary">
                          REC · {(synthesis?.recommendedAction ?? "—").toUpperCase()}
                        </span>
                      </div>
                      {synthesis?.gatekeeper ? (() => {
                        // Tri-state: approved === true → APPROVED,
                        // approved === false → VETOED, null/missing →
                        // UNKNOWN (don't paint a missing field as a
                        // veto, that would mislead the operator).
                        const approved = synthesis.gatekeeper.approved;
                        const tone =
                          approved === true
                            ? "border-win/50 bg-win/5"
                            : approved === false
                              ? "border-loss/50 bg-loss/5"
                              : "border-muted-foreground/40 bg-muted/10";
                        const text =
                          approved === true
                            ? "text-win"
                            : approved === false
                              ? "text-loss"
                              : "text-muted-foreground";
                        const label =
                          approved === true
                            ? "APPROVED"
                            : approved === false
                              ? "VETOED"
                              : "UNKNOWN";
                        return (
                        <div
                          data-testid={`debate-gatekeeper-${d.id}`}
                          className={`mt-2 p-2 border ${tone}`}
                        >
                          <div
                            className={`text-[10px] uppercase tracking-widest font-bold ${text}`}
                          >
                            GATEKEEPER · {label}
                          </div>
                          {synthesis.gatekeeper.vetoReason ? (
                            <p className="text-foreground/90 mt-1">
                              {synthesis.gatekeeper.vetoReason}
                            </p>
                          ) : null}
                          <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">
                            {synthesis.gatekeeper.model ?? "—"} · FINAL{" "}
                            {(synthesis.gatekeeper.recommendedAction ?? "—").toUpperCase()}
                          </div>
                        </div>
                        );
                      })() : null}
                    </section>

                    {/* One card per persona — the actual debate "voices"
                        the operator asked to see, with reasoning verbatim. */}
                    <section className="flex flex-col gap-2">
                      <div className="text-[10px] uppercase tracking-widest text-muted-foreground font-bold">
                        ANALYST_VERDICTS · {verdicts.length}
                      </div>
                      {verdicts.length === 0 ? (
                        <div className="text-[11px] text-muted-foreground italic border border-dashed border-border/60 p-2">
                          No persona verdicts recorded.
                        </div>
                      ) : (
                        verdicts.map((v, idx) => (
                          <div
                            key={`${v.persona ?? "p"}-${idx}`}
                            data-testid={`debate-persona-${d.id}-${v.persona ?? idx}`}
                            className="border border-border/60 bg-muted/5 p-2.5"
                          >
                            <div className="flex items-center justify-between gap-2 mb-1">
                              <div className="text-foreground font-semibold uppercase tracking-wider text-[11px]">
                                {(v.persona ?? "?").toUpperCase()}
                              </div>
                              <div
                                className={`text-[10px] uppercase border px-1.5 py-0.5 ${leanClass(v.lean)}`}
                              >
                                {(v.lean ?? "—").toUpperCase()} ·{" "}
                                {fmtPct(v.probability)}
                              </div>
                            </div>
                            <div className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1.5">
                              {v.provider ?? "—"}/{v.model ?? "—"}
                              {v.fallbackUsed
                                ? ` · FALLBACK${v.fallbackReason ? ` (${v.fallbackReason})` : ""}`
                                : ""}
                            </div>
                            {v.error ? (
                              <p className="text-loss leading-relaxed">
                                ERR · {v.error}
                              </p>
                            ) : v.reasoning ? (
                              <p className="text-foreground/90 leading-relaxed whitespace-pre-wrap">
                                {v.reasoning}
                              </p>
                            ) : (
                              <p className="text-muted-foreground italic">
                                No reasoning recorded.
                              </p>
                            )}
                            <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5">
                              <span>CONF · {fmtPct(v.confidence)}</span>
                              {typeof v.weight === "number" ? (
                                <span>WEIGHT · {v.weight.toFixed(2)}</span>
                              ) : null}
                              {typeof v.latencyMs === "number" ? (
                                <span>{fmtLatency(v.latencyMs)}</span>
                              ) : null}
                            </div>
                          </div>
                        ))
                      )}
                    </section>
                  </div>
                ) : null}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
