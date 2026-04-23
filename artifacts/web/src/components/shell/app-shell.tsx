import type { ReactNode } from "react";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { Toaster } from "@/components/ui/toaster";
import {
  WebSocketProvider,
  usePipelineSocket,
} from "@/components/realtime/websocket-provider";
import { RealtimeNotifications } from "@/components/realtime/realtime-notifications";
import { ShellSidebar } from "./sidebar";
import { ShellHeader } from "./header";
import { DegradedModeBanner } from "./degraded-mode-banner";
import { TradingAlertsBanner } from "./trading-alerts-banner";

interface AppShellProps {
  isAdmin: boolean;
  operatorEmail: string | null;
  children: ReactNode;
}

/**
 * Inner shell that runs *inside* the WebSocketProvider so it can
 * pull the shared pipeline status into the sidebar dot. All page
 * subscribers (pipeline tracker, top-counters, etc.) read from the
 * same context.
 */
function AppShellInner({ isAdmin, operatorEmail, children }: AppShellProps) {
  const ws = usePipelineSocket();

  return (
    <SidebarProvider
      // Spec: 240px desktop sidebar (default shadcn token is 16rem=256px).
      style={{ "--sidebar-width": "240px" } as React.CSSProperties}
    >
      <ShellSidebar isAdmin={isAdmin} wsStatus={ws.status} />
      <SidebarInset className="flex flex-col min-h-[100dvh] bg-background">
        <DegradedModeBanner />
        <TradingAlertsBanner />
        <ShellHeader operatorEmail={operatorEmail} />
        <main
          className="flex-1 p-4 md:p-6 selection:bg-primary/30"
          data-testid="shell-main"
        >
          {children}
        </main>
        {/* Realtime notifications bridge — fires toasts for new alerts and
            live trade fills off the shared pipeline socket. */}
        <RealtimeNotifications />
        {/* Toaster lives inside the shell so toasts render above all
            authed surfaces and unmount cleanly when the user signs out. */}
        <Toaster />
      </SidebarInset>
    </SidebarProvider>
  );
}

export function AppShell(props: AppShellProps) {
  return (
    <WebSocketProvider>
      <AppShellInner {...props} />
    </WebSocketProvider>
  );
}
