import { useMemo } from "react";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "wouter";
import { ArrowRight, Activity } from "lucide-react";
import { apiFetch } from "@/lib/api";

type SnapshotCard = {
  key: string;
  status: "ok" | "degraded" | "down" | "unconfigured";
  detail?: string | null;
  extras?: Record<string, unknown> | null;
};

type HealthSnapshot = {
  cards: SnapshotCard[];
};

function summarise(card: SnapshotCard): string {
  const extras = (card.extras ?? {}) as Record<string, unknown>;
  const reason = typeof extras.reason === "string" ? extras.reason : null;
  const ageRaw = extras.age_seconds;
  const age =
    typeof ageRaw === "number" && Number.isFinite(ageRaw)
      ? Math.round(ageRaw)
      : null;
  const detail = typeof card.detail === "string" ? card.detail : "";

  if (reason === "stalled") {
    if (age !== null) return `Stalled — last cycle ${age}s ago`;
    return "Stalled — no recent cycle";
  }
  if (reason === "errors") {
    const match = detail.match(/(\d+)\s+error/i);
    const count = match ? Number(match[1]) : null;
    if (count && count > 0) {
      return `Last cycle returned ${count} error${count === 1 ? "" : "s"}`;
    }
    return "Last cycle returned errors";
  }
  // Fallback: surface the server-provided detail so a future failure
  // mode we don't yet name still produces a useful banner.
  return detail || "Updater is degraded";
}

export function EloUpdaterBanner() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  const { data } = useQuery<HealthSnapshot>({
    queryKey: ["shell", "health-snapshot"],
    queryFn: () =>
      apiFetch<HealthSnapshot>("/api/health/snapshot", { getToken: token }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const card = useMemo<SnapshotCard | null>(() => {
    const found = (data?.cards ?? []).find((c) => c.key === "elo_updater");
    return found ?? null;
  }, [data]);

  if (!card || card.status !== "degraded") return null;

  const summary = summarise(card);

  return (
    <div
      className="border-b border-warning/40 bg-warning/10 text-warning px-4 py-2 text-[10px] uppercase tracking-widest font-mono flex items-center gap-2"
      data-testid="banner-elo-updater"
    >
      <Activity className="w-3.5 h-3.5 shrink-0" />
      <span className="font-bold">TEAM RATING UPDATER DEGRADED</span>
      <span
        className="opacity-80 normal-case tracking-normal"
        data-testid="text-elo-updater-summary"
      >
        · {summary}
      </span>
      <Link
        href="/system?tab=controls&focus=elo-updater"
        className="ml-auto inline-flex items-center gap-1 px-2 py-0.5 rounded border border-warning/50 hover:bg-warning/20 hover:text-warning focus:outline-none focus:ring-1 focus:ring-warning/60 transition-colors"
        data-testid="link-elo-updater-panel"
      >
        Open updater panel
        <ArrowRight className="w-3 h-3" />
      </Link>
    </div>
  );
}
