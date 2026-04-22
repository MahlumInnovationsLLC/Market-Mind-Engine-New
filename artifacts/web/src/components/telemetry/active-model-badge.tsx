import { useHealthFull, getHealthFullQueryKey } from "@workspace/api-client-react";
import { Cpu, AlertTriangle } from "lucide-react";

function formatLoadedAt(ts: number | null | undefined): string {
  if (!ts) return "unknown";
  try {
    return new Date(ts * 1000).toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
  } catch {
    return "unknown";
  }
}

export default function ActiveModelBadge() {
  const { data: health, isLoading, isError } = useHealthFull({
    query: { refetchInterval: 30000, queryKey: getHealthFullQueryKey() },
  });

  if (isLoading) {
    return (
      <div
        className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground"
        data-testid="badge-active-model-loading"
      >
        MODEL …
      </div>
    );
  }

  const active = health?.active_predictor;

  if (isError || !active) {
    const title = isError
      ? "Could not load /api/health"
      : "No predictor is currently bound — trading is in a degraded state.";
    return (
      <div
        className="border border-destructive px-3 py-1 uppercase tracking-widest text-[10px] text-destructive flex items-center gap-2"
        title={title}
        data-testid="badge-active-model-degraded"
      >
        <AlertTriangle className="w-3 h-3" />
        MODEL :: NONE
      </div>
    );
  }

  const shortName = active.name.split("/").pop() ?? active.name;
  const tone = active.bound
    ? "text-primary border-primary"
    : "text-muted-foreground border-border";
  const fullSha = active.sha256 ?? active.version;
  const tooltip =
    `${active.name}\n` +
    `sha256: ${fullSha}\n` +
    `type: ${active.type}\n` +
    `loaded: ${formatLoadedAt(active.loaded_at)}` +
    (active.bound ? "" : "\n(peek — not yet bound)");

  return (
    <div
      className={`border ${tone} px-3 py-1 uppercase tracking-widest text-[10px] flex items-center gap-2`}
      title={tooltip}
      data-testid="badge-active-model"
    >
      <Cpu className="w-3 h-3" />
      <span className="text-muted-foreground">MODEL</span>
      <span data-testid="text-active-model-name">{shortName}</span>
      <span className="text-muted-foreground">@</span>
      <span data-testid="text-active-model-version">{active.version}</span>
    </div>
  );
}
