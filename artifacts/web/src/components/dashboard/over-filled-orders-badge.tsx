import { useState } from "react";
import {
  getGetOverFilledOrdersTodayQueryKey,
  useGetOverFilledOrdersToday,
} from "@workspace/api-client-react";

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Operator-console badge for GTC orders that Kalshi over-filled today.
 *
 * Backed by `/api/dashboard/over-filled-orders-today` — itself a join
 * of `positions.over_filled = TRUE` (set by `app.kalshi.lifecycle`)
 * with the `order_fills` ledger so the discarded count is exact.
 *
 * Renders nothing while the count is 0 to keep the dashboard quiet on
 * normal days; clicking the badge expands a per-order list with
 * order_id, market, requested vs filled count, and the discarded count.
 */
export default function OverFilledOrdersBadge() {
  const [open, setOpen] = useState(false);
  const { data } = useGetOverFilledOrdersToday({
    query: {
      queryKey: getGetOverFilledOrdersTodayQueryKey(),
      refetchInterval: 60_000,
      staleTime: 30_000,
    },
  });

  const count = data?.count ?? 0;
  if (count <= 0) return null;

  const items = data?.items ?? [];

  return (
    <div className="font-mono" data-testid="over-filled-orders">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-2 border border-loss/60 bg-loss/10 px-3 py-1.5 text-[11px] uppercase tracking-widest text-loss hover:bg-loss/20"
        aria-expanded={open}
        data-testid="over-filled-orders-badge"
      >
        <span className="font-bold tabular-nums">{count}</span>
        <span>over-filled today</span>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>

      {open ? (
        <div
          className="mt-2 border border-border bg-card/60 p-3"
          data-testid="over-filled-orders-list"
        >
          <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">
            GTC orders Kalshi delivered more contracts than requested
            (UTC {data?.date_utc ?? "—"})
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-widest text-muted-foreground">
                  <th className="py-1 pr-3">Order ID</th>
                  <th className="py-1 pr-3">Market</th>
                  <th className="py-1 pr-3">Side</th>
                  <th className="py-1 pr-3 text-right">Requested</th>
                  <th className="py-1 pr-3 text-right">Filled</th>
                  <th className="py-1 pr-3 text-right">Discarded</th>
                  <th className="py-1 pr-3 text-right">Last fill</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr
                    key={row.order_id}
                    className="border-t border-border/40"
                    data-testid={`over-filled-row-${row.order_id}`}
                  >
                    <td
                      className="py-1 pr-3 font-mono text-[11px] text-foreground"
                      title={row.order_id}
                    >
                      {row.order_id}
                    </td>
                    <td
                      className="py-1 pr-3 text-foreground"
                      title={row.market_id ?? ""}
                    >
                      {row.market_title ?? row.market_id ?? "—"}
                    </td>
                    <td className="py-1 pr-3 uppercase">
                      {(row.side ?? "—").toString()}
                    </td>
                    <td className="py-1 pr-3 text-right tabular-nums">
                      {row.requested_count}
                    </td>
                    <td className="py-1 pr-3 text-right tabular-nums">
                      {row.filled_count}
                    </td>
                    <td className="py-1 pr-3 text-right font-bold tabular-nums text-loss">
                      {row.discarded_count}
                    </td>
                    <td className="py-1 pr-3 text-right tabular-nums text-muted-foreground">
                      {fmtTime(row.last_fill_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}
