import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@clerk/react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { apiFetch } from "@/lib/api";

type KalshiPosition = {
  market_ticker: string;
  position: number;
  market_exposure_cents: number;
  realized_pnl_cents: number;
  fees_paid_cents: number;
};

type KalshiPositionsResponse = {
  items: KalshiPosition[];
  count: number;
};

function fmtUsd(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || Number.isNaN(cents)) return "—";
  const usd = cents / 100;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(usd);
}

function pnlClass(c: number | null | undefined): string {
  if (c === null || c === undefined) return "text-foreground";
  if (c < 0) return "text-loss";
  if (c > 0) return "text-win";
  return "text-foreground";
}

export default function ActivePositions() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const { data, isLoading, isError } = useQuery<KalshiPositionsResponse>({
    queryKey: ["dashboard", "active-positions"],
    queryFn: () =>
      apiFetch<KalshiPositionsResponse>("/api/kalshi/positions?limit=10", {
        getToken: token,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const items = (data?.items ?? []).filter((p) => p.position !== 0);
  // Largest exposure first.
  items.sort(
    (a, b) =>
      Math.abs(b.market_exposure_cents) - Math.abs(a.market_exposure_cents),
  );
  const top = items.slice(0, 8);

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-active-positions">
      <SectionHeader
        eyebrow="POSITIONS"
        title="Active positions"
        subtitle={
          data ? `${items.length} open across Kalshi book` : "Live Kalshi book"
        }
        actions={
          <a
            href="/positions"
            className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground transition-colors hover:border-primary hover:text-primary"
            data-testid="link-positions-all"
          >
            All →
          </a>
        }
      />
      {isLoading ? (
        <div className="space-y-2" data-testid="skeleton-active-positions">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-8 animate-pulse bg-muted/10" />
          ))}
        </div>
      ) : isError ? (
        <div className="border border-loss/40 bg-loss/5 p-3 text-[11px] uppercase tracking-widest text-loss">
          KALSHI_FEED_UNAVAILABLE
        </div>
      ) : top.length === 0 ? (
        <div className="border border-border bg-card/40 p-3 text-center text-xs uppercase tracking-widest text-muted-foreground">
          NO_OPEN_POSITIONS
        </div>
      ) : (
        <table className="w-full text-xs">
          <thead className="border-b border-border text-[10px] uppercase tracking-widest text-muted-foreground">
            <tr>
              <th className="py-1 text-left font-normal">MARKET</th>
              <th className="py-1 text-right font-normal">QTY</th>
              <th className="py-1 text-right font-normal">EXPOSURE</th>
              <th className="py-1 text-right font-normal">REAL_PNL</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {top.map((p) => (
              <tr
                key={p.market_ticker}
                data-testid={`row-position-${p.market_ticker}`}
                className="hover:bg-muted/20"
              >
                <td className="py-1.5 pr-2 truncate max-w-[220px] font-mono text-[11px]" title={p.market_ticker}>
                  {p.market_ticker}
                </td>
                <td className="py-1.5 text-right tabular-nums">
                  {p.position}
                </td>
                <td className="py-1.5 text-right tabular-nums text-primary">
                  {fmtUsd(p.market_exposure_cents)}
                </td>
                <td
                  className={`py-1.5 text-right tabular-nums font-bold ${pnlClass(p.realized_pnl_cents)}`}
                >
                  {fmtUsd(p.realized_pnl_cents)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </GlassCard>
  );
}
