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
import { EloUpdaterBanner } from "./elo-updater-banner";
import { StuckWsBanner } from "./stuck-ws-banner";
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
      {/* Keyboard-only skip link — visually hidden until focused.
          Lets screen-reader / keyboard users jump straight to the
          page's main region instead of tabbing through the entire
          sidebar + header on every navigation. */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[60] focus:px-3 focus:py-2 focus:rounded focus:bg-primary focus:text-primary-foreground focus:font-mono focus:text-xs focus:uppercase focus:tracking-widest focus:outline-none focus:ring-2 focus:ring-primary"
        data-testid="link-skip-to-content"
      >
        Skip to main content
      </a>
      <ShellSidebar isAdmin={isAdmin} wsStatus={ws.status} />
      <SidebarInset className="flex flex-col min-h-[100dvh] bg-background">
        <DegradedModeBanner />
        <TradingAlertsBanner />
        <StuckWsBanner />
        <EloUpdaterBanner />
        <ShellHeader operatorEmail={operatorEmail} />
        <main
          id="main-content"
          tabIndex={-1}
          className="flex-1 p-4 md:p-6 selection:bg-primary/30 focus:outline-none"
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
