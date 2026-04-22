import type { ReactNode } from "react";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { useWebSocket } from "@/hooks/use-websocket";
import { ShellSidebar } from "./sidebar";
import { ShellHeader } from "./header";
import { DegradedModeBanner } from "./degraded-mode-banner";
import { TradingAlertsBanner } from "./trading-alerts-banner";

interface AppShellProps {
  isAdmin: boolean;
  children: ReactNode;
}

export function AppShell({ isAdmin, children }: AppShellProps) {
  // Single shell-wide pipeline subscription drives the brand-block
  // connection dot and lets pages share its status via context if
  // needed later. Buffer is tiny — pages keep their own WS hooks
  // for full event streams.
  const ws = useWebSocket({ path: "/api/ws/pipeline", bufferSize: 5 });

  return (
    <SidebarProvider>
      <ShellSidebar isAdmin={isAdmin} wsStatus={ws.status} />
      <SidebarInset className="flex flex-col min-h-[100dvh] bg-background">
        <DegradedModeBanner />
        <TradingAlertsBanner />
        <ShellHeader />
        <main
          className="flex-1 p-4 md:p-6 selection:bg-primary/30"
          data-testid="shell-main"
        >
          {children}
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}
