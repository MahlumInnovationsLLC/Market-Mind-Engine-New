import { Link } from "wouter";
import { useClerk, useUser } from "@clerk/react";
import {
  getGetMyAccessQueryKey,
  useGetMyAccess,
} from "@workspace/api-client-react";
import AccessRequests from "@/components/telemetry/access-requests";
import HealthSnapshot from "@/components/telemetry/health-snapshot";
import TopCounters from "@/components/telemetry/top-counters";
import RecentTrades from "@/components/telemetry/recent-trades";
import RuntimeSettingsPanel from "@/components/telemetry/runtime-settings";
import SwarmAnalysts from "@/components/telemetry/swarm-analysts";
import TradingCycles from "@/components/telemetry/trading-cycles";
import SwarmDebates from "@/components/telemetry/swarm-debates";
import LiveExecuteToggle from "@/components/telemetry/live-execute-toggle";
import EmergencyStop from "@/components/telemetry/emergency-stop";
import ModelInventory from "@/components/telemetry/model-inventory";
import ActiveModelBadge from "@/components/telemetry/active-model-badge";
import SettlementPanel from "@/components/telemetry/settlement-panel";
import VerticalPerformance from "@/components/telemetry/vertical-performance";
import EventsEconCalendarPanel from "@/components/telemetry/events-econ-calendar";
import ModelHealthAlerts from "@/components/telemetry/model-health-alerts";
import WsStatusIndicator from "@/components/ws-status-indicator";
import { useWebSocket } from "@/hooks/use-websocket";

export default function Dashboard() {
  const { user } = useUser();
  const { signOut } = useClerk();
  const operatorLabel =
    user?.primaryEmailAddress?.emailAddress ??
    user?.username ??
    user?.id ??
    "—";

  // Lightweight header subscription so the operator sees connection
  // health at a glance.  The widgets each maintain their own subscription
  // for cache-invalidation; this one just drives the indicator.
  const ws = useWebSocket({ path: "/api/ws/pipeline", bufferSize: 5 });

  const { data: access } = useGetMyAccess({
    query: { queryKey: getGetMyAccessQueryKey(), staleTime: 30_000 },
  });
  const isAdmin = !!access?.is_admin;

  return (
    <div className="min-h-[100dvh] bg-background text-foreground p-4 md:p-6 font-mono selection:bg-primary/30">
      <header className="mb-6 flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl md:text-2xl font-bold tracking-tight text-primary uppercase">AXIOM_PREDICT :: OPERATOR_CONSOLE</h1>
          <p className="text-muted-foreground text-xs uppercase tracking-widest mt-1">Live Telemetry & Swarm Monitoring</p>
        </div>
        <div className="text-xs text-muted-foreground flex flex-wrap items-center gap-4">
          <WsStatusIndicator
            status={ws.status}
            reconnectIn={ws.reconnectIn}
            closeCode={ws.closeCode}
            label="LIVE"
          />
          <ActiveModelBadge />
          <button
            type="button"
            onClick={() => {
              window.dispatchEvent(new CustomEvent("axiom:open-search"));
            }}
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
            data-testid="button-open-search"
            title="Search trades, debates, and cycles"
          >
            Search ⌘K
          </button>
          <Link
            href="/pipeline"
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-primary hover:border-primary"
            data-testid="link-pipeline"
          >
            Pipeline →
          </Link>
          {isAdmin && (
            <Link
              href="/admin"
              className="border border-primary px-3 py-1 uppercase tracking-widest text-[10px] text-primary hover:bg-primary/10"
              data-testid="link-admin"
            >
              Admin →
            </Link>
          )}
          <div className="flex items-center gap-2">
            <span className="uppercase tracking-widest">OP</span>
            <span className="text-foreground truncate max-w-[200px]" data-testid="text-operator-id">{operatorLabel}</span>
          </div>
          <button
            type="button"
            onClick={() => signOut({ redirectUrl: import.meta.env.BASE_URL })}
            className="border border-border px-3 py-1 uppercase tracking-widest text-[10px] text-muted-foreground hover:text-destructive hover:border-destructive transition-colors"
            data-testid="button-sign-out"
          >
            Sign out
          </button>
        </div>
      </header>

      <div className="mb-6 grid grid-cols-1 lg:grid-cols-2 gap-6">
        <VerticalPerformance />
        <EventsEconCalendarPanel />
      </div>

      <main className="grid grid-cols-1 xl:grid-cols-12 gap-6">
        {/* Left Column - Health & Stats */}
        <div className="xl:col-span-3 space-y-6">
          <HealthSnapshot />
          {isAdmin && <EmergencyStop />}
          <LiveExecuteToggle />
          {isAdmin && <AccessRequests />}
          <TopCounters />
          <ModelInventory />
        </div>

        {/* Middle Column - Live Trades & Cycles */}
        <div className="xl:col-span-5 space-y-6">
          <RecentTrades />
          <TradingCycles />
          <SettlementPanel />
        </div>

        {/* Right Column - Swarm Intelligence */}
        <div className="xl:col-span-4 space-y-6">
          <ModelHealthAlerts />
          <RuntimeSettingsPanel />
          <SwarmAnalysts />
          <SwarmDebates />
        </div>
      </main>
    </div>
  );
}
