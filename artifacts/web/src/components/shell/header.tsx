import { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "wouter";
import { useAuth, useClerk, useUser } from "@clerk/react";
import { useQuery } from "@tanstack/react-query";
import { Bell, LogOut, Moon, Search, Sun } from "lucide-react";
import {
  useHealthFull,
  getHealthFullQueryKey,
  useListModelHealthAlerts,
  getListModelHealthAlertsQueryKey,
} from "@workspace/api-client-react";
import { apiFetch } from "@/lib/api";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Separator } from "@/components/ui/separator";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface ShellHeaderProps {
  operatorEmail: string | null;
}

type RuntimeSettingsLite = {
  trading_halted?: boolean;
  aggression_mode?: string;
};

type SubscriptionLite = {
  tier?: string | null;
};

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

export function ShellHeader({ operatorEmail: emailOverride }: ShellHeaderProps) {
  const { user } = useUser();
  const { signOut } = useClerk();
  const { getToken, isSignedIn } = useAuth();
  const [, setLocation] = useLocation();

  const [theme, setTheme] = useState<"dark" | "light">(() => loadTheme());
  useEffect(() => {
    applyTheme(theme);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(THEME_KEY, theme);
    }
  }, [theme]);

  const health = useHealthFull({
    query: { refetchInterval: 30_000, queryKey: getHealthFullQueryKey() },
  });
  const alertsParams = { ack: false, limit: 50 };
  const alerts = useListModelHealthAlerts(alertsParams, {
    query: {
      queryKey: getListModelHealthAlertsQueryKey(alertsParams),
      staleTime: 60_000,
      refetchInterval: 60_000,
    },
  });
  const unreadAlerts = (alerts.data?.items ?? []).length;

  // Runtime settings drives the kill-switch / dry-run portion of the
  // trading-mode pill. The spec asks for PAPER / LIVE / DRY-RUN derived
  // from runtime settings; the current API exposes `trading_halted` (kill
  // switch) but no separate `live_execute_enabled` field, so we combine
  // it with `paper_mode` from /api/health/full:
  //   • paper_mode === true                       → PAPER
  //   • paper_mode === false && trading_halted    → DRY-RUN
  //   • paper_mode === false                      → LIVE
  const tokenGetter = useMemo(
    () => () => getToken().then((t) => t ?? null),
    [getToken],
  );
  const runtime = useQuery<RuntimeSettingsLite>({
    queryKey: ["shell", "runtime-settings"],
    queryFn: () =>
      apiFetch<RuntimeSettingsLite>("/api/dashboard/runtime-settings", {
        getToken: tokenGetter,
      }),
    enabled: !!isSignedIn,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  // Subscription tier — drives the small tier badge next to the avatar
  // (spec: avatar + tier badge + sign-out). No generated hook exists for
  // /api/me/subscription so we fetch it directly.
  const subscription = useQuery<SubscriptionLite>({
    queryKey: ["shell", "me-subscription"],
    queryFn: () =>
      apiFetch<SubscriptionLite>("/api/me/subscription", {
        getToken: tokenGetter,
      }),
    enabled: !!isSignedIn,
    staleTime: 5 * 60_000,
    refetchInterval: 5 * 60_000,
  });
  const tier = (subscription.data?.tier ?? "").toString().trim().toUpperCase();

  const paperMode = health.data?.paper_mode;
  const tradingHalted = !!runtime.data?.trading_halted;
  const env = health.data?.kalshi_env?.toUpperCase();
  let modeLabel = "—";
  let modeClass = "border-border text-muted-foreground";
  if (paperMode === true) {
    modeLabel = "PAPER";
    modeClass = "border-info/60 text-info bg-info/5";
  } else if (paperMode === false && tradingHalted) {
    modeLabel = "DRY-RUN";
    modeClass = "border-warning/60 text-warning bg-warning/5";
  } else if (paperMode === false) {
    modeLabel = "LIVE";
    modeClass = "border-loss/60 text-loss bg-loss/5";
  }

  const operatorEmail =
    emailOverride ??
    user?.primaryEmailAddress?.emailAddress ??
    user?.username ??
    "operator";
  const initials = (operatorEmail || "OP").slice(0, 2).toUpperCase();

  const openSearch = () => {
    window.dispatchEvent(new CustomEvent("axiom:open-search"));
  };

  return (
    <header
      className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-border bg-background/85 backdrop-blur-sm px-4 font-mono"
      data-testid="shell-header"
    >
      <SidebarTrigger data-testid="button-shell-sidebar-trigger" />
      <Separator orientation="vertical" className="h-6 mx-1" />

      <div
        className={`hidden md:flex items-center gap-1.5 border px-2.5 py-1 rounded-sm text-[10px] uppercase tracking-widest font-bold ${modeClass}`}
        data-testid="pill-trading-mode"
      >
        <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />
        <span>{modeLabel}</span>
        {env ? <span className="opacity-60 normal-case font-normal">· {env}</span> : null}
      </div>

      <div className="ml-auto flex items-center gap-1">
        <button
          type="button"
          onClick={openSearch}
          className="hidden sm:flex items-center gap-2 border border-border bg-card/50 hover:border-primary/40 hover:text-primary text-muted-foreground px-2.5 py-1 rounded-sm text-[10px] uppercase tracking-widest transition-colors"
          data-testid="button-shell-open-search"
          title="Search trades, debates, cycles (⌘K)"
        >
          <Search className="w-3 h-3" />
          <span>Search</span>
          <kbd className="ml-2 px-1 py-0.5 border border-border/60 rounded text-[9px] text-muted-foreground/80 normal-case">
            ⌘K
          </kbd>
        </button>
        <button
          type="button"
          onClick={openSearch}
          className="sm:hidden p-2 text-muted-foreground hover:text-primary"
          data-testid="button-shell-open-search-mobile"
          aria-label="Search"
        >
          <Search className="w-4 h-4" />
        </button>

        <button
          type="button"
          onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
          className="p-2 text-muted-foreground hover:text-primary rounded-sm"
          data-testid="button-shell-theme-toggle"
          title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
        >
          {theme === "dark" ? (
            <Sun className="w-4 h-4" />
          ) : (
            <Moon className="w-4 h-4" />
          )}
        </button>

        <Link
          href="/settings/notifications"
          className="relative p-2 text-muted-foreground hover:text-primary rounded-sm"
          data-testid="button-shell-alerts"
          title={unreadAlerts > 0 ? `${unreadAlerts} unread alerts` : "Alerts"}
        >
          <Bell className="w-4 h-4" />
          {unreadAlerts > 0 ? (
            <span
              className="absolute top-1 right-1 min-w-[14px] h-[14px] px-0.5 rounded-full bg-loss text-[9px] text-loss-foreground font-bold flex items-center justify-center"
              data-testid="badge-shell-alerts"
            >
              {unreadAlerts > 99 ? "99+" : unreadAlerts}
            </span>
          ) : null}
        </Link>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="flex items-center gap-2 border border-border hover:border-primary/40 px-2 py-1 rounded-sm text-xs"
              data-testid="button-shell-user"
            >
              <div className="w-6 h-6 rounded-full bg-primary/15 border border-primary/40 text-primary font-bold text-[10px] flex items-center justify-center">
                {initials}
              </div>
              <div className="hidden md:flex flex-col leading-tight items-start">
                <span
                  className="text-foreground truncate max-w-[140px] normal-case"
                  data-testid="text-shell-operator-id"
                >
                  {operatorEmail}
                </span>
              </div>
              {tier ? (
                <span
                  className="hidden md:inline-flex items-center border border-primary/40 bg-primary/10 text-primary px-1.5 py-0.5 rounded-sm text-[9px] uppercase tracking-widest font-bold ml-1"
                  data-testid="badge-shell-tier"
                  title={`Subscription tier: ${tier}`}
                >
                  {tier}
                </span>
              ) : null}
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel className="text-[10px] uppercase tracking-widest text-muted-foreground">
              Operator
            </DropdownMenuLabel>
            <DropdownMenuItem
              onSelect={() => setLocation("/profile")}
              data-testid="menu-shell-profile"
            >
              Profile
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => setLocation("/settings/notifications")}
              data-testid="menu-shell-notifications"
            >
              Notification settings
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={() => setLocation("/billing")}
              data-testid="menu-shell-billing"
            >
              Billing
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onClick={() =>
                signOut({ redirectUrl: import.meta.env.BASE_URL })
              }
              data-testid="button-shell-sign-out"
              className="text-loss focus:text-loss"
            >
              <LogOut className="w-3.5 h-3.5 mr-2" />
              Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
