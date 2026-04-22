import { useEffect, useState } from "react";
import { GlassCard } from "@/components/ui/glass-card";
import AccessRequests from "@/components/telemetry/access-requests";
import EmergencyStop from "@/components/telemetry/emergency-stop";
import LiveExecuteToggle from "@/components/telemetry/live-execute-toggle";
import RuntimeSettingsPanel from "@/components/telemetry/runtime-settings";
import ModelHealthAlerts from "@/components/telemetry/model-health-alerts";

/**
 * Collapsible card grouping the orchestrator + risk controls.
 *
 * Admin-only per §6.1: non-admins do not render the card at all.
 * Default-open is synced to `isAdmin` via an effect so admins see the
 * card expanded as soon as `useGetMyAccess` resolves, even if the
 * first render had `isAdmin=false`.
 */
export default function OperatorControls({ isAdmin }: { isAdmin: boolean }) {
  const [open, setOpen] = useState(isAdmin);

  useEffect(() => {
    if (isAdmin) setOpen(true);
  }, [isAdmin]);

  if (!isAdmin) return null;

  return (
    <GlassCard
      tone="solid"
      padding="none"
      className="overflow-hidden"
      data-testid="operator-controls"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 border-b border-border bg-loss/5 px-3 py-2 text-left transition-colors hover:bg-loss/10"
        data-testid="button-toggle-operator-controls"
      >
        <div className="flex items-center gap-3">
          <span className="text-[10px] font-bold uppercase tracking-widest text-loss">
            OPERATOR CONTROLS
          </span>
          <span className="border border-loss/60 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-loss">
            ADMIN
          </span>
        </div>
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {open ? "▼ HIDE" : "▶ EXPAND"}
        </span>
      </button>
      {open ? (
        <div
          className="grid grid-cols-1 gap-3 p-3 lg:grid-cols-2"
          data-testid="operator-controls-body"
        >
          <EmergencyStop />
          <LiveExecuteToggle />
          <AccessRequests />
          <ModelHealthAlerts />
          <div className="lg:col-span-2">
            <RuntimeSettingsPanel />
          </div>
        </div>
      ) : null}
    </GlassCard>
  );
}
