import { useQuery } from "@tanstack/react-query";
import { resolveApiUrl } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";

type OpenPayoutData = {
  open_count: number;
  total_quantity: number;
  cost_basis_usd: number;
  potential_payout_usd: number;
  potential_profit_usd: number;
  generated_at: string;
};

function fmtUSD(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(Number(n));
}

export default function OpenPayoutTile() {
  const { data, isLoading, error } = useQuery<OpenPayoutData>({
    queryKey: ["dashboard-open-payout"],
    queryFn: async () => {
      const r = await fetch(
        resolveApiUrl(
          `${import.meta.env.BASE_URL}api/dashboard/open-payout`,
        ),
        { credentials: "include" },
      );
      if (!r.ok) throw new Error(`open-payout ${r.status}`);
      return r.json();
    },
    refetchInterval: 30_000,
    staleTime: 25_000,
  });

  const profit = data?.potential_profit_usd ?? null;
  const profitClass =
    profit === null ? "text-foreground" : profit >= 0 ? "text-win" : "text-loss";

  return (
    <GlassCard
      tone="solid"
      padding="sm"
      data-testid="card-open-payout"
      className="flex flex-col gap-1"
    >
      <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
        POTENTIAL PAYOUT · OPEN BETS
      </span>
      {isLoading ? (
        <span
          className="block h-7 w-28 animate-pulse bg-muted/20"
          aria-busy="true"
          data-testid="open-payout-skeleton"
        />
      ) : error ? (
        <span
          className="text-xs text-rose-400"
          data-testid="open-payout-error"
        >
          Failed to load.
        </span>
      ) : (
        <>
          <span
            className="text-2xl font-bold tabular-nums text-primary"
            data-testid="open-payout-max"
          >
            {fmtUSD(data?.potential_payout_usd)}
          </span>
          <span
            className="text-[11px] tabular-nums text-muted-foreground"
            data-testid="open-payout-breakdown"
          >
            {data?.open_count ?? 0} open · stake {fmtUSD(data?.cost_basis_usd)}
            {" · max profit "}
            <span className={profitClass}>{fmtUSD(profit)}</span>
          </span>
        </>
      )}
    </GlassCard>
  );
}
