import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";

type FallbackRow = {
  persona_id: string;
  fallbacks: number;
  primary_provider: string | null;
  primary_model: string | null;
  fallback_provider: string | null;
  fallback_model: string | null;
  last_reason: string | null;
};

type FallbackResponse = {
  date_utc: string;
  items: FallbackRow[];
};

export default function SwarmFallbacksToday() {
  const { data, isLoading } = useQuery<FallbackResponse>({
    queryKey: ["dashboard", "swarm-fallbacks-today"],
    queryFn: () =>
      apiFetch<FallbackResponse>("/api/dashboard/swarm-fallbacks-today"),
    refetchInterval: 60_000,
  });

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-72 bg-card" />;
  }

  const items = data?.items ?? [];
  const dateUtc = data?.date_utc ?? "";
  const total = items.reduce((acc, r) => acc + (r.fallbacks ?? 0), 0);

  return (
    <div
      className="border border-border bg-card flex flex-col h-[420px]"
      data-testid="card-swarm-fallbacks-today"
    >
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          PROVIDER_FALLBACKS_TODAY {dateUtc ? `· ${dateUtc}_UTC` : ""}
        </h2>
        <span className="text-xs text-muted-foreground">
          {total}_EVENTS · {items.length}_PERSONAS
        </span>
      </div>
      <div className="overflow-auto grow">
        {items.length === 0 ? (
          <div className="p-4 text-center text-muted-foreground text-xs">
            NO_FALLBACKS_TODAY
          </div>
        ) : (
          <table className="w-full text-xs text-left">
            <thead className="sticky top-0 bg-card border-b border-border text-muted-foreground uppercase text-[10px]">
              <tr>
                <th className="p-2 font-normal">PERSONA</th>
                <th className="p-2 font-normal text-right">N</th>
                <th className="p-2 font-normal">PRIMARY → FALLBACK</th>
                <th className="p-2 font-normal">LAST_REASON</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/50">
              {items.map((r) => (
                <tr
                  key={r.persona_id}
                  className="hover:bg-muted/30 transition-colors"
                  data-testid={`row-fallback-${r.persona_id}`}
                >
                  <td
                    className="p-2 text-primary uppercase truncate max-w-[160px]"
                    title={r.persona_id}
                  >
                    {r.persona_id}
                  </td>
                  <td className="p-2 text-right text-warning font-bold">
                    {r.fallbacks}
                  </td>
                  <td className="p-2 text-muted-foreground text-[10px]">
                    {(r.primary_provider ?? "?")}/{r.primary_model ?? "?"}
                    {" → "}
                    <span className="text-foreground">
                      {(r.fallback_provider ?? "?")}/{r.fallback_model ?? "?"}
                    </span>
                  </td>
                  <td
                    className="p-2 text-muted-foreground text-[10px] truncate max-w-[260px]"
                    title={r.last_reason ?? ""}
                  >
                    {r.last_reason ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
