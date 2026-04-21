import { useGetSwarmPersonas } from "@workspace/api-client-react";

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtNum(n: number | null | undefined, digits = 3): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(digits);
}

export default function SwarmAnalysts() {
  const { data, isLoading } = useGetSwarmPersonas();

  if (isLoading) {
    return <div className="border border-border p-4 animate-pulse h-72 bg-card" />;
  }

  const items = data?.items ?? [];

  return (
    <div className="border border-border bg-card flex flex-col h-[420px]">
      <div className="border-b border-border p-2 px-3 bg-muted/20 flex justify-between items-center shrink-0">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          SWARM_ANALYSTS
        </h2>
        <span className="text-xs text-muted-foreground">{items.length}_PERSONAS</span>
      </div>
      <div className="overflow-auto grow">
        <table className="w-full text-xs text-left">
          <thead className="sticky top-0 bg-card border-b border-border text-muted-foreground uppercase text-[10px]">
            <tr>
              <th className="p-2 font-normal">PERSONA</th>
              <th className="p-2 font-normal">LEAGUE</th>
              <th className="p-2 font-normal text-right">N</th>
              <th className="p-2 font-normal text-right">BRIER</th>
              <th className="p-2 font-normal text-right">CAL_ERR</th>
              <th className="p-2 font-normal text-right">ACC</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {items.length === 0 ? (
              <tr>
                <td colSpan={6} className="p-4 text-center text-muted-foreground">
                  NO_METRICS_FOUND
                </td>
              </tr>
            ) : (
              items.map((p, i) => {
                const brier = p.brier ?? null;
                const brierClass =
                  brier === null
                    ? "text-muted-foreground"
                    : brier < 0.2
                      ? "text-win"
                      : brier > 0.25
                        ? "text-loss"
                        : "text-foreground";
                return (
                  <tr key={i} className="hover:bg-muted/30 transition-colors">
                    <td className="p-2 text-primary uppercase truncate max-w-[140px]" title={p.persona ?? ""}>
                      {p.persona ?? "—"}
                    </td>
                    <td className="p-2 text-muted-foreground uppercase">
                      {p.league ?? "—"}
                      {p.market_type ? (
                        <span className="text-[10px] block opacity-60">{p.market_type}</span>
                      ) : null}
                    </td>
                    <td className="p-2 text-right">{p.n ?? "—"}</td>
                    <td className={`p-2 text-right font-bold ${brierClass}`}>{fmtNum(brier, 3)}</td>
                    <td className="p-2 text-right text-muted-foreground">{fmtNum(p.calibration_error, 3)}</td>
                    <td className="p-2 text-right">{fmtPct(p.accuracy, 0)}</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
