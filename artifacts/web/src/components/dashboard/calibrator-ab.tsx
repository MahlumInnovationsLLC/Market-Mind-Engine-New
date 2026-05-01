import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { customFetch } from "@workspace/api-client-react";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";

interface AbSnapshot {
  id: number;
  role: "champion" | "challenger" | "retired_champion";
  sample_count: number;
  brier_in_sample: number | null;
  brier_out_of_sample: number | null;
  fit_at: string | null;
}

interface AbItem {
  league: string;
  champion: AbSnapshot | null;
  challenger: AbSnapshot | null;
  snapshots: AbSnapshot[];
  last_promotion_at: string | null;
}

interface AbResponse {
  items: AbItem[];
  supported: string[];
  champion_count?: number;
  challenger_count?: number;
  error?: string;
}

function fmt(n: number | null | undefined, digits = 4): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

function deltaTone(champion: number | null, challenger: number | null): string {
  if (champion == null || challenger == null) return "text-muted-foreground";
  if (challenger < champion - 1e-9) return "text-success";
  if (challenger > champion + 1e-9) return "text-destructive";
  return "text-muted-foreground";
}

function RoleDot({ role }: { role: AbSnapshot["role"] }) {
  const cls =
    role === "champion"
      ? "bg-success"
      : role === "challenger"
        ? "bg-primary"
        : "bg-muted-foreground/40";
  return (
    <span
      className={`inline-block w-1.5 h-1.5 rounded-full ${cls}`}
      title={role}
    />
  );
}

function LeagueRow({ item }: { item: AbItem }) {
  const ch = item.champion;
  const cl = item.challenger;
  const cmpDelta =
    ch?.brier_out_of_sample != null && cl?.brier_out_of_sample != null
      ? cl.brier_out_of_sample - ch.brier_out_of_sample
      : null;
  return (
    <div
      className="border border-border rounded-md p-3 bg-card/40 flex flex-col gap-1.5"
      data-testid={`tile-calibrator-ab-${item.league}`}
    >
      <div className="flex items-baseline justify-between">
        <div className="text-xs font-semibold tracking-wider">
          {item.league}
        </div>
        {item.last_promotion_at && (
          <div
            className="text-[9px] uppercase tracking-widest text-success"
            title={`Last promotion: ${fmtTime(item.last_promotion_at)}`}
          >
            promoted
          </div>
        )}
      </div>
      <div className="grid grid-cols-2 gap-x-2 text-[10px]">
        <div className="border border-border/60 rounded px-2 py-1.5 bg-success/5">
          <div className="text-[9px] uppercase tracking-widest text-muted-foreground">
            champion
          </div>
          <div className="text-foreground">
            n: {ch?.sample_count ?? "—"}
          </div>
          <div className="text-muted-foreground">
            in: <span className="text-foreground">{fmt(ch?.brier_in_sample)}</span>
          </div>
          <div className="text-muted-foreground">
            oos: <span className="text-foreground">{fmt(ch?.brier_out_of_sample)}</span>
          </div>
        </div>
        <div className="border border-border/60 rounded px-2 py-1.5 bg-primary/5">
          <div className="text-[9px] uppercase tracking-widest text-muted-foreground">
            challenger
          </div>
          <div className="text-foreground">
            n: {cl?.sample_count ?? "—"}
          </div>
          <div className="text-muted-foreground">
            in: <span className="text-foreground">{fmt(cl?.brier_in_sample)}</span>
          </div>
          <div className="text-muted-foreground">
            oos: <span className="text-foreground">{fmt(cl?.brier_out_of_sample)}</span>
          </div>
        </div>
      </div>
      <div className={`text-[10px] ${deltaTone(ch?.brier_out_of_sample ?? null, cl?.brier_out_of_sample ?? null)}`}>
        Δ oos: {cmpDelta == null ? "—" : (cmpDelta >= 0 ? "+" : "") + cmpDelta.toFixed(4)}
      </div>
      <div className="flex items-center gap-1 pt-1 border-t border-border/40">
        <span className="text-[9px] text-muted-foreground mr-1">history:</span>
        {item.snapshots.length === 0 ? (
          <span className="text-[10px] text-muted-foreground">none</span>
        ) : (
          item.snapshots.map((s) => (
            <span key={s.id} title={`${s.role} · ${fmtTime(s.fit_at)} · oos ${fmt(s.brier_out_of_sample)}`}>
              <RoleDot role={s.role} />
            </span>
          ))
        )}
      </div>
    </div>
  );
}

export default function CalibratorAb() {
  const q = useQuery({
    queryKey: ["dashboard-calibrator-ab"],
    queryFn: async () =>
      customFetch<AbResponse>("/api/dashboard/calibration/ab"),
    staleTime: 60_000,
    refetchInterval: 120_000,
  });

  const items = useMemo(() => q.data?.items ?? [], [q.data]);
  const championCount =
    q.data?.champion_count ?? items.filter((i) => i.champion != null).length;
  const challengerCount =
    q.data?.challenger_count ?? items.filter((i) => i.challenger != null).length;

  return (
    <GlassCard tone="solid" padding="md" data-testid="card-calibrator-ab">
      <SectionHeader
        eyebrow="A/B"
        title="Champion vs challenger"
        subtitle={
          q.data?.error
            ? `Failed to load: ${q.data.error}`
            : `${championCount} champions · ${challengerCount} challengers · auto-promoted after 2 consecutive OOS-Brier wins.`
        }
      />
      {q.isLoading && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          Loading A/B history…
        </div>
      )}
      {q.isError && (
        <div className="text-xs text-destructive py-6 text-center">
          Failed to load: {String((q.error as Error)?.message ?? q.error)}
        </div>
      )}
      {!q.isLoading && !q.isError && items.length === 0 && (
        <div className="text-xs text-muted-foreground py-6 text-center">
          No A/B history yet — the next refit cycle will populate this view.
        </div>
      )}
      {!q.isLoading && !q.isError && items.length > 0 && (
        <div
          className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3"
          data-testid="grid-calibrator-ab"
        >
          {items.map((it) => (
            <LeagueRow key={it.league} item={it} />
          ))}
        </div>
      )}
    </GlassCard>
  );
}
