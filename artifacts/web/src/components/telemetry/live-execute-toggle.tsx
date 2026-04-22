import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getGetLiveExecuteStateQueryKey,
  useGetLiveExecuteState,
  useSetLiveExecuteState,
} from "@workspace/api-client-react";

/**
 * Live-execute kill switch UI.
 *
 * Renders nothing for sub-Elite callers (the API also 403s the POST,
 * but we hide the UI entirely so free/Pro users never see a control
 * they can't operate).  Elite + founder see an arming toggle that
 * flips ``system_settings.risk.trading_mode`` between ``paper`` and
 * ``live``.
 */
export default function LiveExecuteToggle() {
  const queryClient = useQueryClient();
  const queryKey = getGetLiveExecuteStateQueryKey();
  const { data, isLoading } = useGetLiveExecuteState({
    query: { queryKey, refetchInterval: 30_000 },
  });
  const mutation = useSetLiveExecuteState({
    mutation: {
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey });
      },
    },
  });
  const [pendingArm, setPendingArm] = useState(false);

  if (isLoading || !data) {
    return null;
  }
  if (!data.can_toggle) {
    // Sub-Elite — surface nothing.  Pricing page handles the upsell.
    return null;
  }

  const enabled = !!data.enabled;
  const busy = mutation.isPending;

  function onToggle() {
    if (busy) return;
    if (!enabled) {
      // Two-step arm so a stray click can't flip a paper deployment
      // into live execution by accident.
      if (!pendingArm) {
        setPendingArm(true);
        return;
      }
    }
    setPendingArm(false);
    mutation.mutate({ data: { enabled: !enabled } });
  }

  const stateLabel = enabled ? "LIVE" : "PAPER";
  const stateClass = enabled
    ? "text-loss border-loss/60 bg-loss/10"
    : "text-muted-foreground border-border";
  const buttonLabel = enabled
    ? "Switch to PAPER"
    : pendingArm
      ? "Confirm: ARM LIVE"
      : "Arm LIVE execution";

  return (
    <div
      className="border border-border bg-card p-3 flex flex-col gap-2"
      data-testid="live-execute-toggle"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider font-bold text-muted-foreground">
          LIVE_EXECUTE
        </h2>
        <span
          className={`text-[10px] uppercase border px-2 py-0.5 ${stateClass}`}
          data-testid="live-execute-state"
        >
          {stateLabel}
        </span>
      </div>
      <p className="text-[10px] text-muted-foreground leading-snug">
        Flips <code>risk.trading_mode</code>. Elite-only — sub-Elite
        users never see this control.
      </p>
      <button
        type="button"
        onClick={onToggle}
        disabled={busy}
        className={`text-xs uppercase tracking-widest border px-3 py-1.5 transition-colors ${
          enabled
            ? "border-muted-foreground text-muted-foreground hover:bg-muted/30"
            : pendingArm
              ? "border-loss text-loss hover:bg-loss/10"
              : "border-primary text-primary hover:bg-primary/10"
        } ${busy ? "opacity-50 cursor-not-allowed" : ""}`}
        data-testid="button-live-execute-toggle"
      >
        {busy ? "…" : buttonLabel}
      </button>
      {pendingArm && !enabled && (
        <button
          type="button"
          onClick={() => setPendingArm(false)}
          className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground"
          data-testid="button-live-execute-cancel"
        >
          cancel
        </button>
      )}
    </div>
  );
}
