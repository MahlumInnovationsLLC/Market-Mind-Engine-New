import { useState } from "react";
import { GlassCard } from "@/components/ui/glass-card";
import AccessRequests from "@/components/telemetry/access-requests";
import EmergencyStop from "@/components/telemetry/emergency-stop";
import LiveExecuteToggle from "@/components/telemetry/live-execute-toggle";
import RuntimeSettingsPanel from "@/components/telemetry/runtime-settings";
import ModelHealthAlerts from "@/components/telemetry/model-health-alerts";

/**
 * Collapsible card grouping the orchestrator + risk controls.
 *
 * - Visible to everyone, but admin-only widgets (EmergencyStop,
 *   AccessRequests, ModelHealthAlerts) are gated inside.
 * - LiveExecuteToggle and RuntimeSettings remain visible to all —
 *   each component already self-gates by tier and the API enforces
 *   real authorization.
 * - Expanded by default for admins; collapsed by default for everyone
 *   else, per the §6.1 brief.
 */
export default function OperatorControls({ isAdmin }: { isAdmin: boolean }) {
  const [open, setOpen] = useState(isAdmin);

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
        className={`flex w-full items-center justify-between gap-2 border-b border-border px-3 py-2 text-left transition-colors ${
          isAdmin ? "bg-loss/5 hover:bg-loss/10" : "bg-muted/20 hover:bg-muted/30"
        }`}
        data-testid="button-toggle-operator-controls"
      >
        <div className="flex items-center gap-3">
          <span
            className={`text-[10px] uppercase tracking-widest font-bold ${
              isAdmin ? "text-loss" : "text-muted-foreground"
            }`}
          >
            OPERATOR CONTROLS
          </span>
          {isAdmin ? (
            <span className="border border-loss/60 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-loss">
              ADMIN
            </span>
          ) : null}
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
          {isAdmin ? <EmergencyStop /> : null}
          <LiveExecuteToggle />
          {isAdmin ? <AccessRequests /> : null}
          {isAdmin ? <ModelHealthAlerts /> : null}
          <div className="lg:col-span-2">
            <RuntimeSettingsPanel />
          </div>
        </div>
      ) : null}
    </GlassCard>
  );
}
