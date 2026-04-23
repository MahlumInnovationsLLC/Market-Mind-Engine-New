import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, type AppStateStatus } from "react-native";

const DOMAIN = process.env.EXPO_PUBLIC_DOMAIN;

let _tokenGetter: (() => Promise<string | null>) | null = null;

/** Wired from app/_layout.tsx alongside the API token getter so the WS
 *  hook can authenticate without depending on Clerk hooks directly
 *  (keeping it usable from anywhere in the tree). */
export function setWsTokenGetter(
  getter: (() => Promise<string | null>) | null,
): void {
  _tokenGetter = getter;
}

export type WSStatus =
  | "idle"
  | "connecting"
  | "open"
  | "closed"
  | "error"
  | "unauthorized";

export interface UseMobileWebSocketOptions {
  path: string;
  onMessage?: (event: Record<string, unknown>) => void;
  enabled?: boolean;
}

export interface UseMobileWebSocketResult {
  status: WSStatus;
  reconnectIn: number | null;
  closeCode: number | null;
  reconnectNow: () => void;
}

function backoffMs(attempt: number, unauthorized: boolean): number {
  if (unauthorized) {
    const seq = [15_000, 30_000, 60_000, 120_000];
    return seq[Math.min(attempt - 1, seq.length - 1)];
  }
  const base = Math.min(30_000, 1_000 * Math.pow(2, Math.min(attempt, 5)));
  return base + Math.floor(Math.random() * 500);
}

function buildWsUrl(path: string, token: string | null): string | null {
  if (!DOMAIN) return null;
  const proto = DOMAIN.startsWith("https") ? "wss" : "ws";
  const host = DOMAIN.replace(/^https?:\/\//, "").replace(/\/$/, "");
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  const url = `${proto}://${host}${cleanPath}`;
  return token ? `${url}?token=${encodeURIComponent(token)}` : url;
}

/**
 * React-Native equivalent of the web `useWebSocket` hook.
 *
 *  - Uses the global RN WebSocket (already polyfilled by React Native).
 *  - Pulls the auth token from the shared `setWsTokenGetter` so it
 *    works the same way as `apiFetch`.
 *  - Reconnects with exponential backoff (slower for auth failures).
 *  - Pauses when the app backgrounds, reconnects when it returns to
 *    the foreground (mobile-specific lifecycle).
 *  - Each connect() call gets a monotonically-increasing generation id;
 *    callbacks/closes from older sockets are ignored so a fast
 *    reconnectNow() or AppState transition can't flip status from a
 *    stale socket or schedule duplicate reconnect timers.
 *
 * Designed to be wrapped by the {@link WebSocketProvider} so multiple
 * subscribers share one connection.
 */
export function useMobileWebSocket({
  path,
  onMessage,
  enabled = true,
}: UseMobileWebSocketOptions): UseMobileWebSocketResult {
  const [status, setStatus] = useState<WSStatus>("idle");
  const [reconnectIn, setReconnectIn] = useState<number | null>(null);
  const [closeCode, setCloseCode] = useState<number | null>(null);

  const onMessageRef = useRef(onMessage);
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  const wsRef = useRef<WebSocket | null>(null);
  const genRef = useRef(0);
  const currentGenRef = useRef(0);
  const attemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);
  const appStateRef = useRef<AppStateStatus>(AppState.currentState);

  const clearTimer = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  // Detach handlers + close any existing socket without letting its
  // lingering events touch state. Bumps the generation counter so any
  // in-flight onclose/onerror from the old socket is ignored.
  const detachAndCloseCurrent = useCallback(() => {
    const sock = wsRef.current;
    wsRef.current = null;
    if (sock) {
      sock.onopen = null;
      sock.onmessage = null;
      sock.onerror = null;
      sock.onclose = null;
      try {
        sock.close();
      } catch {
        // ignore
      }
    }
  }, []);

  const scheduleReconnect = useCallback(
    (unauthorized: boolean, doConnect: () => void) => {
      if (stoppedRef.current) return;
      attemptRef.current += 1;
      const delay = backoffMs(attemptRef.current, unauthorized);
      setReconnectIn(delay);
      clearTimer();
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        doConnect();
      }, delay);
    },
    [clearTimer],
  );

  const connect = useCallback(async () => {
    if (stoppedRef.current) return;
    if (!enabled) return;
    if (appStateRef.current !== "active") return;

    clearTimer();
    detachAndCloseCurrent();

    const myGen = ++genRef.current;
    currentGenRef.current = myGen;

    setStatus("connecting");
    setReconnectIn(null);

    let token: string | null = null;
    try {
      token = _tokenGetter ? await _tokenGetter() : null;
    } catch {
      token = null;
    }

    // Bail if a newer connect superseded us while we awaited the token.
    if (myGen !== currentGenRef.current || stoppedRef.current) return;

    const url = buildWsUrl(path, token);
    if (!url) {
      setStatus("error");
      return;
    }

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch {
      setStatus("error");
      scheduleReconnect(false, () => void connect());
      return;
    }
    wsRef.current = socket;

    socket.onopen = () => {
      if (myGen !== currentGenRef.current) return;
      attemptRef.current = 0;
      setStatus("open");
      setCloseCode(null);
    };
    socket.onmessage = (msg: WebSocketMessageEvent) => {
      if (myGen !== currentGenRef.current) return;
      const raw = msg.data;
      if (typeof raw !== "string") return;
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") {
          onMessageRef.current?.(parsed as Record<string, unknown>);
        }
      } catch {
        // ignore non-JSON frames
      }
    };
    socket.onerror = () => {
      if (myGen !== currentGenRef.current) return;
      setStatus("error");
    };
    socket.onclose = (ev: WebSocketCloseEvent) => {
      // Stale-socket guard: only the active generation may flip
      // shared status / schedule reconnects.
      if (myGen !== currentGenRef.current) return;
      const code = ev?.code ?? null;
      setCloseCode(code);
      const unauthorized = code === 4401 || code === 4403;
      setStatus(unauthorized ? "unauthorized" : "closed");
      if (!stoppedRef.current && enabled) {
        scheduleReconnect(unauthorized, () => void connect());
      }
    };
  }, [path, enabled, clearTimer, detachAndCloseCurrent, scheduleReconnect]);

  const reconnectNow = useCallback(() => {
    attemptRef.current = 0;
    void connect();
  }, [connect]);

  // Initial connect + AppState lifecycle
  useEffect(() => {
    stoppedRef.current = false;
    void connect();
    const sub = AppState.addEventListener(
      "change",
      (next: AppStateStatus) => {
        const prev = appStateRef.current;
        appStateRef.current = next;
        if (next === "active" && prev !== "active") {
          attemptRef.current = 0;
          void connect();
        } else if (next !== "active" && prev === "active") {
          clearTimer();
          // Bump the generation so the old socket's onclose can't
          // schedule a phantom reconnect while we're backgrounded.
          currentGenRef.current = ++genRef.current;
          detachAndCloseCurrent();
          setStatus("closed");
        }
      },
    );
    return () => {
      stoppedRef.current = true;
      clearTimer();
      // Ensure no stale onclose from the active socket fires after unmount.
      currentGenRef.current = ++genRef.current;
      detachAndCloseCurrent();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, enabled]);

  return { status, reconnectIn, closeCode, reconnectNow };
}
