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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { TabBar, useTabState, type TabDef } from "@/components/shell/tab-bar";
import { HealthPanel } from "@/pages/health";
import { NotificationPrefsPanel } from "@/pages/settings-notifications";
import {
  AuditLogPanel,
  CryptoAccuracyPanel,
  CryptoBacktestPanel,
  EloUpdaterPanel,
  GatekeeperModelPanel,
  GatekeeperTuningPanel,
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
  VerticalHaltHistoryPanel,
  VerticalHaltSchedulePanel,
  VerticalHaltsPanel,
  VerticalVisibilityPanel,
} from "@/pages/admin";
import LiveExecuteToggle from "@/components/telemetry/live-execute-toggle";
import EmergencyStop from "@/components/telemetry/emergency-stop";
import AccessRequests from "@/components/telemetry/access-requests";
import AutoResearchPanel from "@/components/intelligence/autoresearch-panel";

const ALL_TABS = ["health", "controls", "risk", "swarm-tuning", "reconciler", "stuck-orders", "settings", "research", "audit", "users", "leads"] as const;
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
  "swarm-tuning": {
    key: "swarm-tuning",
    label: "Swarm tuning",
    adminOnly: true,
    subtitle: "Per-vertical gatekeeper-knob overrides · effective values · audit history.",
  },
  reconciler: {
    key: "reconciler",
    label: "Reconciler",
    adminOnly: true,
    subtitle: "Position-drift queue · accept-truth, snooze, or audit auto-skeletons.",
  },
  "stuck-orders": {
    key: "stuck-orders",
    label: "Stuck orders",
    adminOnly: true,
    subtitle: "Order-submit dead-letter queue · mark-cancelled, force-retry, or claim-filled.",
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
              Vertical halt schedules
            </div>
            <VerticalHaltSchedulePanel />
          </GlassCard>

          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Vertical halt history
            </div>
            <VerticalHaltHistoryPanel />
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

      {tab === "swarm-tuning" && isAdmin && (
        <div data-testid="panel-system-swarm-tuning">
          <GlassCard tone="solid" padding="md">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
              Per-vertical gatekeeper tuning
            </div>
            <GatekeeperTuningPanel />
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

      {tab === "stuck-orders" && isAdmin && (
        <div data-testid="panel-system-stuck-orders">
          <GlassCard tone="solid" padding="md">
            <StuckOrdersPanel />
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

// The drift queue serves both Kalshi and Polymarket; the operator
// picks one venue at a time so the table stays focused on a single
// reconciler partition (task #715).
const RECONCILER_EXCHANGES = ["kalshi", "polymarket"] as const;
type ReconcilerExchange = (typeof RECONCILER_EXCHANGES)[number];

type DriftRow = {
  id: number;
  exchange: string;
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
  // Server-enriched fields (LEFT JOIN markets / model_health_alerts):
  market_title: string | null;
  age_seconds: number | null;
  last_alert_at: string | null;
  last_alert_severity: string | null;
  last_alert_message: string | null;
};

type DriftListResponse = {
  items: DriftRow[];
  state: string;
  exchange: string;
  resolution_enabled: boolean;
};

function formatAge(seconds: number | null): string {
  if (seconds == null || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

// Modal state for the operator confirm flow. Both actions reuse the
// same dialog so the operator always sees the local-vs-exchange diff
// before submitting — this is a real-money path, browser prompts are
// not enough.
type PendingAction =
  | { kind: "accept"; row: DriftRow }
  | { kind: "snooze"; row: DriftRow }
  | null;

function ReconcilerDriftsPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const [stateFilter, setStateFilter] = useState<"open" | "snoozed" | "resolved">("open");
  const [exchangeFilter, setExchangeFilter] =
    useState<ReconcilerExchange>("kalshi");
  const [busyTicker, setBusyTicker] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingAction>(null);
  // Form fields live in panel state so they survive re-renders while
  // the modal is open and reset cleanly when it closes.
  const [reasonInput, setReasonInput] = useState<string>("");
  const [snoozeHours, setSnoozeHours] = useState<number>(24);

  const openAccept = (row: DriftRow) => {
    setReasonInput("");
    setPending({ kind: "accept", row });
  };
  const openSnooze = (row: DriftRow) => {
    setReasonInput("");
    setSnoozeHours(24);
    setPending({ kind: "snooze", row });
  };
  const closeModal = () => {
    setPending(null);
    setReasonInput("");
    setSnoozeHours(24);
  };

  const { data, isLoading, error, refetch } = useQuery<DriftListResponse>({
    queryKey: ["admin", "reconciler", "drifts", stateFilter, exchangeFilter],
    queryFn: () =>
      apiFetch<DriftListResponse>(
        `/api/admin/reconciler/drifts?state=${stateFilter}&limit=200&exchange=${exchangeFilter}`,
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  const submitAccept = async (ticker: string, reason: string) => {
    setBusyTicker(ticker);
    setActionMsg(null);
    try {
      const res = await apiFetch<Record<string, unknown>>(
        `/api/admin/reconciler/drifts/${encodeURIComponent(ticker)}/accept-exchange-truth?exchange=${exchangeFilter}`,
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
      closeModal();
    }
  };

  const submitSnooze = async (ticker: string, hours: number, reason: string) => {
    // Clamp to API bounds (server will also reject) so a fat-finger
    // doesn't bounce off the wire.
    const safeHours = Math.max(1, Math.min(720, Number(hours) || 24));
    const until = new Date(Date.now() + safeHours * 3600 * 1000).toISOString();
    setBusyTicker(ticker);
    setActionMsg(null);
    try {
      const res = await apiFetch<Record<string, unknown>>(
        `/api/admin/reconciler/drifts/${encodeURIComponent(ticker)}/snooze?exchange=${exchangeFilter}`,
        { method: "POST", body: { until, reason }, getToken: tokenFn },
      );
      setActionMsg(
        res.changed
          ? `Snoozed ${ticker} until ${new Date(until).toLocaleString()}.`
          : `No active drift for ${ticker} (${res.reason ?? "unknown"}).`,
      );
      await refetch();
    } catch (e) {
      setActionMsg(`Failed: ${(e as Error).message}`);
    } finally {
      setBusyTicker(null);
      closeModal();
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
          Drift queue · {items.length} {stateFilter} · {exchangeFilter}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex gap-1" role="tablist" aria-label="Exchange">
            {RECONCILER_EXCHANGES.map((x) => (
              <button
                key={x}
                type="button"
                onClick={() => setExchangeFilter(x)}
                className={`border px-2 py-1 text-[10px] uppercase tracking-widest transition ${
                  exchangeFilter === x
                    ? "border-accent-cyan bg-accent-cyan/10 text-accent-cyan"
                    : "border-border bg-card text-muted-foreground hover:border-accent-cyan"
                }`}
                data-testid={`reconciler-exchange-${x}`}
              >
                {x}
              </button>
            ))}
          </div>
          <div className="flex gap-1" role="tablist" aria-label="State">
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
                <th className="px-2 py-1">Title</th>
                <th className="px-2 py-1">Local</th>
                <th className="px-2 py-1">Exch</th>
                <th className="px-2 py-1">Drift</th>
                <th className="px-2 py-1">State</th>
                <th className="px-2 py-1">Age</th>
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
                  <td
                    className="max-w-[260px] truncate px-2 py-1 text-muted-foreground"
                    title={row.market_title ?? ""}
                    data-testid={`drift-title-${row.ticker}`}
                  >
                    {row.market_title ?? "—"}
                  </td>
                  <td className="px-2 py-1">{row.local_qty}</td>
                  <td className="px-2 py-1">{row.exchange_qty}</td>
                  <td className="px-2 py-1">{row.drift_qty}</td>
                  <td className="px-2 py-1 uppercase tracking-widest">{row.state}</td>
                  <td
                    className="px-2 py-1 tabular-nums"
                    data-testid={`drift-age-${row.ticker}`}
                  >
                    {formatAge(row.age_seconds)}
                  </td>
                  <td className="px-2 py-1">
                    {new Date(row.last_seen_at).toLocaleString()}
                  </td>
                  <td className="px-2 py-1">{row.resolver_email ?? "—"}</td>
                  <td className="px-2 py-1 text-right">
                    {(row.state === "open" || row.state === "snoozed") && (
                      <span className="space-x-1">
                        <button
                          type="button"
                          onClick={() => openAccept(row)}
                          disabled={busyTicker === row.ticker}
                          className="border border-primary px-2 py-0.5 text-[10px] uppercase tracking-widest text-primary hover:bg-primary/10 disabled:opacity-50"
                          data-testid={`button-accept-${row.ticker}`}
                        >
                          Accept truth
                        </button>
                        <button
                          type="button"
                          onClick={() => openSnooze(row)}
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
      <ReconcilerActionDialog
        pending={pending}
        reasonInput={reasonInput}
        setReasonInput={setReasonInput}
        snoozeHours={snoozeHours}
        setSnoozeHours={setSnoozeHours}
        busyTicker={busyTicker}
        onCancel={closeModal}
        onConfirmAccept={(ticker, reason) => submitAccept(ticker, reason)}
        onConfirmSnooze={(ticker, hours, reason) =>
          submitSnooze(ticker, hours, reason)
        }
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Confirmation modal — shows the local-vs-exchange diff explicitly so
// the operator can never accept-truth without seeing what they're
// actually committing to. Browser ``window.prompt`` / ``window.confirm``
// don't show structured data and aren't acceptable for a real-money
// path.
// ---------------------------------------------------------------------------

type ReconcilerActionDialogProps = {
  pending: PendingAction;
  reasonInput: string;
  setReasonInput: (v: string) => void;
  snoozeHours: number;
  setSnoozeHours: (v: number) => void;
  busyTicker: string | null;
  onCancel: () => void;
  onConfirmAccept: (ticker: string, reason: string) => void;
  onConfirmSnooze: (ticker: string, hours: number, reason: string) => void;
};

function ReconcilerActionDialog(props: ReconcilerActionDialogProps) {
  const {
    pending,
    reasonInput,
    setReasonInput,
    snoozeHours,
    setSnoozeHours,
    busyTicker,
    onCancel,
    onConfirmAccept,
    onConfirmSnooze,
  } = props;
  const open = pending !== null;
  const row = pending?.row;
  const isAccept = pending?.kind === "accept";
  const busy = !!row && busyTicker === row.ticker;

  // Preview snooze deadline so the operator can sanity-check the
  // hours-from-now math against an absolute clock time before
  // submitting.
  const snoozeUntilLabel = useMemo(() => {
    const safe = Math.max(1, Math.min(720, Number(snoozeHours) || 24));
    return new Date(Date.now() + safe * 3600 * 1000).toLocaleString();
  }, [snoozeHours]);

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o && !busy) onCancel();
      }}
    >
      <DialogContent
        className="max-w-lg"
        data-testid="reconciler-action-dialog"
      >
        {row && (
          <>
            <DialogHeader>
              <DialogTitle>
                {isAccept
                  ? `Accept exchange truth for ${row.ticker}?`
                  : `Snooze drift alerts for ${row.ticker}?`}
              </DialogTitle>
              <DialogDescription>
                {isAccept
                  ? "This closes every open local position on this ticker (status='closed', quantity=0) and inserts a synthetic correction row matching the exchange's reported quantity. The operation runs in a single database transaction — partial failure rolls back."
                  : "This silences alerts for the ticker until the chosen deadline. It does NOT touch the positions table — use Accept truth if the local quantity is genuinely wrong."}
              </DialogDescription>
            </DialogHeader>

            {/* Local vs exchange diff — the whole point of the modal. */}
            <div
              className="grid grid-cols-3 gap-2 border border-border bg-muted/20 p-3 text-[11px]"
              data-testid="reconciler-dialog-diff"
            >
              <div>
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Local
                </div>
                <div className="text-lg font-bold tabular-nums">
                  {row.local_qty}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Exchange
                </div>
                <div className="text-lg font-bold tabular-nums">
                  {row.exchange_qty}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Drift
                </div>
                <div
                  className={`text-lg font-bold tabular-nums ${
                    row.drift_qty === 0
                      ? "text-muted-foreground"
                      : "text-destructive"
                  }`}
                >
                  {row.drift_qty > 0 ? `+${row.drift_qty}` : row.drift_qty}
                </div>
              </div>
              <div className="col-span-3 border-t border-border pt-2 text-[11px] text-muted-foreground">
                <div>
                  <span className="font-semibold text-foreground">Title:</span>{" "}
                  {row.market_title ?? "—"}
                </div>
                <div>
                  <span className="font-semibold text-foreground">Age:</span>{" "}
                  {formatAge(row.age_seconds)} · first seen{" "}
                  {new Date(row.first_seen_at).toLocaleString()}
                </div>
                {row.last_alert_severity && (
                  <div>
                    <span className="font-semibold text-foreground">
                      Last alert:
                    </span>{" "}
                    [{row.last_alert_severity}] {row.last_alert_message}
                  </div>
                )}
              </div>
            </div>

            {!isAccept && (
              <label className="block text-[11px]">
                <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Snooze for (hours, 1–720)
                </span>
                <input
                  type="number"
                  min={1}
                  max={720}
                  step={1}
                  value={snoozeHours}
                  onChange={(e) =>
                    setSnoozeHours(Number(e.target.value) || 24)
                  }
                  disabled={busy}
                  data-testid="reconciler-dialog-hours"
                  className="mt-1 w-full border border-border bg-card px-2 py-1 font-mono"
                />
                <span className="mt-1 block text-[10px] text-muted-foreground">
                  Until: {snoozeUntilLabel}
                </span>
              </label>
            )}

            <label className="block text-[11px]">
              <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Reason {isAccept ? "(optional)" : "(optional)"}
              </span>
              <textarea
                rows={3}
                value={reasonInput}
                onChange={(e) => setReasonInput(e.target.value)}
                placeholder={
                  isAccept
                    ? "e.g. exchange settled, our fill never landed"
                    : "e.g. waiting on Kalshi support ticket #1234"
                }
                disabled={busy}
                data-testid="reconciler-dialog-reason"
                className="mt-1 w-full border border-border bg-card px-2 py-1 font-mono"
              />
            </label>

            <DialogFooter>
              <button
                type="button"
                onClick={onCancel}
                disabled={busy}
                className="border border-border px-3 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:border-primary hover:text-foreground disabled:opacity-50"
                data-testid="reconciler-dialog-cancel"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  if (isAccept) {
                    onConfirmAccept(row.ticker, reasonInput);
                  } else {
                    onConfirmSnooze(row.ticker, snoozeHours, reasonInput);
                  }
                }}
                className={`border px-3 py-1 text-[11px] uppercase tracking-widest disabled:opacity-50 ${
                  isAccept
                    ? "border-destructive bg-destructive/10 text-destructive hover:bg-destructive/20"
                    : "border-primary bg-primary/10 text-primary hover:bg-primary/20"
                }`}
                data-testid="reconciler-dialog-confirm"
              >
                {busy
                  ? "Working…"
                  : isAccept
                    ? "Confirm accept truth"
                    : "Confirm snooze"}
              </button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
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

// ---------------------------------------------------------------------------
// Stuck-orders dead-letter console (task #710). Reads
// ``GET /api/admin/orders/dead-lettered`` and operator-resolves rows
// via ``POST /api/admin/orders/{coid}/resolve``. Mirrors the
// reconciler-drift pattern: a state filter (open / failing / resolved
// / all), a 30s auto-refresh, a confirmation dialog that always shows
// the original order intent (market, side, action, contracts) so the
// operator never picks an action without seeing what they're
// committing to.
// ---------------------------------------------------------------------------

type StuckOrderRow = {
  client_order_id: string;
  market_ticker: string;
  side: string;
  action: string;
  contracts: number;
  limit_price_cents: number | null;
  vertical: string | null;
  recovery_attempts: number;
  last_recovery_at: string | null;
  last_recovery_outcome: string | null;
  last_error: string | null;
  first_failed_at: string;
  dead_lettered_at: string | null;
  resolved_at: string | null;
  resolution_action: string | null;
  resolver_email: string | null;
  resolution_reason: string | null;
  fill_price_cents: number | null;
  age_seconds: number;
};

type StuckOrderListResponse = {
  items: StuckOrderRow[];
  state: string;
  error?: string;
};

type StuckPendingAction =
  | { kind: "mark_cancelled"; row: StuckOrderRow }
  | { kind: "force_retry"; row: StuckOrderRow }
  | { kind: "claim_filled"; row: StuckOrderRow }
  | null;

type StuckStateFilter = "open" | "failing" | "resolved" | "all";

function StuckOrdersPanel() {
  const { getToken, isSignedIn } = useAuth();
  const tokenFn = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const [stateFilter, setStateFilter] = useState<StuckStateFilter>("open");
  const [busyCoid, setBusyCoid] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [pending, setPending] = useState<StuckPendingAction>(null);
  const [reasonInput, setReasonInput] = useState<string>("");
  const [fillPriceCents, setFillPriceCents] = useState<number>(50);

  const openAction = (
    kind: "mark_cancelled" | "force_retry" | "claim_filled",
    row: StuckOrderRow,
  ) => {
    setReasonInput("");
    // Default the claim_filled price to the original limit_price if we
    // have one — operators almost always want to confirm at-or-near
    // their original cap, so save them a typing step but keep them
    // free to override.
    setFillPriceCents(
      row.limit_price_cents != null ? row.limit_price_cents : 50,
    );
    setPending({ kind, row });
  };
  const closeModal = () => {
    setPending(null);
    setReasonInput("");
    setFillPriceCents(50);
  };

  const { data, isLoading, error, refetch } = useQuery<StuckOrderListResponse>({
    queryKey: ["admin", "orders", "dead-lettered", stateFilter],
    queryFn: () =>
      apiFetch<StuckOrderListResponse>(
        `/api/admin/orders/dead-lettered?state=${stateFilter}&limit=200`,
        { getToken: tokenFn },
      ),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
  });

  const submitResolve = async (
    coid: string,
    action: "mark_cancelled" | "force_retry" | "claim_filled",
    reason: string,
    priceCents: number | null,
  ) => {
    setBusyCoid(coid);
    setActionMsg(null);
    try {
      const body: Record<string, unknown> = { action, reason };
      if (action === "claim_filled") {
        body.fill_price_cents = priceCents;
      }
      const res = await apiFetch<Record<string, unknown>>(
        `/api/admin/orders/${encodeURIComponent(coid)}/resolve`,
        { method: "POST", body, getToken: tokenFn },
      );
      const tag =
        action === "mark_cancelled"
          ? "marked cancelled"
          : action === "force_retry"
            ? "cleared for retry"
            : `claimed filled @ ${priceCents}¢`;
      setActionMsg(`Resolved ${coid}: ${tag} (resolver=${res.resolver_email}).`);
      await refetch();
    } catch (e) {
      setActionMsg(`Failed: ${(e as Error).message}`);
    } finally {
      setBusyCoid(null);
      closeModal();
    }
  };

  if (isLoading) return <div className="h-10 animate-pulse bg-muted/30" />;
  if (error) {
    return (
      <p className="text-xs text-destructive" data-testid="stuck-orders-error">
        Failed to load stuck-orders queue: {(error as Error).message}
      </p>
    );
  }
  const items = data?.items ?? [];
  return (
    <div className="space-y-3" data-testid="stuck-orders-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-cyan/80">
          Dead-letter queue · {items.length} {stateFilter}
        </div>
        <div className="flex gap-1" role="tablist">
          {(["open", "failing", "resolved", "all"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStateFilter(s)}
              className={`border px-2 py-1 text-[10px] uppercase tracking-widest transition ${
                stateFilter === s
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-border bg-card text-muted-foreground hover:border-primary"
              }`}
              data-testid={`stuck-orders-filter-${s}`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>
      {data?.error && (
        <p className="border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-300">
          Server reported a load error: {data.error}
        </p>
      )}
      {actionMsg && (
        <p className="text-[11px] text-muted-foreground" data-testid="stuck-orders-action-msg">
          {actionMsg}
        </p>
      )}
      {items.length === 0 ? (
        <p className="text-[11px] text-muted-foreground">
          No {stateFilter} stuck orders. The order-submit recovery queue is clean.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[11px]">
            <thead>
              <tr className="border-b border-border text-left text-[10px] uppercase tracking-widest text-muted-foreground">
                <th className="px-2 py-1">CoID</th>
                <th className="px-2 py-1">Market</th>
                <th className="px-2 py-1">Side</th>
                <th className="px-2 py-1">Act</th>
                <th className="px-2 py-1">Qty</th>
                <th className="px-2 py-1">Limit¢</th>
                <th className="px-2 py-1">Tries</th>
                <th className="px-2 py-1">Last outcome</th>
                <th className="px-2 py-1">Age</th>
                <th className="px-2 py-1">State</th>
                <th className="px-2 py-1 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => {
                const isOpen =
                  row.dead_lettered_at !== null && row.resolved_at === null;
                const isFailing =
                  row.dead_lettered_at === null && row.resolved_at === null;
                const stateTag = row.resolved_at
                  ? `resolved:${row.resolution_action ?? "?"}`
                  : row.dead_lettered_at
                    ? "dead-lettered"
                    : "failing";
                return (
                  <tr
                    key={row.client_order_id}
                    className="border-b border-border/40"
                    data-testid={`stuck-row-${row.client_order_id}`}
                  >
                    <td
                      className="max-w-[200px] truncate px-2 py-1 font-mono"
                      title={row.client_order_id}
                    >
                      {row.client_order_id}
                    </td>
                    <td
                      className="max-w-[200px] truncate px-2 py-1 text-muted-foreground"
                      title={row.market_ticker}
                    >
                      {row.market_ticker}
                    </td>
                    <td className="px-2 py-1 uppercase">{row.side}</td>
                    <td className="px-2 py-1 uppercase">{row.action}</td>
                    <td className="px-2 py-1 tabular-nums">{row.contracts}</td>
                    <td className="px-2 py-1 tabular-nums">
                      {row.limit_price_cents ?? "—"}
                    </td>
                    <td
                      className="px-2 py-1 tabular-nums"
                      data-testid={`stuck-tries-${row.client_order_id}`}
                    >
                      {row.recovery_attempts}
                    </td>
                    <td
                      className="max-w-[180px] truncate px-2 py-1 text-muted-foreground"
                      title={row.last_error ?? row.last_recovery_outcome ?? ""}
                    >
                      {row.last_recovery_outcome ?? "—"}
                    </td>
                    <td
                      className="px-2 py-1 tabular-nums"
                      data-testid={`stuck-age-${row.client_order_id}`}
                    >
                      {formatAge(row.age_seconds)}
                    </td>
                    <td className="px-2 py-1 uppercase tracking-widest">
                      {stateTag}
                    </td>
                    <td className="px-2 py-1 text-right">
                      {(isOpen || isFailing) && (
                        <span className="space-x-1">
                          <button
                            type="button"
                            onClick={() => openAction("mark_cancelled", row)}
                            disabled={busyCoid === row.client_order_id || isFailing}
                            title={
                              isFailing
                                ? "Only force-retry is available before dead-letter promotion"
                                : "Declare cancelled at venue (no fill recorded)"
                            }
                            className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground hover:border-primary hover:text-foreground disabled:opacity-50"
                            data-testid={`button-cancel-${row.client_order_id}`}
                          >
                            Mark cancelled
                          </button>
                          <button
                            type="button"
                            onClick={() => openAction("force_retry", row)}
                            disabled={busyCoid === row.client_order_id}
                            className="border border-primary px-2 py-0.5 text-[10px] uppercase tracking-widest text-primary hover:bg-primary/10 disabled:opacity-50"
                            data-testid={`button-retry-${row.client_order_id}`}
                          >
                            Force retry
                          </button>
                          <button
                            type="button"
                            onClick={() => openAction("claim_filled", row)}
                            disabled={busyCoid === row.client_order_id || isFailing}
                            title={
                              isFailing
                                ? "Only force-retry is available before dead-letter promotion"
                                : "Confirm operator-verified fill"
                            }
                            className="border border-destructive px-2 py-0.5 text-[10px] uppercase tracking-widest text-destructive hover:bg-destructive/10 disabled:opacity-50"
                            data-testid={`button-claim-${row.client_order_id}`}
                          >
                            Claim filled
                          </button>
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <StuckOrderActionDialog
        pending={pending}
        reasonInput={reasonInput}
        setReasonInput={setReasonInput}
        fillPriceCents={fillPriceCents}
        setFillPriceCents={setFillPriceCents}
        busyCoid={busyCoid}
        onCancel={closeModal}
        onConfirm={(coid, action, reason, price) =>
          submitResolve(coid, action, reason, price)
        }
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stuck-orders confirmation modal — always renders the original
// intent (market / side / action / contracts / limit) and the
// per-row recovery counters so the operator can never resolve
// without seeing exactly what they're committing the system to.
// ---------------------------------------------------------------------------

type StuckOrderActionDialogProps = {
  pending: StuckPendingAction;
  reasonInput: string;
  setReasonInput: (v: string) => void;
  fillPriceCents: number;
  setFillPriceCents: (v: number) => void;
  busyCoid: string | null;
  onCancel: () => void;
  onConfirm: (
    coid: string,
    action: "mark_cancelled" | "force_retry" | "claim_filled",
    reason: string,
    fillPriceCents: number | null,
  ) => void;
};

function StuckOrderActionDialog(props: StuckOrderActionDialogProps) {
  const {
    pending,
    reasonInput,
    setReasonInput,
    fillPriceCents,
    setFillPriceCents,
    busyCoid,
    onCancel,
    onConfirm,
  } = props;
  const open = pending !== null;
  const row = pending?.row;
  const kind = pending?.kind;
  const busy = !!row && busyCoid === row.client_order_id;

  const title =
    kind === "mark_cancelled"
      ? `Mark ${row?.client_order_id ?? ""} cancelled?`
      : kind === "force_retry"
        ? `Force retry on ${row?.client_order_id ?? ""}?`
        : `Claim ${row?.client_order_id ?? ""} as filled?`;
  const description =
    kind === "mark_cancelled"
      ? "Declares the order cancelled at the venue. No fill is recorded; no position rollup is touched. Use only when you've confirmed the order will never reach a terminal state at Kalshi."
      : kind === "force_retry"
        ? "Clears the recovery row so the next orchestrator cycle will re-submit a fresh client_order_id for the same intent. Use when the upstream blocker (rate-limit, transient 5xx, etc.) is gone."
        : "Asserts an operator-verified fill at the supplied price. Inserts an order_fills row (source='operator_manual') and updates the positions rollup. This is irreversible from the UI — double-check the price.";
  const validClaim =
    kind !== "claim_filled" ||
    (Number.isFinite(fillPriceCents) &&
      fillPriceCents >= 0 &&
      fillPriceCents <= 100);

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o && !busy) onCancel();
      }}
    >
      <DialogContent
        className="max-w-lg"
        data-testid="stuck-orders-dialog"
      >
        {row && kind && (
          <>
            <DialogHeader>
              <DialogTitle>{title}</DialogTitle>
              <DialogDescription>{description}</DialogDescription>
            </DialogHeader>

            <div
              className="grid grid-cols-2 gap-2 border border-border bg-muted/20 p-3 text-[11px]"
              data-testid="stuck-orders-dialog-summary"
            >
              <div>
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Market
                </div>
                <div className="font-mono">{row.market_ticker}</div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Intent
                </div>
                <div className="font-mono uppercase">
                  {row.action} {row.side} × {row.contracts}
                  {row.limit_price_cents != null
                    ? ` @ ${row.limit_price_cents}¢`
                    : ""}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Recovery attempts
                </div>
                <div className="text-lg font-bold tabular-nums">
                  {row.recovery_attempts}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Age
                </div>
                <div className="text-lg font-bold tabular-nums">
                  {formatAge(row.age_seconds)}
                </div>
              </div>
              <div className="col-span-2 border-t border-border pt-2 text-[11px] text-muted-foreground">
                <div>
                  <span className="font-semibold text-foreground">
                    Last outcome:
                  </span>{" "}
                  {row.last_recovery_outcome ?? "—"}
                </div>
                {row.last_error && (
                  <div className="break-all">
                    <span className="font-semibold text-foreground">
                      Last error:
                    </span>{" "}
                    {row.last_error}
                  </div>
                )}
              </div>
            </div>

            {kind === "claim_filled" && (
              <label className="block text-[11px]">
                <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                  Fill price (cents, 0–100)
                </span>
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  value={fillPriceCents}
                  onChange={(e) =>
                    setFillPriceCents(Number(e.target.value))
                  }
                  disabled={busy}
                  data-testid="stuck-orders-dialog-price"
                  className="mt-1 w-full border border-border bg-card px-2 py-1 font-mono"
                />
                {!validClaim && (
                  <span className="mt-1 block text-[10px] text-destructive">
                    Price must be 0–100 (Kalshi yes/no markets are 1¢
                    increments).
                  </span>
                )}
              </label>
            )}

            <label className="block text-[11px]">
              <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
                Reason (optional)
              </span>
              <textarea
                rows={3}
                value={reasonInput}
                onChange={(e) => setReasonInput(e.target.value)}
                placeholder={
                  kind === "mark_cancelled"
                    ? "e.g. Kalshi support confirmed order rejected"
                    : kind === "force_retry"
                      ? "e.g. upstream 502s cleared, requeue"
                      : "e.g. fills feed gap, manual confirmation from venue"
                }
                disabled={busy}
                data-testid="stuck-orders-dialog-reason"
                className="mt-1 w-full border border-border bg-card px-2 py-1 font-mono"
              />
            </label>

            <DialogFooter>
              <button
                type="button"
                onClick={onCancel}
                disabled={busy}
                className="border border-border px-3 py-1 text-[11px] uppercase tracking-widest text-muted-foreground hover:border-primary hover:text-foreground disabled:opacity-50"
                data-testid="stuck-orders-dialog-cancel"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={busy || !validClaim}
                onClick={() =>
                  onConfirm(
                    row.client_order_id,
                    kind,
                    reasonInput,
                    kind === "claim_filled" ? fillPriceCents : null,
                  )
                }
                className={`border px-3 py-1 text-[11px] uppercase tracking-widest disabled:opacity-50 ${
                  kind === "claim_filled"
                    ? "border-destructive bg-destructive/10 text-destructive hover:bg-destructive/20"
                    : kind === "mark_cancelled"
                      ? "border-amber-500 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20"
                      : "border-primary bg-primary/10 text-primary hover:bg-primary/20"
                }`}
                data-testid="stuck-orders-dialog-confirm"
              >
                {busy
                  ? "Working…"
                  : kind === "claim_filled"
                    ? "Confirm claim filled"
                    : kind === "mark_cancelled"
                      ? "Confirm mark cancelled"
                      : "Confirm force retry"}
              </button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
