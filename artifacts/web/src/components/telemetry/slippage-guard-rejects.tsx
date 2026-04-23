import {
  useGetSlippageGuardRejects,
  getGetSlippageGuardRejectsQueryKey,
} from "@workspace/api-client-react";

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtCents(c: number | null | undefined): string {
  if (c === null || c === undefined) return "—";
  return `${c}¢`;
}

function fmtPct(p: number | null | undefined): string {
  if (p === null || p === undefined) return "—";
  return `${p.toFixed(1)}%`;
}

export interface SlippageGuardRejectsPanelProps {
  cycleId: number;
}

export default function SlippageGuardRejectsPanel({
  cycleId,
}: SlippageGuardRejectsPanelProps) {
  const params = { cycle_id: cycleId, limit: 100 };
  const { data, isLoading, isError } = useGetSlippageGuardRejects(params, {
    query: {
      queryKey: getGetSlippageGuardRejectsQueryKey(params),
      staleTime: 30_000,
    },
  });

  if (isLoading) {
    return (
      <div
        className="border-t border-border bg-muted/10 p-4 text-xs text-muted-foreground"
        data-testid="slippage-guard-panel-loading"
      >
        Loading slippage-guard rejects…
      </div>
    );
  }
  if (isError) {
    return (
      <div className="border-t border-border bg-muted/10 p-4 text-xs text-loss">
        Failed to load slippage-guard rejects for cycle {cycleId}.
      </div>
    );
  }

  const items = data?.items ?? [];
  if (items.length === 0) {
    return (
      <div className="border-t border-border bg-muted/10 p-4 text-xs text-muted-foreground">
        No slippage-guard rejects recorded inside this cycle.
      </div>
    );
  }

  return (
    <div
      className="border-t border-border bg-muted/10"
      data-testid={`slippage-guard-panel-${cycleId}`}
    >
      <div className="overflow-auto">
        <table className="w-full text-[11px] text-left">
          <thead className="bg-card/40 text-muted-foreground uppercase text-[10px]">
            <tr>
              <th className="p-2 font-normal">TIME</th>
              <th className="p-2 font-normal">MARKET</th>
              <th className="p-2 font-normal">SIDE</th>
              <th className="p-2 font-normal text-right">REQ/AVAIL</th>
              <th className="p-2 font-normal text-right">WORST</th>
              <th className="p-2 font-normal text-right">LIMIT</th>
              <th className="p-2 font-normal text-right">SLIP</th>
              <th className="p-2 font-normal text-right">EDGE ERASED</th>
              <th className="p-2 font-normal">DETAIL</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/40">
            {items.map((r) => (
              <tr
                key={r.id}
                data-testid={`slippage-guard-row-${r.id}`}
                className="hover:bg-muted/20 transition-colors"
              >
                <td className="p-2 text-muted-foreground whitespace-nowrap">
                  {fmtTime(r.created_at)}
                </td>
                <td className="p-2 text-primary font-mono">
                  {r.market_ticker ?? "—"}
                </td>
                <td className="p-2 uppercase">{r.side ?? "—"}</td>
                <td className="p-2 text-right whitespace-nowrap">
                  {(r.requested_count ?? "—") +
                    " / " +
                    (r.filled_available ?? "—")}
                </td>
                <td className="p-2 text-right">
                  {fmtCents(r.worst_fill_price_cents)}
                </td>
                <td className="p-2 text-right">
                  {fmtCents(r.limit_price_cents)}
                </td>
                <td className="p-2 text-right">{fmtCents(r.slippage_cents)}</td>
                <td className="p-2 text-right text-loss">
                  {fmtPct(r.edge_erased_pct)}
                </td>
                <td className="p-2 text-muted-foreground">
                  {r.detail ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
