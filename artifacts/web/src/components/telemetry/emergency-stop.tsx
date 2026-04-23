import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getGetLiveExecuteStateQueryKey,
  useAdminEmergencyStop,
  useAdminRearmTrading,
  useGetLiveExecuteState,
} from "@workspace/api-client-react";
import { formatErrorMessage } from "@/lib/api";

/**
 * Operator "kill all" control + re-arm.
 *
 * The Stop button has a two-step confirm so a stray click can't cancel
 * a live book by accident. When the system is halted (the emergency
 * stop persists ``risk.trading_mode='halt'`` so the cycle gate keeps
 * refusing orders across restarts) we surface the halted state and
 * the re-arm controls so admins don't have to UPDATE system_settings
 * by hand. Visible only to admins (the parent dashboard already gates
 * rendering on `isAdmin`); the endpoints also enforce admin auth.
 */
export default function EmergencyStop() {
  const queryClient = useQueryClient();
  const liveExecuteKey = getGetLiveExecuteStateQueryKey();
  const { data: liveExecute } = useGetLiveExecuteState({
    query: { queryKey: liveExecuteKey, refetchInterval: 30_000 },
  });

  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");
  const [result, setResult] = useState<{
    canceled: number;
    at: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rearmError, setRearmError] = useState<string | null>(null);
  const [rearmResult, setRearmResult] = useState<{
    mode: string;
    at: string;
  } | null>(null);

  const stopMutation = useAdminEmergencyStop({
    mutation: {
      onSuccess: (data) => {
        setResult({
          canceled: Number(data?.canceled_orders ?? 0),
          at: new Date().toISOString(),
        });
        setError(null);
        setConfirming(false);
        setRearmResult(null);
        queryClient.invalidateQueries({ queryKey: liveExecuteKey });
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

  const rearmMutation = useAdminRearmTrading({
    mutation: {
      onSuccess: (data) => {
        setRearmResult({
          mode: String(data?.trading_mode ?? ""),
          at: new Date().toISOString(),
        });
        setRearmError(null);
        // Clear the prior halt result so the card doesn't keep
        // claiming the system is still halted after a successful
        // re-arm.
        setResult(null);
        queryClient.invalidateQueries({ queryKey: liveExecuteKey });
      },
      onError: (err: unknown) => {
        setRearmError(
          formatErrorMessage(
            err,
            err && typeof err === "object" && "message" in err
              ? undefined
              : "rearm failed",
          ),
        );
      },
    },
  });

  const stopBusy = stopMutation.isPending;
  const rearmBusy = rearmMutation.isPending;
  const halted = (liveExecute?.effective_mode ?? "").toLowerCase() === "halt";

  function onArm() {
    if (stopBusy) return;
    setError(null);
    setConfirming(true);
  }

  function onCancelArm() {
    if (stopBusy) return;
    setConfirming(false);
  }

  function onConfirm() {
    if (stopBusy) return;
    stopMutation.mutate({
      data: { reason: reason.trim() || undefined },
    });
  }

  function onRearm(mode: "paper" | "live") {
    if (rearmBusy) return;
    setRearmError(null);
    rearmMutation.mutate({ data: { mode } });
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
        {halted ? (
          <span
            className="text-[10px] uppercase border border-loss bg-loss text-background px-2 py-0.5"
            data-testid="emergency-stop-state"
          >
            HALTED
          </span>
        ) : (
          <span
            className="text-[10px] uppercase border border-loss/60 text-loss px-2 py-0.5"
            data-testid="emergency-stop-state"
          >
            KILL ALL
          </span>
        )}
      </div>
      <p className="text-[10px] text-muted-foreground leading-snug">
        Cancels every resting Kalshi order, halts the trading loop, and
        emails the on-call operator. Persists across restarts until an
        admin re-arms.
      </p>

      {halted ? (
        <div
          className="flex flex-col gap-2 border border-loss/60 p-2"
          data-testid="emergency-stop-rearm"
        >
          <p className="text-[10px] uppercase tracking-widest text-loss">
            Trading is halted. Re-arm to resume the loop.
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => onRearm("paper")}
              disabled={rearmBusy}
              className={`flex-1 text-xs uppercase tracking-widest border border-muted-foreground text-muted-foreground px-3 py-1.5 hover:bg-muted/30 ${
                rearmBusy ? "opacity-50 cursor-not-allowed" : ""
              }`}
              data-testid="button-rearm-paper"
            >
              {rearmBusy ? "…" : "Re-arm trading (PAPER)"}
            </button>
            <button
              type="button"
              onClick={() => onRearm("live")}
              disabled={rearmBusy}
              className={`flex-1 text-xs uppercase tracking-widest border border-loss text-loss px-3 py-1.5 hover:bg-loss/10 ${
                rearmBusy ? "opacity-50 cursor-not-allowed" : ""
              }`}
              data-testid="button-rearm-live"
            >
              {rearmBusy ? "…" : "Re-arm trading (LIVE)"}
            </button>
          </div>
          {rearmError && (
            <p
              className="text-[10px] text-loss"
              data-testid="text-rearm-error"
            >
              {rearmError}
            </p>
          )}
        </div>
      ) : !confirming ? (
        <button
          type="button"
          onClick={onArm}
          disabled={stopBusy}
          className={`text-xs uppercase tracking-widest border px-3 py-1.5 transition-colors border-loss text-loss hover:bg-loss/10 ${
            stopBusy ? "opacity-50 cursor-not-allowed" : ""
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
            disabled={stopBusy}
            className="bg-background border border-border text-xs px-2 py-1 font-mono"
            data-testid="input-emergency-stop-reason"
            maxLength={200}
          />
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onConfirm}
              disabled={stopBusy}
              className={`flex-1 text-xs uppercase tracking-widest border border-loss bg-loss text-background px-3 py-1.5 hover:bg-loss/80 ${
                stopBusy ? "opacity-50 cursor-not-allowed" : ""
              }`}
              data-testid="button-emergency-stop-confirm"
            >
              {stopBusy ? "…" : "CONFIRM: KILL ALL"}
            </button>
            <button
              type="button"
              onClick={onCancelArm}
              disabled={stopBusy}
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
      {rearmResult && !rearmError && (
        <p
          className="text-[10px] text-muted-foreground"
          data-testid="text-rearm-result"
        >
          Re-armed to {rearmResult.mode.toUpperCase()} at {rearmResult.at}.
        </p>
      )}
    </div>
  );
}
