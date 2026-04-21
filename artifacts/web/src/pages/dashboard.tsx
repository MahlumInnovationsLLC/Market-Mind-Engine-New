import { useClerk, useUser } from "@clerk/react";
import HealthSnapshot from "@/components/telemetry/health-snapshot";
import TopCounters from "@/components/telemetry/top-counters";
import RecentTrades from "@/components/telemetry/recent-trades";
import SwarmAnalysts from "@/components/telemetry/swarm-analysts";
import TradingCycles from "@/components/telemetry/trading-cycles";
import SwarmDebates from "@/components/telemetry/swarm-debates";
import ModelInventory from "@/components/telemetry/model-inventory";

export default function Dashboard() {
  const { user } = useUser();
  const { signOut } = useClerk();
  const operatorLabel =
    user?.primaryEmailAddress?.emailAddress ??
    user?.username ??
    user?.id ??
    "—";

  return (
    <div className="min-h-[100dvh] bg-background text-foreground p-4 md:p-6 font-mono selection:bg-primary/30">
      <header className="mb-6 flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl md:text-2xl font-bold tracking-tight text-primary uppercase">AXIOM_PREDICT :: OPERATOR_CONSOLE</h1>
          <p className="text-muted-foreground text-xs uppercase tracking-widest mt-1">Live Telemetry & Swarm Monitoring</p>
        </div>
        <div className="text-xs text-muted-foreground flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-success animate-pulse" />
            SYSTEM_ONLINE
          </div>
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

      <main className="grid grid-cols-1 xl:grid-cols-12 gap-6">
        {/* Left Column - Health & Stats */}
        <div className="xl:col-span-3 space-y-6">
          <HealthSnapshot />
          <TopCounters />
          <ModelInventory />
        </div>

        {/* Middle Column - Live Trades & Cycles */}
        <div className="xl:col-span-5 space-y-6">
          <RecentTrades />
          <TradingCycles />
        </div>

        {/* Right Column - Swarm Intelligence */}
        <div className="xl:col-span-4 space-y-6">
          <SwarmAnalysts />
          <SwarmDebates />
        </div>
      </main>
    </div>
  );
}
