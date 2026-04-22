import { useEffect, useRef, useState } from "react";
import type { WSStatus } from "@/hooks/use-websocket";

type Event = Record<string, unknown>;

interface ConsoleTailProps {
  events: Event[];
  status: WSStatus;
  defaultOpen?: boolean;
  testId?: string;
}

function pickStr(o: Event, k: string): string | null {
  const v = o[k];
  return typeof v === "string" ? v : null;
}

function pickNum(o: Event, k: string): number | null {
  const v = o[k];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function fmtTime(s: string | null): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleTimeString([], {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return s;
  }
}

function eventTone(t: string | null): string {
  if (!t) return "text-muted-foreground";
  if (t.includes("error") || t.includes("blocked") || t.includes("reject")) return "text-loss";
  if (t.includes("complete") || t.includes("placed") || t.includes("settle")) return "text-success";
  if (t.includes("start") || t.includes("candidate")) return "text-primary";
  return "text-foreground";
}

function summarize(ev: Event): string {
  const type = pickStr(ev, "type") ?? "event";
  const market = pickStr(ev, "market_id") ?? pickStr(ev, "ticker");
  const cycle = pickStr(ev, "cycle_id");
  const stage = pickStr(ev, "stage");
  const reason = pickStr(ev, "reason") ?? pickStr(ev, "message");
  const prob = pickNum(ev, "model_prob") ?? pickNum(ev, "probability");
  const parts: string[] = [type.toUpperCase()];
  if (stage) parts.push(`stage=${stage}`);
  if (cycle) parts.push(`cycle=${cycle.slice(0, 8)}`);
  if (market) parts.push(`mkt=${market}`);
  if (prob !== null) parts.push(`p=${(prob * 100).toFixed(1)}%`);
  if (reason) parts.push(`· ${reason}`);
  return parts.join(" ");
}

export default function ConsoleTail({
  events,
  status,
  defaultOpen = false,
  testId = "console-tail",
}: ConsoleTailProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [follow, setFollow] = useState(true);
  const scrollerRef = useRef<HTMLDivElement | null>(null);

  // Show newest at the bottom (terminal style). Cap to last 200 to
  // keep the DOM bounded.
  const tail = events.slice(-200);

  useEffect(() => {
    if (!open || !follow) return;
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [tail.length, open, follow]);

  return (
    <section
      className="border border-border bg-card"
      data-testid={testId}
    >
      <header className="flex items-center justify-between border-b border-border bg-muted/20 px-3 py-2">
        <div className="flex items-center gap-2">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              status === "open"
                ? "bg-success animate-pulse"
                : status === "connecting"
                  ? "bg-primary animate-pulse"
                  : "bg-muted-foreground"
            }`}
          />
          <h2 className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
            CONSOLE_TAIL
          </h2>
          <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
            · {tail.length} buffered
          </span>
        </div>
        <div className="flex items-center gap-2">
          {open && (
            <button
              type="button"
              onClick={() => setFollow((v) => !v)}
              aria-pressed={follow}
              className={`border px-2 py-0.5 text-[10px] uppercase tracking-widest transition-colors ${
                follow
                  ? "border-primary text-primary"
                  : "border-border text-muted-foreground hover:text-primary hover:border-primary"
              }`}
              data-testid="button-console-follow"
              title={follow ? "Auto-follow on" : "Auto-follow paused"}
            >
              {follow ? "▮▮ Follow" : "▶ Follow"}
            </button>
          )}
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="border border-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground hover:border-primary hover:text-primary"
            aria-expanded={open}
            data-testid="button-console-toggle"
          >
            {open ? "Collapse" : "Expand"}
          </button>
        </div>
      </header>
      {open && (
        <div
          ref={scrollerRef}
          className="max-h-72 overflow-auto bg-background/40 p-2 font-mono text-[11px]"
          data-testid="console-tail-body"
        >
          {tail.length === 0 ? (
            <div className="p-6 text-center text-[10px] uppercase tracking-widest text-muted-foreground">
              {status === "open"
                ? "Waiting for pipeline events…"
                : "Stream not connected."}
            </div>
          ) : (
            <ul className="space-y-0.5">
              {tail.map((ev, i) => {
                const type = pickStr(ev, "type");
                const ts = pickStr(ev, "ts") ?? pickStr(ev, "timestamp");
                const seq = pickNum(ev, "_seq");
                return (
                  <li
                    key={`${seq ?? i}-${type ?? "e"}`}
                    className="flex gap-2"
                    data-testid="console-tail-line"
                  >
                    <span className="text-muted-foreground shrink-0 tabular-nums">
                      {fmtTime(ts)}
                    </span>
                    <span className={`${eventTone(type)} truncate`}>
                      {summarize(ev)}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
