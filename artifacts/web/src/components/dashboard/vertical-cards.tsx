import { useMemo } from "react";
import { Link } from "wouter";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import {
  getGetMyAccessQueryKey,
  getGetVerticalsOverviewQueryKey,
  useGetMyAccess,
  useGetVerticalsOverview,
  type VerticalHaltMeta,
  type VerticalsOverviewEntry,
} from "@workspace/api-client-react";
import { apiFetch } from "@/lib/api";
import { GlassCard } from "@/components/ui/glass-card";
import { SectionHeader } from "@/components/ui/section-header";
import { useVerticalFilter } from "@/hooks/use-selected-verticals";
import { usePeriod } from "./period-context";
import { usePlacedBy } from "./placed-by-context";

type PersonaOverrideSummary = {
  disabledByVertical: Record<string, string[]>;
  totalVerticalsWithOverrides: number;
};

type PersonaTogglesPayload = {
  items: Array<{
    vertical: string;
    vertical_label?: string;
    persona_profile?: string | null;
    personas: Array<{ id: string; display?: string; enabled: boolean }>;
    orphaned?: boolean;
  }>;
};

function usePersonaOverrideSummary(enabled: boolean): PersonaOverrideSummary | null {
  const { getToken } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const { data } = useQuery<PersonaTogglesPayload>({
    queryKey: ["admin", "persona-toggles", "summary"],
    queryFn: () =>
      apiFetch<PersonaTogglesPayload>("/api/admin/persona-toggles", {
        getToken: tokenFn,
      }),
    enabled,
    staleTime: 60_000,
    refetchInterval: 120_000,
  });
  return useMemo(() => {
    if (!data) return null;
    const disabledByVertical: Record<string, string[]> = {};
    for (const v of data.items) {
      const off = v.personas.filter((p) => !p.enabled).map((p) => p.display || p.id);
      if (off.length > 0) disabledByVertical[v.vertical] = off;
    }
    return {
      disabledByVertical,
      totalVerticalsWithOverrides: Object.keys(disabledByVertical).length,
    };
  }, [data]);
}

function fmtCurrency(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function pnlClass(n: number | null | undefined): string {
  if (n === null || n === undefined) return "text-foreground";
  if (n < 0) return "text-loss";
  if (n > 0) return "text-win";
  return "text-foreground";
}

function fmtRelativePast(iso: string | null | undefined): string {
  if (!iso) return "unknown time";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return iso;
  const delta = Math.max(0, Date.now() - t);
  const sec = Math.round(delta / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 48) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}

function buildHaltTooltip(meta: VerticalHaltMeta): string {
  const who = meta.by_email || meta.by_user_id || "an admin";
  const when = meta.at ? `${fmtRelativePast(meta.at)} (${meta.at})` : "an unknown time";
  const reason = meta.reason ? ` — “${meta.reason}”` : "";
  const sourceLabel =
    meta.source === "scheduled_rule"
      ? " · scheduled rule"
      : meta.source === "manual_admin"
        ? ""
        : meta.source
          ? ` · ${meta.source}`
          : "";
  const ttl = meta.expires_at
    ? ` · auto-resumes at ${meta.expires_at}`
    : "";
  return `Halted by ${who} ${when}${reason}${sourceLabel}${ttl}`;
}

function VerticalCard({
  entry,
  disabledPersonas,
}: {
  entry: VerticalsOverviewEntry;
  disabledPersonas?: string[];
}) {
  const isPnlPos = entry.pnl > 0;
  const isPnlNeg = entry.pnl < 0;
  const slug = entry.vertical;
  const haltMeta = entry.halt ?? null;
  const isHalted = !!entry.halted;

  return (
    <Link
      href={`/verticals?selected=${encodeURIComponent(slug)}`}
      data-testid={`card-vertical-overview-${slug}`}
      className="block focus:outline-none"
    >
      <GlassCard
        tone="solid"
        padding="sm"
        className={`flex h-full flex-col gap-2 transition-colors hover:[border-color:hsl(var(--primary)/0.5)] ${
          entry.pinned ? "[border-color:hsl(var(--primary)/0.4)]" : ""
        } ${entry.hidden ? "opacity-60" : ""} ${
          isHalted ? "[border-color:hsl(var(--destructive)/0.6)]" : ""
        }`}
      >
        <div className="flex items-baseline justify-between gap-2">
          <span className="truncate text-xs font-bold uppercase tracking-widest text-primary">
            {slug}
          </span>
          <div className="flex shrink-0 items-center gap-1 text-[9px] uppercase tracking-widest">
            {isHalted ? (
              <span
                className="border border-destructive bg-destructive/10 px-1 font-bold text-destructive"
                title={haltMeta ? buildHaltTooltip(haltMeta) : "Halted"}
                data-testid={`badge-vertical-halted-${slug}`}
              >
                HALTED
              </span>
            ) : null}
            {entry.pinned ? (
              <span className="border border-primary/60 px-1 text-primary">
                PIN
              </span>
            ) : null}
            {entry.hidden ? (
              <span className="border border-border px-1 text-muted-foreground">
                HID
              </span>
            ) : null}
            {!entry.enabled ? (
              <span className="border border-loss/60 px-1 text-loss">OFF</span>
            ) : null}
            {disabledPersonas && disabledPersonas.length > 0 ? (
              <span
                className="border border-accent-warning/60 px-1 text-accent-warning"
                title={`Personas disabled: ${disabledPersonas.join(", ")}`}
                data-testid={`badge-persona-override-${slug}`}
              >
                {disabledPersonas.length} OFF
              </span>
            ) : null}
          </div>
        </div>

        <div
          className={`text-xl font-bold tabular-nums ${
            isPnlNeg ? "text-loss" : isPnlPos ? "text-win" : "text-foreground"
          }`}
          data-testid={`text-vertical-pnl-${slug}`}
        >
          {fmtCurrency(entry.pnl)}
        </div>

        <div className="grid grid-cols-3 gap-2 text-[10px]">
          <div>
            <div className="text-muted-foreground/70 uppercase tracking-widest">
              WIN
            </div>
            <div className="font-bold tabular-nums">
              {fmtPct(entry.win_rate ?? null, 0)}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground/70 uppercase tracking-widest">
              N
            </div>
            <div className="font-bold tabular-nums">{entry.trades}</div>
          </div>
          <div>
            <div className="text-muted-foreground/70 uppercase tracking-widest">
              SHARPE
            </div>
            <div className={`font-bold tabular-nums ${pnlClass(entry.sharpe ?? null)}`}>
              {fmtNum(entry.sharpe ?? null)}
            </div>
          </div>
        </div>

        <div className="mt-auto flex items-center justify-between text-[9px] uppercase tracking-widest text-muted-foreground/70">
          <span>{entry.open_positions} open</span>
          {entry.brier !== null && entry.brier !== undefined ? (
            <span>brier {fmtNum(entry.brier, 3)}</span>
          ) : null}
        </div>
      </GlassCard>
    </Link>
  );
}

export default function VerticalCards() {
  const { period, days } = usePeriod();
  // Trade-provenance filter (T004).  Vertical cards aggregate the same
  // trades the KPI strip and per-vertical performance panel use, so
  // every tile must honour the toggle to stay numerically consistent.
  const { placedBy } = usePlacedBy();
  const params = { days, placed_by: placedBy };
  const { data, isLoading } = useGetVerticalsOverview(params, {
    query: {
      queryKey: getGetVerticalsOverviewQueryKey(params),
      refetchInterval: 60_000,
      staleTime: 30_000,
    },
  });
  const { matches } = useVerticalFilter();
  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 60_000 },
  });
  const isAdmin = !!access?.is_admin;
  const personaSummary = usePersonaOverrideSummary(isAdmin);

  const all = data?.verticals ?? [];
  const visible = all
    .filter((v) => matches(v.vertical))
    .sort((a, b) => {
      // Pinned first, then by absolute pnl desc.
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      return Math.abs(b.pnl) - Math.abs(a.pnl);
    });

  const overrideCount = personaSummary?.totalVerticalsWithOverrides ?? 0;
  const haltedEntries = useMemo(
    () => all.filter((v) => v.halted),
    [all],
  );

  return (
    <div data-testid="vertical-cards">
      <SectionHeader
        eyebrow={`VERTICALS · ${period.toUpperCase()}`}
        title="Per-vertical performance"
        subtitle="Click a card to drill into intelligence + calibration."
      />
      {haltedEntries.length > 0 ? (
        <div
          className="mb-3 flex flex-wrap items-center gap-2 border border-destructive/50 bg-destructive/5 px-3 py-2 text-[11px]"
          data-testid="halted-verticals-banner"
        >
          <span className="font-bold uppercase tracking-widest text-destructive">
            Halted
          </span>
          <span className="text-muted-foreground normal-case tracking-normal">
            {haltedEntries.length === 1
              ? "1 vertical is currently sat out — risk gates will reject its candidates next cycle."
              : `${haltedEntries.length} verticals are currently sat out — risk gates will reject their candidates next cycle.`}
          </span>
          {haltedEntries.map((v) => (
            <span
              key={v.vertical}
              title={v.halt ? buildHaltTooltip(v.halt) : "Halted"}
              className="border border-destructive/60 bg-destructive/10 px-1.5 py-0.5 font-bold uppercase tracking-widest text-destructive"
              data-testid={`halted-banner-chip-${v.vertical}`}
            >
              {v.vertical}
            </span>
          ))}
          {isAdmin ? (
            <Link
              href="/system?tab=controls"
              className="ml-auto border border-destructive/60 px-1.5 py-0.5 uppercase tracking-widest text-destructive hover:bg-destructive/10"
              data-testid="halted-banner-manage"
            >
              Manage
            </Link>
          ) : null}
        </div>
      ) : null}
      {isAdmin && overrideCount > 0 ? (
        <div
          className="mb-3 flex flex-wrap items-center gap-2 border border-accent-warning/40 bg-accent-warning/5 px-3 py-2 text-[11px]"
          data-testid="persona-override-summary"
        >
          <span className="font-bold uppercase tracking-widest text-accent-warning">
            Persona overrides
          </span>
          <span className="text-muted-foreground normal-case tracking-normal">
            {overrideCount} {overrideCount === 1 ? "vertical is" : "verticals are"}{" "}
            running with non-default personas:
          </span>
          {Object.entries(personaSummary?.disabledByVertical ?? {}).map(
            ([v, disabled]) => (
              <Link
                key={v}
                href="/system?tab=risk"
                title={`Disabled: ${disabled.join(", ")}`}
                className="border border-accent-warning/60 px-1.5 py-0.5 uppercase tracking-widest text-accent-warning hover:bg-accent-warning/10"
                data-testid={`persona-override-chip-${v}`}
              >
                {v} · {disabled.length} OFF
              </Link>
            ),
          )}
        </div>
      ) : null}
      {isLoading && all.length === 0 ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <GlassCard
              key={i}
              tone="solid"
              padding="sm"
              className="h-28 animate-pulse"
            />
          ))}
        </div>
      ) : visible.length === 0 ? (
        <GlassCard
          tone="solid"
          padding="sm"
          className="text-center text-xs uppercase tracking-widest text-muted-foreground"
        >
          NO_VERTICALS_VISIBLE
        </GlassCard>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {visible.map((v) => (
            <VerticalCard
              key={v.vertical}
              entry={v}
              disabledPersonas={
                isAdmin
                  ? personaSummary?.disabledByVertical[v.vertical]
                  : undefined
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}
