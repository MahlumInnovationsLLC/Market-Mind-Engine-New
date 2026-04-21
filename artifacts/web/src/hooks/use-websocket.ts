import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/react";

export type WSStatus = "idle" | "connecting" | "open" | "closed" | "error" | "unauthorized";

export interface UseWebSocketOptions {
  path: string;
  onMessage?: (event: Record<string, unknown>) => void;
  enabled?: boolean;
  bufferSize?: number;
}

export interface UseWebSocketResult {
  status: WSStatus;
  events: Record<string, unknown>[];
  lastEvent: Record<string, unknown> | null;
  reconnectIn: number | null;
  closeCode: number | null;
  reconnectNow: () => void;
  clear: () => void;
}

const basePath = import.meta.env.BASE_URL.replace(/\/$/, "");

function buildWsUrl(path: string, token: string | null): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  const url = `${proto}//${window.location.host}${basePath}${cleanPath}`;
  return token ? `${url}?token=${encodeURIComponent(token)}` : url;
}

// Backoff schedules (ms): normal disconnect ramps fast (1→30s);
// auth failures back off slower so we keep retrying after Clerk
// rotates a stale token without hammering the server.
function backoffMs(attempt: number, unauthorized: boolean): number {
  if (unauthorized) {
    // 15s, 30s, 60s, 120s, then 120s.
    const seq = [15_000, 30_000, 60_000, 120_000];
    return seq[Math.min(attempt - 1, seq.length - 1)];
  }
  const base = Math.min(30_000, 1_000 * Math.pow(2, Math.min(attempt, 5)));
  return base + Math.floor(Math.random() * 500);
}

export function useWebSocket(opts: UseWebSocketOptions): UseWebSocketResult {
  const { path, onMessage, enabled = true, bufferSize = 200 } = opts;
  const { isSignedIn, getToken } = useAuth();
  const [status, setStatus] = useState<WSStatus>("idle");
  const [events, setEvents] = useState<Record<string, unknown>[]>([]);
  const [reconnectIn, setReconnectIn] = useState<number | null>(null);
  const [closeCode, setCloseCode] = useState<number | null>(null);

  // Refs for non-reactive bookkeeping.  Using a generation counter
  // makes stale callbacks (from an old socket) no-ops once we've
  // moved on to a new attempt.
  const wsRef = useRef<WebSocket | null>(null);
  const generationRef = useRef(0);
  const attemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const countdownTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const onMessageRef = useRef(onMessage);
  const stoppedRef = useRef(false);
  const lastUnauthorizedRef = useRef(false);

  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  const teardownTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (countdownTimerRef.current) {
      clearInterval(countdownTimerRef.current);
      countdownTimerRef.current = null;
    }
    setReconnectIn(null);
  }, []);

  // Detach handlers from a socket so its lifecycle events no longer
  // mutate state for a generation that has already been replaced.
  const detach = useCallback((ws: WebSocket | null) => {
    if (!ws) return;
    ws.onopen = null;
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;
  }, []);

  const connect = useCallback(async () => {
    if (stoppedRef.current) return;
    // Cancel any pending timers for the previous attempt.
    teardownTimers();
    // Detach + close any existing socket so its handlers can't race
    // with the new generation.
    if (wsRef.current) {
      detach(wsRef.current);
      try {
        wsRef.current.close();
      } catch {
        // ignore
      }
      wsRef.current = null;
    }
    const gen = ++generationRef.current;
    setStatus("connecting");
    let token: string | null = null;
    try {
      token = (await getToken()) ?? null;
    } catch {
      token = null;
    }
    if (stoppedRef.current || gen !== generationRef.current) return;
    const url = buildWsUrl(path, token);
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      scheduleReconnect(false);
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      if (gen !== generationRef.current) return;
      attemptRef.current = 0;
      lastUnauthorizedRef.current = false;
      setStatus("open");
      setCloseCode(null);
      teardownTimers();
    };
    ws.onmessage = (msg) => {
      if (gen !== generationRef.current) return;
      let payload: Record<string, unknown> | null = null;
      try {
        payload = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (!payload || typeof payload !== "object") return;
      // Server signals auth failure with a structured frame just before
      // closing with 4401.  Surface it explicitly so the UI can react.
      if (payload.type === "auth_error") {
        lastUnauthorizedRef.current = true;
        setStatus("unauthorized");
        return;
      }
      setEvents((prev) => {
        const next = [...prev, payload!];
        return next.length > bufferSize
          ? next.slice(next.length - bufferSize)
          : next;
      });
      try {
        onMessageRef.current?.(payload);
      } catch {
        // swallow handler errors so socket stays alive
      }
    };
    ws.onerror = () => {
      if (gen !== generationRef.current) return;
      setStatus("error");
    };
    ws.onclose = (ev) => {
      if (gen !== generationRef.current) return;
      wsRef.current = null;
      setCloseCode(ev.code);
      const unauthorized =
        ev.code === 4401 || lastUnauthorizedRef.current;
      lastUnauthorizedRef.current = unauthorized;
      setStatus(unauthorized ? "unauthorized" : "closed");
      if (stoppedRef.current) return;
      scheduleReconnect(unauthorized);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, getToken, bufferSize, teardownTimers, detach]);

  const scheduleReconnect = useCallback(
    (unauthorized: boolean) => {
      if (stoppedRef.current) return;
      teardownTimers();
      const attempt = attemptRef.current + 1;
      attemptRef.current = attempt;
      const delay = backoffMs(attempt, unauthorized);
      let remaining = Math.ceil(delay / 1000);
      setReconnectIn(remaining);
      countdownTimerRef.current = setInterval(() => {
        remaining -= 1;
        setReconnectIn(remaining > 0 ? remaining : 0);
      }, 1000);
      reconnectTimerRef.current = setTimeout(() => {
        teardownTimers();
        void connect();
      }, delay);
    },
    [connect, teardownTimers],
  );

  const reconnectNow = useCallback(() => {
    attemptRef.current = 0;
    lastUnauthorizedRef.current = false;
    void connect();
  }, [connect]);

  const clear = useCallback(() => setEvents([]), []);

  useEffect(() => {
    stoppedRef.current = !enabled || !isSignedIn;
    if (!enabled || !isSignedIn) {
      teardownTimers();
      // Bump generation so any in-flight onclose for the existing
      // socket cannot schedule another reconnect after we tear down.
      generationRef.current += 1;
      if (wsRef.current) {
        detach(wsRef.current);
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
      attemptRef.current = 0;
      lastUnauthorizedRef.current = false;
      setStatus("idle");
      return;
    }
    void connect();
    return () => {
      stoppedRef.current = true;
      teardownTimers();
      generationRef.current += 1;
      if (wsRef.current) {
        detach(wsRef.current);
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
  }, [enabled, isSignedIn, connect, teardownTimers, detach]);

  const lastEvent = events.length > 0 ? events[events.length - 1] : null;

  return { status, events, lastEvent, reconnectIn, closeCode, reconnectNow, clear };
}
