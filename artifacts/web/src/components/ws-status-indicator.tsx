import type { WSStatus } from "@/hooks/use-websocket";

interface Props {
  status: WSStatus;
  reconnectIn: number | null;
  label?: string;
  closeCode?: number | null;
}

export default function WsStatusIndicator({
  status,
  reconnectIn,
  label = "LIVE",
  closeCode = null,
}: Props) {
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
  return (
    <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest" data-testid="ws-status">
      <span
        className={`w-2 h-2 rounded-full ${dot} ${pulse ? "animate-pulse" : ""}`}
      />
      <span className="text-muted-foreground">{text}</span>
    </div>
  );
}
