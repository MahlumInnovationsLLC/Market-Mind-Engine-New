import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { resolveApiUrl } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

// Task #828 — Cross-book line-shopping router operator tile.
// Lists the last 24h best_line_alert events; one-click "mark placed
// manually" toggle records the operator's action so the daily honesty
// report and downstream P&L reconciliation can join against it.

type BestLineAlert = {
  dedupe_key: string;
  kind: string;
  league: string;
  market_type: string;
  matchup: string;
  sent_at: string | null;
  action: string | null;
  operator_email: string | null;
  placed_book: string | null;
  placed_amount_usd: number | null;
  note: string | null;
  action_at: string | null;
  placed_manually: boolean;
};

type BestLineAlertsResponse = {
  items: BestLineAlert[];
  hours: number;
  count?: number;
  error?: string;
};

function fmtAgo(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const secs = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function kindBadge(kind: string): { label: string; tone: string } {
  if (kind === "reroute_polymarket") {
    return { label: "Rerouted to Polymarket", tone: "text-blue-300 border-blue-500/40 bg-blue-500/10" };
  }
  if (kind === "block_traditional") {
    return { label: "Blocked — better at sportsbook", tone: "text-amber-300 border-amber-500/40 bg-amber-500/10" };
  }
  return { label: kind || "—", tone: "text-slate-300 border-slate-500/40 bg-slate-500/10" };
}

export default function BestLineAlertsPanel() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error } = useQuery<BestLineAlertsResponse>({
    queryKey: ["best-line-alerts", 24],
    queryFn: async () => {
      const r = await fetch(resolveApiUrl("/api/public/best-line-alerts?hours=24&limit=100"));
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return (await r.json()) as BestLineAlertsResponse;
    },
    refetchInterval: 60_000,
  });

  const markPlaced = useMutation({
    mutationFn: async (args: { key: string; placed: boolean }) => {
      const r = await fetch(resolveApiUrl("/api/admin/best-line-alerts/mark-placed"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ dedupe_key: args.key, placed: args.placed }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["best-line-alerts", 24] });
    },
  });

  const items = data?.items ?? [];

  return (
    <GlassCard>
      <SectionHeader
        title="Best-line alerts (last 24h)"
        subtitle="Cross-book line-shopping router (task #828). Manual placements are recorded for P&L reconciliation."
      />
      <div className="px-4 pb-4">
        {isLoading && <div className="text-sm text-slate-400">Loading…</div>}
        {isError && (
          <div className="text-sm text-rose-400">
            Failed to load: {(error as Error)?.message ?? "unknown error"}
          </div>
        )}
        {!isLoading && !isError && items.length === 0 && (
          <div className="text-sm text-slate-400">
            No best-line alerts in the last 24 hours. The gate is armed; either no candidates
            traded sports markets or every chosen venue offered the best line.
          </div>
        )}
        {items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead className="text-slate-400 text-left">
                <tr>
                  <th className="py-2 pr-3">When</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">League / Market</th>
                  <th className="py-2 pr-3">Matchup</th>
                  <th className="py-2 pr-3">Placed manually?</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => {
                  const badge = kindBadge(row.kind);
                  const placed = row.placed_manually;
                  const disabled = markPlaced.isPending;
                  return (
                    <tr key={row.dedupe_key} className="border-t border-white/5">
                      <td className="py-2 pr-3 text-slate-300 whitespace-nowrap">{fmtAgo(row.sent_at)}</td>
                      <td className="py-2 pr-3">
                        <span className={`text-xs px-2 py-0.5 rounded border ${badge.tone}`}>{badge.label}</span>
                      </td>
                      <td className="py-2 pr-3 text-slate-200">
                        {row.league} <span className="text-slate-500">/</span> {row.market_type}
                      </td>
                      <td className="py-2 pr-3 text-slate-100">{row.matchup || "—"}</td>
                      <td className="py-2 pr-3">
                        {row.kind === "block_traditional" ? (
                          <button
                            type="button"
                            disabled={disabled}
                            onClick={() => markPlaced.mutate({ key: row.dedupe_key, placed: !placed })}
                            className={`text-xs px-2 py-1 rounded border transition ${
                              placed
                                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                                : "border-slate-500/40 text-slate-300 hover:bg-white/5"
                            } ${disabled ? "opacity-50" : ""}`}
                          >
                            {placed ? "Placed ✓" : "Mark placed"}
                          </button>
                        ) : (
                          <span className="text-slate-500 text-xs">n/a (auto-routed)</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </GlassCard>
  );
}
