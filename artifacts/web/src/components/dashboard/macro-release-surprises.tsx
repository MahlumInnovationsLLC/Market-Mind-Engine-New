import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

interface ReleaseHistoryItem {
  source: string;
  signal_id: string;
  release_type: string;
  release_at: string | null;
  consensus: number | null;
  previous: number | null;
  actual: number | null;
  revised_previous: number | null;
  surprise: number | null;
  direction: "above" | "below" | "inline" | "unknown";
}

interface ReleaseHistoryResponse {
  items: ReleaseHistoryItem[];
}

function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function fmtSurprise(n: number | null): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}`;
}

function fmtTime(s: string | null): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

function directionTone(direction: ReleaseHistoryItem["direction"]): string {
  switch (direction) {
    case "above":
      return "text-success";
    case "below":
      return "text-destructive";
    case "inline":
      return "text-muted-foreground";
    default:
      return "text-muted-foreground";
  }
}

function directionGlyph(direction: ReleaseHistoryItem["direction"]): string {
  switch (direction) {
    case "above":
      return "▲";
    case "below":
      return "▼";
    case "inline":
      return "■";
    default:
      return "·";
  }
}

export default function MacroReleaseSurprises() {
  const q = useQuery({
    queryKey: ["dashboard-macro-release-surprises"],
    queryFn: async () =>
      customFetch<ReleaseHistoryResponse>(
        "/api/events-econ/release-history?limit=20",
      ),
    staleTime: 60_000,
    refetchInterval: 120_000,
  });

  const items = useMemo(() => q.data?.items ?? [], [q.data]);

  return (
    <GlassCard
      tone="solid"
      padding="md"
      data-testid="card-macro-release-surprises"
    >
      <SectionHeader
        eyebrow="EVENTS · ECON"
        title="Macro release surprises"
        subtitle="Last 20 settled prints — actual vs consensus."
      />
      {q.isLoading && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          Loading recent releases…
        </div>
      )}
      {q.isError && (
        <div className="text-xs text-destructive py-6 text-center">
          Failed to load: {String((q.error as Error)?.message ?? q.error)}
        </div>
      )}
      {!q.isLoading && !q.isError && items.length === 0 && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          No settled macro releases yet.
        </div>
      )}
      {!q.isLoading && !q.isError && items.length > 0 && (
        <ul
          className="text-xs font-mono divide-y divide-border"
          data-testid="list-macro-release-surprises"
        >
          <li className="grid grid-cols-[8rem_1fr_5rem_5rem_5rem_1.5rem] gap-2 py-1 text-[10px] uppercase text-muted-foreground">
            <span>When</span>
            <span>Type</span>
            <span className="text-right">Consensus</span>
            <span className="text-right">Actual</span>
            <span className="text-right">Surprise</span>
            <span className="text-right" />
          </li>
          {items.map((it) => (
            <li
              key={`${it.source}/${it.signal_id}`}
              className="grid grid-cols-[8rem_1fr_5rem_5rem_5rem_1.5rem] gap-2 py-1 items-center"
              data-testid={`row-release-${it.signal_id}`}
            >
              <span className="text-muted-foreground truncate">
                {fmtTime(it.release_at)}
              </span>
              <span className="font-bold">{it.release_type}</span>
              <span className="text-right">{fmtNum(it.consensus)}</span>
              <span className="text-right">{fmtNum(it.actual)}</span>
              <span className={`text-right ${directionTone(it.direction)}`}>
                {fmtSurprise(it.surprise)}
              </span>
              <span
                className={`text-right ${directionTone(it.direction)}`}
                aria-label={it.direction}
                title={it.direction}
              >
                {directionGlyph(it.direction)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </GlassCard>
  );
}
