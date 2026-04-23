import { useEffect, useState } from "react";
import type { WSStatus } from "@/hooks/use-websocket";

interface Props {
  status: WSStatus;
  reconnectIn: number | null;
  label?: string;
  closeCode?: number | null;
  /** Timestamp (ms) of the most recent successful background rotation.
   *  When this changes the badge briefly flashes "REFRESHED" so an
   *  operator watching the dashboard can see that the silent token
   *  swap actually fired. */
  lastRotationAt?: number | null;
  /** Cumulative count of successful background rotations. Rendered as
   *  a tooltip on the badge so it's inspectable without opening the
   *  console. */
  rotationCount?: number;
  /** Most recent rotation failure description (cleared on the next
   *  success). When set, the badge is annotated with a small "REFRESH
   *  FAILED" warning so the issue is visible before it degrades into
   *  a full disconnect. */
  lastRotationError?: string | null;
}

const REFRESH_FLASH_MS = 2500;

export default function WsStatusIndicator({
  status,
  reconnectIn,
  label = "LIVE",
  closeCode = null,
  lastRotationAt = null,
  rotationCount = 0,
  lastRotationError = null,
}: Props) {
  // Drive the brief "REFRESHED" pulse off lastRotationAt: when it
  // changes to a new timestamp, show the flash for a few seconds.
  const [flashing, setFlashing] = useState(false);
  useEffect(() => {
    if (!lastRotationAt) return;
    setFlashing(true);
    const t = setTimeout(() => setFlashing(false), REFRESH_FLASH_MS);
    return () => clearTimeout(t);
  }, [lastRotationAt]);

  let dot = "bg-muted-foreground";
  let text = "OFFLINE";
  let pulse = false;
  if (status === "open") {
    dot = "bg-success";
    pulse = true;
    text = label;
  } else if (status === "connecting") {
    dot = "bg-warning";
    pulse = true;
    text = "CONNECTING";
  } else if (status === "error") {
    dot = "bg-destructive";
    text = "ERROR";
  } else if (status === "closed") {
    dot = "bg-destructive";
    if (closeCode === 4401) text = "UNAUTHORIZED";
    else if (reconnectIn !== null) text = `RETRY ${reconnectIn}s`;
    else text = "DISCONNECTED";
  } else if (status === "unauthorized") {
    dot = "bg-destructive";
    text = reconnectIn !== null ? `AUTH RETRY ${reconnectIn}s` : "UNAUTHORIZED";
  } else if (status === "idle") {
    text = "STANDBY";
  }

  // While open, prefer the flash text over the steady label so
  // operators get a clear, non-alarming signal that a rotation
  // happened.  We deliberately keep the dot color the same so the
  // connection still reads as healthy.
  const showFlash = flashing && status === "open";
  const displayText = showFlash ? "REFRESHED" : text;
  const showWarning = !!lastRotationError && status === "open" && !showFlash;

  const tooltipParts: string[] = [];
  if (rotationCount > 0) {
    tooltipParts.push(
      `${rotationCount} background refresh${rotationCount === 1 ? "" : "es"}`,
    );
  }
  if (lastRotationError) {
    tooltipParts.push(`Last refresh failed: ${lastRotationError}`);
  }
  const tooltip = tooltipParts.length > 0 ? tooltipParts.join(" • ") : undefined;

  return (
    <div
      className="flex items-center gap-2 text-[10px] uppercase tracking-widest"
      data-testid="ws-status"
      title={tooltip}
      data-rotation-count={rotationCount}
      data-rotation-flash={showFlash ? "true" : "false"}
    >
      <span
        className={`w-2 h-2 rounded-full ${dot} ${
          pulse || showFlash ? "animate-pulse" : ""
        }`}
      />
      <span className={showFlash ? "text-success" : "text-muted-foreground"}>
        {displayText}
      </span>
      {showWarning && (
        <span
          className="text-warning"
          data-testid="ws-rotation-warning"
        >
          REFRESH FAILED
        </span>
      )}
    </div>
  );
}
