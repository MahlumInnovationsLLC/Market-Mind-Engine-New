import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";

type ProviderRow = {
  provider: string;
  total: number;
  fallback: number;
  rate: number;
  reasons: string[];
  would_alert: boolean;
};

type ProviderFallbackRatesResponse = {
  window_minutes: number;
  threshold_pct: number;
  min_samples: number;
  alert_enabled: boolean;
  items: ProviderRow[];
};

function formatPct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

export default function ProviderFallbackRates() {
  const { data, isLoading } = useQuery<ProviderFallbackRatesResponse>({
    queryKey: ["dashboard", "llm-fallback-rates"],
    queryFn: () =>
      apiFetch<ProviderFallbackRatesResponse>(
        "/api/dashboard/llm-fallback-rates",
      ),
    refetchInterval: 30_000,
  });

  if (isLoading) {
    return (
      <div
        className="border border-border p-4 animate-pulse h-72 bg-card"
        data-testid="card-llm-fallback-rates-loading"
      />
    );
  }

  const items = data?.items ?? [];
  const window = data?.window_minutes ?? 0;
  const thresholdPct = data?.threshold_pct ?? 0;
  const minSamples = data?.min_samples ?? 0;

  return (
    <div
      className="border border-border bg-card flex flex-col h-[420px]"
      data-testid="card-llm-fallback-rates"
    >
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          LLM_FALLBACK_RATE · LAST_{window}m
        </h2>
        <span
          className="text-xs text-muted-foreground"
          data-testid="text-llm-fallback-rates-config"
        >
          THRESHOLD {thresholdPct.toFixed(1)}% · MIN_N {minSamples}
        </span>
      </div>
      <div className="overflow-auto grow">
        {items.length === 0 ? (
          <div
            className="p-4 text-center text-muted-foreground text-xs"
            data-testid="text-llm-fallback-rates-empty"
          >
            NO_PERSONA_CALLS_IN_WINDOW
          </div>
        ) : (
          <table className="w-full text-xs text-left">
            <thead className="sticky top-0 bg-card border-b border-border text-muted-foreground uppercase text-[10px]">
              <tr>
                <th className="p-2 font-normal">PROVIDER</th>
                <th className="p-2 font-normal text-right">RATE</th>
                <th className="p-2 font-normal text-right">FALLBACK / TOTAL</th>
                <th className="p-2 font-normal">RECENT_REASONS</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/50">
              {items.map((r) => {
                const reasonText = r.reasons.length
                  ? r.reasons.join(" · ")
                  : "—";
                return (
                  <tr
                    key={r.provider}
                    className="hover:bg-muted/30 transition-colors"
                    data-testid={`row-llm-fallback-rate-${r.provider}`}
                  >
                    <td
                      className="p-2 text-primary uppercase truncate max-w-[140px]"
                      title={r.provider}
                    >
                      {r.provider}
                    </td>
                    <td
                      className={`p-2 text-right font-bold ${
                        r.would_alert ? "text-destructive" : "text-foreground"
                      }`}
                      data-testid={`text-llm-fallback-rate-${r.provider}`}
                    >
                      {formatPct(r.rate)}
                    </td>
                    <td className="p-2 text-right text-muted-foreground">
                      {r.fallback} / {r.total}
                    </td>
                    <td
                      className="p-2 text-muted-foreground text-[10px] truncate max-w-[260px]"
                      title={reasonText}
                    >
                      {reasonText}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
