import { useMemo } from "react";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { apiFetch } from "@/lib/api";

type SnapshotCard = {
  key: string;
  label: string;
  status: "ok" | "degraded" | "down" | "unconfigured";
};

type HealthSnapshot = {
  cards: SnapshotCard[];
  summary: {
    ok: number;
    degraded: number;
    down: number;
    unconfigured: number;
    overall: "ok" | "degraded" | "down";
  };
};

export function DegradedModeBanner() {
  const { getToken, isSignedIn } = useAuth();
  const token = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );

  // Spec: subscribe to /api/health/snapshot (the operator collector
  // endpoint) so the banner reflects the same health view the /health
  // page renders, not the lighter-weight /api/health/full readiness probe.
  const { data } = useQuery<HealthSnapshot>({
    queryKey: ["shell", "health-snapshot"],
    queryFn: () =>
      apiFetch<HealthSnapshot>("/api/health/snapshot", { getToken: token }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  if (!data || data.summary?.overall === "ok") return null;

  // Surface up to 4 component keys that aren't OK so the operator can
  // see at a glance what is degraded without opening the /health page.
  const reasons = (data.cards ?? [])
    .filter((c) => c.status === "down" || c.status === "degraded")
    .slice(0, 4)
    .map((c) => c.key.toUpperCase());

  const isDown = data.summary?.overall === "down";

  return (
    <div
      className={`border-b px-4 py-2 text-[10px] uppercase tracking-widest font-mono flex items-center gap-2 ${
        isDown
          ? "border-loss/40 bg-loss/10 text-loss"
          : "border-warning/40 bg-warning/10 text-warning"
      }`}
      data-testid="banner-degraded"
    >
      <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
      <span className="font-bold">
        {isDown ? "SUBSYSTEM DOWN" : "DEGRADED MODE"}
      </span>
      {reasons.length > 0 ? (
        <span className="opacity-80">· {reasons.join(" · ")}</span>
      ) : null}
      <span className="ml-auto opacity-60 normal-case tracking-normal">
        Some subsystems are unavailable. Trading may be paused.
      </span>
    </div>
  );
}
