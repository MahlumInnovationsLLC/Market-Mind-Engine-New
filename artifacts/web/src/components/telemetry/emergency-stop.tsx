import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAdminEmergencyStop } from "@workspace/api-client-react";
import { formatErrorMessage } from "@/lib/api";

/**
 * Operator "kill all" control.
 *
 * Two-step confirm so a stray click can't cancel a live book by accident.
 * Visible only to admins (the parent dashboard already gates rendering on
 * `isAdmin`). The endpoint itself also enforces admin auth.
 */
export default function EmergencyStop() {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");
  const [result, setResult] = useState<{
    canceled: number;
    at: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const mutation = useAdminEmergencyStop({
    mutation: {
      onSuccess: (data) => {
        setResult({
          canceled: Number(data?.canceled_orders ?? 0),
          at: new Date().toISOString(),
        });
        setError(null);
        setConfirming(false);
        // Live-execute toggle reads the same trading_mode setting.
        queryClient.invalidateQueries({
          queryKey: ["/orchestrator/live-execute"],
        });
      },
      onError: (err: unknown) => {
        setError(
          formatErrorMessage(
            err,
            err && typeof err === "object" && "message" in err
              ? undefined
              : "emergency-stop failed",
          ),
        );
      },
    },
  });

  const busy = mutation.isPending;

  function onArm() {
    if (busy) return;
    setError(null);
    setConfirming(true);
  }

  function onCancelArm() {
    if (busy) return;
    setConfirming(false);
  }

  function onConfirm() {
    if (busy) return;
    mutation.mutate({
      data: { reason: reason.trim() || undefined },
    });
  }

  return (
    <div
      className="border border-loss/60 bg-loss/5 p-3 flex flex-col gap-2"
      data-testid="emergency-stop"
    >
      <div className="flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider font-bold text-loss">
          EMERGENCY_STOP
        </h2>
        <span className="text-[10px] uppercase border border-loss/60 text-loss px-2 py-0.5">
          KILL ALL
        </span>
      </div>
      <p className="text-[10px] text-muted-foreground leading-snug">
        Cancels every resting Kalshi order, halts the trading loop, and
        emails the on-call operator. Persists across restarts until an
        admin re-arms LIVE.
      </p>

      {!confirming ? (
        <button
          type="button"
          onClick={onArm}
          disabled={busy}
          className={`text-xs uppercase tracking-widest border px-3 py-1.5 transition-colors border-loss text-loss hover:bg-loss/10 ${
            busy ? "opacity-50 cursor-not-allowed" : ""
          }`}
          data-testid="button-emergency-stop-arm"
        >
          Emergency Stop
        </button>
      ) : (
        <div className="flex flex-col gap-2 border border-loss/60 p-2">
          <label className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Reason (optional)
          </label>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. settlement feed lag"
            disabled={busy}
            className="bg-background border border-border text-xs px-2 py-1 font-mono"
            data-testid="input-emergency-stop-reason"
            maxLength={200}
          />
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy}
              className={`flex-1 text-xs uppercase tracking-widest border border-loss bg-loss text-background px-3 py-1.5 hover:bg-loss/80 ${
                busy ? "opacity-50 cursor-not-allowed" : ""
              }`}
              data-testid="button-emergency-stop-confirm"
            >
              {busy ? "…" : "CONFIRM: KILL ALL"}
            </button>
            <button
              type="button"
              onClick={onCancelArm}
              disabled={busy}
              className="text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground border border-border px-2"
              data-testid="button-emergency-stop-cancel"
            >
              cancel
            </button>
          </div>
        </div>
      )}

      {error && (
        <p
          className="text-[10px] text-loss"
          data-testid="text-emergency-stop-error"
        >
          {error}
        </p>
      )}
      {result && !error && (
        <p
          className="text-[10px] text-muted-foreground"
          data-testid="text-emergency-stop-result"
        >
          Halted at {result.at}. {result.canceled} order
          {result.canceled === 1 ? "" : "s"} canceled.
        </p>
      )}
    </div>
  );
}
