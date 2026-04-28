import { useEffect, useMemo, useState } from "react";
import { Link } from "wouter";
import { useAuth } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import {
  getGetMyAccessQueryKey,
  useGetMyAccess,
} from "@workspace/api-client-react";
import { apiFetch } from "@/lib/api";
import { useFocusFromQuery } from "@/hooks/use-focus-from-query";
import { GlassCard } from "@/components/ui/glass-card";
import { TabBar, useTabState, type TabDef } from "@/components/shell/tab-bar";
import { HealthPanel } from "@/pages/health";
import { NotificationPrefsPanel } from "@/pages/settings-notifications";
import {
  AuditLogPanel,
  CryptoAccuracyPanel,
  CryptoBacktestPanel,
  EloUpdaterPanel,
  GatekeeperModelPanel,
  GithubMirrorPanel,
  LeadsPanel,
  ModelsPanel,
  PersonasPanel,
  RcWebhookPanel,
  RiskKnobsPanel,
  RuntimePanel,
  TechAiPromotionPanel,
  TriggerCyclePanel,
  UserManagementPanel,
  VerticalHaltsPanel,
  VerticalVisibilityPanel,
} from "@/pages/admin";
import LiveExecuteToggle from "@/components/telemetry/live-execute-toggle";
import EmergencyStop from "@/components/telemetry/emergency-stop";
import AccessRequests from "@/components/telemetry/access-requests";
import AutoResearchPanel from "@/components/intelligence/autoresearch-panel";

const ALL_TABS = ["health", "controls", "risk", "reconciler", "settings", "research", "audit", "users", "leads"] as const;
type TabKey = (typeof ALL_TABS)[number];

const TAB_DEFS: Record<TabKey, TabDef<TabKey> & { adminOnly: boolean; subtitle: string }> = {
  health: {
    key: "health",
    label: "Health",
    adminOnly: false,
    subtitle: "Live probes · model drift · recent errors · alert routing.",
  },
  controls: {
    key: "controls",
    label: "Controls",
    adminOnly: true,
    subtitle: "Live execute · emergency stop · runtime mode · vertical visibility · cycles.",
  },
  risk: {
    key: "risk",
    label: "Risk",
    adminOnly: true,
    subtitle: "Edge / Kelly / agreement / confidence-band gating thresholds.",
  },
  reconciler: {
    key: "reconciler",
    label: "Reconciler",
    adminOnly: true,
    subtitle: "Position-drift queue · accept-truth, snooze, or audit auto-skeletons.",
  },
  settings: {
    key: "settings",
    label: "Settings",
    adminOnly: false,
    subtitle: "Per-channel notification preferences (email · push · SMS · Telegram).",
  },
  research: {
    key: "research",
    label: "Research",
    adminOnly: true,
    subtitle: "AutoResearch candidate queue — promote, reject, or reset discovered signals.",
  },
  audit: {
    key: "audit",
    label: "Audit",
    adminOnly: true,
    subtitle: "Operator action log — last 100 entries, refreshes every 30s.",
  },
  users: {
    key: "users",
    label: "Users",
    adminOnly: true,
    subtitle: "Approve, promote, and inspect operator accounts.",
  },
  leads: {
    key: "leads",
    label: "Leads",
    adminOnly: true,
    subtitle: "Early-access landing-page sign-ups · filter by interest / date · CSV export.",
  },
};

export default function SystemPage() {
  useFocusFromQuery();

  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });
  const isAdmin = !!access?.is_admin;

  const visibleTabs = useMemo<TabKey[]>(
    () =>
      ALL_TABS.filter((k) => (TAB_DEFS[k].adminOnly ? isAdmin : true)),
    [isAdmin],
  );

  const [tab, setTab] = useTabState<TabKey>(visibleTabs, "health", "/system");

  const subtitle = TAB_DEFS[tab]?.subtitle ?? "";

  const tabBarItems: TabDef<TabKey>[] = visibleTabs.map((k) => ({
    key: k,
    label: TAB_DEFS[k].label,
  }));

  return (
    <div className="space-y-5 font-mono" data-testid="page-system">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              SYSTEM
            </div>
            <h1 className="text-xl font-bold uppercase tracking-tight text-foreground lg:text-2xl">
              Health · Controls · Account
            </h1>
            <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>
          </div>
          {!isAdmin && (
            <span
              className="border border-border bg-card px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground"
              data-testid="badge-non-admin"
            >
              Operator console — admin tabs hidden
            </span>
          )}
        </div>
        <TabBar
          tabs={tabBarItems}
          active={tab}
          onSelect={setTab}
          ariaLabel="System view"
          testIdPrefix="system"
        />
      </header>

      {tab === "health" && (
        <div data-testid="panel-system-health">
          <HealthPanel />
        </div>
      )}

      {tab === "controls" && isAdmin && (
        <div className="space-y-5" data-testid="panel-system-controls">
          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Kill switches
            </div>
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              <LiveExecuteToggle />
              <EmergencyStop />
            </div>
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Daily-loss cap
            </div>
            <DailyLossPanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Runtime mode
            </div>
            <RuntimePanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Gatekeeper model
            </div>
            <GatekeeperModelPanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Vertical halts
            </div>
            <VerticalHaltsPanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Vertical visibility
            </div>
            <VerticalVisibilityPanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Trigger cycle
            </div>
            <TriggerCyclePanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md" data-focus-id="elo-updater">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Team rating updater
            </div>
            <EloUpdaterPanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Models
            </div>
            <ModelsPanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Crypto-binary backtest
            </div>
            <CryptoBacktestPanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Realised crypto accuracy
            </div>
            <CryptoAccuracyPanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              GitHub recovery mirror
            </div>
            <GithubMirrorPanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Pending access requests
            </div>
            <AccessRequests />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Mobile IAP webhook
            </div>
            <RcWebhookPanel />
          </GlassCard>
        </div>
      )}

      {tab === "risk" && isAdmin && (
        <div className="space-y-5" data-testid="panel-system-risk">
          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Gating thresholds
            </div>
            <RiskKnobsPanel />
          </GlassCard>
          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Personas
            </div>
            <p className="mb-3 text-[11px] text-muted-foreground">
              Per-persona enable / weight controls — paired with the risk
              knobs above so you can tune the swarm and gating together.
            </p>
            <PersonasPanel />
          </GlassCard>
          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Tech-AI Kelly promotion
            </div>
            <TechAiPromotionPanel />
          </GlassCard>
        </div>
      )}

      {tab === "reconciler" && isAdmin && (
        <div data-testid="panel-system-reconciler">
          <GlassCard tone="solid" padding="md">
            <ReconcilerDriftsPanel />
          </GlassCard>
        </div>
      )}

      {tab === "settings" && (
        <div className="space-y-5" data-testid="panel-system-settings">
          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Appearance
            </div>
            <ThemePickerPanel />
          </GlassCard>
          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Notifications
            </div>
            <NotificationPrefsPanel />
          </GlassCard>
        </div>
      )}

      {tab === "research" && isAdmin && (
        <div data-testid="panel-system-research">
          <AutoResearchPanel isAdmin={isAdmin} />
        </div>
      )}

      {tab === "audit" && isAdmin && (
        <div data-testid="panel-system-audit">
          <GlassCard tone="solid" padding="md">
            <AuditLogPanel />
          </GlassCard>
        </div>
      )}

      {tab === "users" && isAdmin && (
        <div data-testid="panel-system-users">
          <GlassCard tone="solid" padding="md">
            <UserManagementPanel />
          </GlassCard>
        </div>
      )}

      {tab === "leads" && isAdmin && (
        <div data-testid="panel-system-leads">
          <GlassCard tone="solid" padding="md">
            <LeadsPanel />
          </GlassCard>
        </div>
      )}

      {/* Defensive: if URL forces an admin tab on a non-admin (e.g. shared
          link), show a polite explainer instead of a blank page. */}
      {!isAdmin && TAB_DEFS[tab]?.adminOnly && (
        <GlassCard tone="solid" padding="md" data-testid="panel-system-forbidden">
          <p className="text-xs text-muted-foreground">
            This tab is restricted to admin operators. Ask an existing admin to
            promote your account from{" "}
            <Link
              href="/system?tab=users"
              className="text-accent-cyan underline hover:opacity-80"
            >
              the Users panel
            </Link>
            .
          </p>
        </GlassCard>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Theme picker — mirrors the dark/light toggle in the shell header so the
// preference is also discoverable from System → Settings (per the
// "cyberpunk-finance" UI overhaul). Uses the same localStorage key
// (`axiom.theme`) and CSS class (`light-theme` on <html>) as the header
// toggle so flipping either one stays in sync.
// ---------------------------------------------------------------------------

const THEME_KEY = "axiom.theme";

function applyTheme(mode: "dark" | "light") {
  if (typeof document === "undefined") return;
  const html = document.documentElement;
  if (mode === "light") html.classList.add("light-theme");
  else html.classList.remove("light-theme");
}

function loadTheme(): "dark" | "light" {
  if (typeof window === "undefined") return "dark";
  const stored = window.localStorage.getItem(THEME_KEY);
  return stored === "light" ? "light" : "dark";
}

const THEME_EVENT = "axiom:theme-changed";

function ThemePickerPanel() {
  const [theme, setThemeState] = useState<"dark" | "light">(() => loadTheme());

  // Cross-component sync: listen for storage events (other tabs) AND a
  // custom in-window event dispatched by the shell header toggle so
  // flipping the theme from either control updates the other instantly.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onStorage = (e: StorageEvent) => {
      if (e.key === THEME_KEY) setThemeState(loadTheme());
    };
    const onLocal = (e: Event) => {
      const detail = (e as CustomEvent<"dark" | "light">).detail;
      if (detail === "dark" || detail === "light") setThemeState(detail);
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener(THEME_EVENT, onLocal as EventListener);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(THEME_EVENT, onLocal as EventListener);
    };
  }, []);

  const setTheme = (next: "dark" | "light") => {
    setThemeState(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(THEME_KEY, next);
      window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: next }));
    }
    applyTheme(next);
  };

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const options: Array<{ value: "dark" | "light"; label: string; hint: string }> = [
    { value: "dark", label: "Dark", hint: "Default cyberpunk-finance palette." },
    { value: "light", label: "Light", hint: "High-contrast daytime palette." },
  ];

  return (
    <div className="space-y-3" data-testid="panel-theme-picker">
      <p className="text-[11px] text-muted-foreground">
        Theme preference is saved to this browser only.
      </p>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {options.map((opt) => {
          const active = theme === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => setTheme(opt.value)}
              className={`flex flex-col items-start gap-1 border px-3 py-2 text-left text-xs uppercase tracking-widest transition ${
                active
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border bg-card text-muted-foreground hover:border-primary hover:text-foreground"
              }`}
              aria-pressed={active}
              data-testid={`button-theme-${opt.value}`}
            >
              <span className="text-[11px] font-bold">{opt.label}</span>
              <span className="text-[10px] normal-case tracking-normal text-muted-foreground">
                {opt.hint}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

type DriftRow = {
  id: number;
  ticker: string;
  vertical: string | null;
  local_qty: number;
  exchange_qty: number;
  drift_qty: number;
  state: string;
  resolver_email: string | null;
  resolution_reason: string | null;
  snoozed_until: string | null;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at: string | null;
  created_at: string;
};

type DriftListResponse = {
  items: DriftRow[];
  state_filter: string;
  resolution_enabled: boolean;
};

function ReconcilerDriftsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const [stateFilter, setStateFilter] = useState<"open" | "snoozed" | "resolved">("open");
  const [busyTicker, setBusyTicker] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const { data, isLoading, error, refetch } = useQuery<DriftListResponse>({
    queryKey: ["admin", "reconciler", "drifts", stateFilter],
    queryFn: () =>
      apiFetch<DriftListResponse>(
        `/api/admin/reconciler/drifts?state_filter=${stateFilter}&limit=200`,
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  const accept = async (ticker: string) => {
    const reason = window.prompt(
      `Accept exchange truth for ${ticker}? Optional reason:`,
      "",
    );
    if (reason === null) return; // cancel
    if (!window.confirm(
      `This will close all local positions on ${ticker} and create a synthetic row matching the exchange. Continue?`,
    )) return;
    setBusyTicker(ticker);
    setActionMsg(null);
    try {
      const res = await apiFetch<Record<string, unknown>>(
        `/api/admin/reconciler/drifts/${encodeURIComponent(ticker)}/accept-exchange-truth`,
        { method: "POST", body: { reason }, getToken: tokenFn },
      );
      setActionMsg(
        res.changed
          ? `Resolved ${ticker} (accepted exchange = ${res.exchange_qty}).`
          : `No active drift for ${ticker}.`,
      );
      await refetch();
    } catch (e) {
      setActionMsg(`Failed: ${(e as Error).message}`);
    } finally {
      setBusyTicker(null);
    }
  };

  const snooze = async (ticker: string) => {
    const hoursStr = window.prompt(
      `Snooze ${ticker} drift alerts for how many hours? (1–720)`,
      "24",
    );
    if (hoursStr === null) return;
    const hours = Math.max(1, Math.min(720, Number(hoursStr) || 24));
    const reason = window.prompt(`Reason for snoozing ${ticker}? (optional)`, "");
    if (reason === null) return;
    setBusyTicker(ticker);
    setActionMsg(null);
    try {
      const res = await apiFetch<Record<string, unknown>>(
        `/api/admin/reconciler/drifts/${encodeURIComponent(ticker)}/snooze`,
        {
          method: "POST",
          body: { snooze_seconds: hours * 3600, reason },
          getToken: tokenFn,
        },
      );
      setActionMsg(
        res.changed
          ? `Snoozed ${ticker} for ${hours}h.`
          : `No active drift for ${ticker}.`,
      );
      await refetch();
    } catch (e) {
      setActionMsg(`Failed: ${(e as Error).message}`);
    } finally {
      setBusyTicker(null);
    }
  };

  if (isLoading) return <div className="h-10 animate-pulse bg-muted/30" />;
  if (error) {
    return (
      <p className="text-xs text-destructive" data-testid="reconciler-error">
        Failed to load drift queue: {(error as Error).message}
      </p>
    );
  }
  const items = data?.items ?? [];
  const flagOff = data && !data.resolution_enabled;
  return (
    <div className="space-y-3" data-testid="reconciler-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
          Drift queue · {items.length} {stateFilter}
        </div>
        <div className="flex gap-1" role="tablist">
          {(["open", "snoozed", "resolved"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStateFilter(s)}
              className={`border px-2 py-1 text-[10px] uppercase tracking-widest transition ${
                stateFilter === s
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border bg-card text-muted-foreground hover:border-primary"
              }`}
              data-testid={`reconciler-filter-${s}`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>
      {flagOff && (
        <p className="border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-300">
          Resolution workflow flag (AXIOM_RECONCILER_RESOLUTION_ENABLED) is off — the
          reconciler is detecting drift but not writing rows here. Manual
          accept-truth / snooze still work for any pre-existing rows.
        </p>
      )}
      {actionMsg && (
        <p className="text-[11px] text-muted-foreground" data-testid="reconciler-action-msg">
          {actionMsg}
        </p>
      )}
      {items.length === 0 ? (
        <p className="text-[11px] text-muted-foreground">
          No {stateFilter} drift rows. The reconciler queue is clean.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[11px]">
            <thead>
              <tr className="border-b border-border text-left text-[10px] uppercase tracking-widest text-muted-foreground">
                <th className="px-2 py-1">Ticker</th>
                <th className="px-2 py-1">Local</th>
                <th className="px-2 py-1">Exch</th>
                <th className="px-2 py-1">Drift</th>
                <th className="px-2 py-1">State</th>
                <th className="px-2 py-1">Last seen</th>
                <th className="px-2 py-1">Resolver</th>
                <th className="px-2 py-1 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <tr
                  key={row.id}
                  className="border-b border-border/40"
                  data-testid={`drift-row-${row.ticker}`}
                >
                  <td className="px-2 py-1 font-bold">{row.ticker}</td>
                  <td className="px-2 py-1">{row.local_qty}</td>
                  <td className="px-2 py-1">{row.exchange_qty}</td>
                  <td className="px-2 py-1">{row.drift_qty}</td>
                  <td className="px-2 py-1 uppercase tracking-widest">{row.state}</td>
                  <td className="px-2 py-1">
                    {new Date(row.last_seen_at).toLocaleString()}
                  </td>
                  <td className="px-2 py-1">{row.resolver_email ?? "—"}</td>
                  <td className="px-2 py-1 text-right">
                    {(row.state === "open" || row.state === "snoozed") && (
                      <span className="space-x-1">
                        <button
                          type="button"
                          onClick={() => accept(row.ticker)}
                          disabled={busyTicker === row.ticker}
                          className="border border-primary px-2 py-0.5 text-[10px] uppercase tracking-widest text-primary hover:bg-primary/10 disabled:opacity-50"
                          data-testid={`button-accept-${row.ticker}`}
                        >
                          Accept truth
                        </button>
                        <button
                          type="button"
                          onClick={() => snooze(row.ticker)}
                          disabled={busyTicker === row.ticker}
                          className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground hover:border-primary hover:text-foreground disabled:opacity-50"
                          data-testid={`button-snooze-${row.ticker}`}
                        >
                          Snooze
                        </button>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

type DailyLossSnapshot = {
  date_local: string;
  timezone: string;
  realized_pnl_usd: number;
  unrealized_pnl_usd: number;
  net_pnl_usd: number;
  start_of_day_bankroll_usd: number | null;
  threshold_usd: number | null;
  threshold_pct: number | null;
  threshold_pct_usd: number | null;
  loss_usd: number;
  tripped: boolean;
  tripped_reason: string | null;
  used_pct_abs: number | null;
  used_pct_pct: number | null;
  used_pct_max: number | null;
  configured: boolean;
};

function fmtUsd(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function DailyLossPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const { data, isLoading, error } = useQuery<DailyLossSnapshot>({
    queryKey: ["system", "daily-loss"],
    queryFn: () =>
      apiFetch<DailyLossSnapshot>("/api/risk/daily-loss", {
        getToken: tokenFn,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  if (isLoading) {
    return <div className="h-10 animate-pulse bg-muted/30" />;
  }
  if (error || !data) {
    return (
      <div className="text-loss text-xs" data-testid="daily-loss-error">
        Failed to load daily-loss snapshot.
      </div>
    );
  }

  const usedPct = data.used_pct_max ?? 0;
  const barWidth = Math.max(0, Math.min(100, usedPct));
  const barTone = data.tripped
    ? "bg-loss"
    : usedPct >= 75
      ? "bg-accent-warning"
      : "bg-accent-cyan";

  const stateLabel = !data.configured
    ? "UNCONFIGURED"
    : data.tripped
      ? "TRIPPED"
      : "ARMED";
  const stateClass = !data.configured
    ? "border-border text-muted-foreground"
    : data.tripped
      ? "border-loss/60 bg-loss/10 text-loss"
      : "border-accent-cyan/40 text-accent-cyan";

  return (
    <div className="space-y-3" data-testid="panel-daily-loss">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] text-muted-foreground leading-snug">
          Auto-halts trading when today&apos;s realized + unrealized P&amp;L
          breaches the absolute dollar or % of start-of-day bankroll cap.
          Tripping flips <code>risk.trading_mode</code> to <code>halt</code>;
          an operator re-enables from the runtime mode panel.
        </p>
        <span
          className={`text-[10px] uppercase border px-2 py-0.5 ${stateClass}`}
          data-testid="daily-loss-state"
        >
          {stateLabel}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 text-[11px] md:grid-cols-4">
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Today ({data.timezone})
          </div>
          <div
            className="font-mono text-sm"
            data-testid="daily-loss-date"
          >
            {data.date_local}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Net P&amp;L
          </div>
          <div
            className={`font-mono text-sm ${
              data.net_pnl_usd < 0 ? "text-loss" : "text-accent-cyan"
            }`}
            data-testid="daily-loss-net"
          >
            {fmtUsd(data.net_pnl_usd)}
          </div>
          <div className="text-[10px] text-muted-foreground">
            realized {fmtUsd(data.realized_pnl_usd)} · unrealized{" "}
            {fmtUsd(data.unrealized_pnl_usd)}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Absolute cap
          </div>
          <div
            className="font-mono text-sm"
            data-testid="daily-loss-threshold-abs"
          >
            {data.threshold_usd === null ? "—" : fmtUsd(data.threshold_usd)}
          </div>
          {data.used_pct_abs !== null && (
            <div className="text-[10px] text-muted-foreground">
              used {data.used_pct_abs.toFixed(1)}%
            </div>
          )}
        </div>
        <div>
          <div className="text-muted-foreground uppercase tracking-wider">
            Percent cap
          </div>
          <div
            className="font-mono text-sm"
            data-testid="daily-loss-threshold-pct"
          >
            {data.threshold_pct === null
              ? "—"
              : `${data.threshold_pct.toFixed(2)}%`}
          </div>
          {data.threshold_pct_usd !== null && (
            <div className="text-[10px] text-muted-foreground">
              ≈ {fmtUsd(data.threshold_pct_usd)} · bankroll{" "}
              {fmtUsd(data.start_of_day_bankroll_usd)}
            </div>
          )}
        </div>
      </div>

      {data.configured && (
        <div>
          <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground">
            <span>Loss budget used</span>
            <span data-testid="daily-loss-used-pct">
              {data.used_pct_max === null
                ? "—"
                : `${data.used_pct_max.toFixed(1)}%`}
            </span>
          </div>
          <div className="mt-1 h-1.5 w-full bg-muted/30">
            <div
              className={`h-1.5 ${barTone} transition-[width]`}
              style={{ width: `${barWidth}%` }}
              data-testid="daily-loss-bar"
            />
          </div>
        </div>
      )}

      {data.tripped && data.tripped_reason && (
        <div
          className="border border-loss/60 bg-loss/10 p-2 text-[11px] text-loss"
          data-testid="daily-loss-reason"
        >
          {data.tripped_reason}
        </div>
      )}

      {!data.configured && (
        <div className="text-[11px] text-muted-foreground">
          No thresholds configured — set{" "}
          <code>AXIOM_DAILY_LOSS_HALT_USD</code> and/or{" "}
          <code>AXIOM_DAILY_LOSS_HALT_PCT</code> (or operator overrides{" "}
          <code>risk.daily_loss_halt_usd</code> /{" "}
          <code>risk.daily_loss_halt_pct</code>) to arm the cap.
        </div>
      )}
    </div>
  );
}
