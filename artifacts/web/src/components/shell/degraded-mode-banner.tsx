import { useHealthFull, getHealthFullQueryKey } from "@workspace/api-client-react";
import { AlertTriangle } from "lucide-react";

export function DegradedModeBanner() {
  const { data } = useHealthFull({
    query: { refetchInterval: 30_000, queryKey: getHealthFullQueryKey() },
  });

  if (!data || data.status === "ok") return null;

  const reasons: string[] = [];
  if (data.postgres && !data.postgres.ok) reasons.push("DB");
  if (data.storage && !data.storage.ok) reasons.push("STORAGE");
  if (data.secrets_present) {
    if (!data.secrets_present.kalshi) reasons.push("KALSHI_AUTH");
    if (!data.secrets_present.openai && !data.secrets_present.anthropic)
      reasons.push("LLM");
  }

  return (
    <div
      className="border-b border-warning/40 bg-warning/10 text-warning px-4 py-2 text-[10px] uppercase tracking-widest font-mono flex items-center gap-2"
      data-testid="banner-degraded"
    >
      <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
      <span className="font-bold">DEGRADED MODE</span>
      {reasons.length > 0 ? (
        <span className="opacity-80">· {reasons.join(" · ")}</span>
      ) : null}
      <span className="ml-auto opacity-60 normal-case tracking-normal">
        Some subsystems are unavailable. Trading may be paused.
      </span>
    </div>
  );
}
