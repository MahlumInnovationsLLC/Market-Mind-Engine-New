import { Link, useLocation } from "wouter";
import {
  Activity,
  BarChart3,
  Briefcase,
  Brain,
  Cpu,
  LayoutDashboard,
  LineChart,
  Settings,
  Shield,
  ShieldAlert,
  TrendingUp,
  User,
  Bell,
} from "lucide-react";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarSeparator,
} from "@/components/ui/sidebar";
import {
  getGetVerticalsOverviewQueryKey,
  getListModelHealthAlertsQueryKey,
  useGetVerticalsOverview,
  useListModelHealthAlerts,
} from "@workspace/api-client-react";
import type { WSStatus } from "@/hooks/use-websocket";

interface SidebarItem {
  href: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  badge?: number | string | null;
  testId: string;
  exact?: boolean;
}

interface SidebarSectionDef {
  label: string;
  items: SidebarItem[];
}

interface ShellSidebarProps {
  isAdmin: boolean;
  wsStatus: WSStatus;
}

function isActive(current: string, href: string, exact?: boolean): boolean {
  if (exact) return current === href;
  return current === href || current.startsWith(`${href}/`);
}

export function ShellSidebar({ isAdmin, wsStatus }: ShellSidebarProps) {
  const [location] = useLocation();

  // Sum open positions across visible verticals — drives the Positions badge.
  const verticals = useGetVerticalsOverview(
    { days: 7 },
    {
      query: {
        queryKey: getGetVerticalsOverviewQueryKey({ days: 7 }),
        staleTime: 60_000,
        refetchInterval: 60_000,
      },
    },
  );
  const openPositions = (verticals.data?.verticals ?? []).reduce(
    (sum, v) => sum + (v.open_positions ?? 0),
    0,
  );

  // Unread alert count — drives the Intelligence badge.
  const alertsParams = { ack: false, limit: 50 };
  const alerts = useListModelHealthAlerts(alertsParams, {
    query: {
      queryKey: getListModelHealthAlertsQueryKey(alertsParams),
      staleTime: 60_000,
      refetchInterval: 60_000,
    },
  });
  const unreadAlerts = (alerts.data?.items ?? []).length;

  const sections: SidebarSectionDef[] = [
    {
      label: "Markets",
      items: [
        {
          href: "/app",
          label: "Overview",
          icon: LayoutDashboard,
          testId: "nav-overview",
          exact: true,
        },
        {
          href: "/verticals",
          label: "Verticals",
          icon: BarChart3,
          testId: "nav-verticals",
        },
      ],
    },
    {
      label: "Portfolio",
      items: [
        {
          href: "/positions",
          label: "Positions",
          icon: Briefcase,
          badge: openPositions > 0 ? openPositions : null,
          testId: "nav-positions",
        },
        {
          href: "/trading",
          label: "Trading",
          icon: TrendingUp,
          testId: "nav-trading",
        },
      ],
    },
    {
      label: "Intelligence",
      items: [
        {
          href: "/pipeline",
          label: "Pipeline",
          icon: Activity,
          testId: "nav-pipeline",
        },
        {
          href: "/intelligence",
          label: "Intelligence",
          icon: Brain,
          badge: unreadAlerts > 0 ? unreadAlerts : null,
          testId: "nav-intelligence",
        },
      ],
    },
    {
      label: "System",
      items: [
        {
          href: "/system",
          label: "System",
          icon: Cpu,
          testId: "nav-system",
        },
        ...(isAdmin
          ? [
              {
                href: "/admin",
                label: "Admin",
                icon: Shield,
                testId: "nav-admin",
              },
            ]
          : []),
      ],
    },
    {
      label: "Account",
      items: [
        {
          href: "/settings/notifications",
          label: "Notifications",
          icon: Bell,
          testId: "nav-notifications",
        },
        {
          href: "/profile",
          label: "Profile",
          icon: User,
          testId: "nav-profile",
        },
      ],
    },
  ];

  const wsDot =
    wsStatus === "open"
      ? "bg-success animate-pulse"
      : wsStatus === "connecting"
        ? "bg-warning animate-pulse"
        : "bg-destructive";

  return (
    <Sidebar collapsible="icon" data-testid="shell-sidebar">
      <SidebarHeader>
        <Link
          href="/app"
          className="flex items-center gap-2 px-2 py-2 group/brand"
          data-testid="link-shell-brand"
        >
          <div className="relative w-8 h-8 shrink-0 rounded-md border border-primary/40 bg-primary/10 flex items-center justify-center">
            <LineChart className="w-4 h-4 text-primary" />
            <span
              className={`absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full ${wsDot}`}
              aria-hidden
            />
          </div>
          <div className="flex flex-col leading-tight group-data-[collapsible=icon]:hidden">
            <span className="text-sm font-bold tracking-tight text-foreground uppercase">
              Axiom
            </span>
            <span className="text-[9px] uppercase tracking-widest text-muted-foreground">
              Predict
            </span>
          </div>
        </Link>
      </SidebarHeader>
      <SidebarSeparator />
      <SidebarContent>
        {sections.map((section) => (
          <SidebarGroup key={section.label}>
            <SidebarGroupLabel className="text-[10px] uppercase tracking-widest">
              {section.label}
            </SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {section.items.map((item) => {
                  const active = isActive(location, item.href, item.exact);
                  const Icon = item.icon;
                  return (
                    <SidebarMenuItem key={item.href}>
                      <SidebarMenuButton
                        asChild
                        isActive={active}
                        tooltip={item.label}
                        data-testid={item.testId}
                      >
                        <Link href={item.href}>
                          <Icon className="w-4 h-4" />
                          <span>{item.label}</span>
                        </Link>
                      </SidebarMenuButton>
                      {item.badge ? (
                        <SidebarMenuBadge data-testid={`${item.testId}-badge`}>
                          {item.badge}
                        </SidebarMenuBadge>
                      ) : null}
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>
      <SidebarFooter>
        <div className="px-2 py-1.5 text-[9px] uppercase tracking-widest text-muted-foreground/60 group-data-[collapsible=icon]:hidden">
          <ShieldAlert className="inline w-3 h-3 mr-1" />
          v1.0 · operator
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}
