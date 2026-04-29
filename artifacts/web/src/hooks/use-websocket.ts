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
  /** Number of successful background token rotations since this hook
   *  mounted.  Useful as a debug counter — operators can watch this
   *  tick up to confirm rotations are still happening on long sessions. */
  rotationCount: number;
  /** Timestamp (ms) of the most recent successful background rotation,
   *  or null if none has occurred yet.  UI can use this to flash a
   *  brief acknowledgement on the connection badge. */
  lastRotationAt: number | null;
  /** Number of background rotation attempts that failed (e.g. token
   *  fetch threw, new socket couldn't be constructed, or the new
   *  socket closed before the swap).  Stays > 0 until the next
   *  successful rotation. */
  rotationFailureCount: number;
  /** Human-readable description of the most recent rotation failure,
   *  or null if the last rotation succeeded.  Surfaced so support /
   *  operators can tell what went wrong without digging through the
   *  raw browser console. */
  lastRotationError: string | null;
}

const basePath = import.meta.env.BASE_URL.replace(/\/$/, "");

/**
 * Build the absolute `ws(s)://` URL for a relative socket path.
 *
 * Mirrors `resolveApiUrl` in `lib/api.ts`: when the operator sets
 * `VITE_API_URL` (the Mac-Studio cutover knob) the WebSocket connects
 * to that origin with the protocol auto-upgraded http→ws / https→wss,
 * and the artifact `BASE_URL` prefix is dropped because the configured
 * backend owns its own routing. When `VITE_API_URL` is empty (default,
 * Replit deployment) we fall back to the same-origin behavior the app
 * has always had.
 *
 * Exported so unit tests can cover both branches without needing to
 * stand up a `useWebSocket` render harness.
 */
export function buildWsUrl(path: string, token: string | null): string {
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  const apiBase = (import.meta.env.VITE_API_URL ?? "").toString().replace(/\/$/, "");
  let url: string;
  if (apiBase) {
    try {
      const parsed = new URL(apiBase);
      const wsProto = parsed.protocol === "https:" ? "wss:" : "ws:";
      const apiPathPrefix = parsed.pathname.replace(/\/$/, "");
      url = `${wsProto}//${parsed.host}${apiPathPrefix}${cleanPath}`;
    } catch {
      // Malformed `VITE_API_URL` — never silently swap to a different
      // host. Fall back to the same-origin path so a typo'd env var
      // can't redirect every WebSocket to wherever the parser landed.
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      url = `${proto}//${window.location.host}${basePath}${cleanPath}`;
    }
  } else {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    url = `${proto}//${window.location.host}${basePath}${cleanPath}`;
  }
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

// Parse the `exp` claim (unix seconds) out of a JWT without verifying
// the signature.  Returns the expiry in milliseconds since epoch, or
// null if the token is missing/malformed/has no exp.  We only use this
// to schedule a *proactive* rotation — the server still re-verifies
// the new token, so a tampered exp can't extend a session.
function jwtExpMs(token: string | null): number | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length < 2) return null;
  try {
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const json = JSON.parse(atob(padded));
    if (typeof json.exp === "number" && isFinite(json.exp)) {
      return json.exp * 1000;
    }
  } catch {
    // ignore
  }
  return null;
}

// How long before the JWT's exp do we rotate?  Clerk session JWTs are
// typically valid for ~60s, so a 10s safety window leaves enough time
// for the new socket to open and swap in.  When we can't read exp,
// fall back to a fixed 50s rotation.
const ROTATION_SAFETY_MS = 10_000;
const ROTATION_FALLBACK_MS = 50_000;
const ROTATION_MIN_MS = 5_000;

function rotationDelayMs(token: string | null): number {
  const expMs = jwtExpMs(token);
  if (expMs == null) return ROTATION_FALLBACK_MS;
  const delta = expMs - Date.now() - ROTATION_SAFETY_MS;
  return Math.max(ROTATION_MIN_MS, delta);
}

export function useWebSocket(opts: UseWebSocketOptions): UseWebSocketResult {
  const { path, onMessage, enabled = true, bufferSize = 200 } = opts;
  const { isSignedIn, getToken } = useAuth();
  const [status, setStatus] = useState<WSStatus>("idle");
  const [events, setEvents] = useState<Record<string, unknown>[]>([]);
  const [reconnectIn, setReconnectIn] = useState<number | null>(null);
  const [closeCode, setCloseCode] = useState<number | null>(null);
  const [rotationCount, setRotationCount] = useState(0);
  const [lastRotationAt, setLastRotationAt] = useState<number | null>(null);
  const [rotationFailureCount, setRotationFailureCount] = useState(0);
  const [lastRotationError, setLastRotationError] = useState<string | null>(
    null,
  );

  // Refs for non-reactive bookkeeping.  Using a generation counter
  // makes stale callbacks (from an old socket) no-ops once we've
  // moved on to a new attempt.
  const wsRef = useRef<WebSocket | null>(null);
  const generationRef = useRef(0);
  const attemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const countdownTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const rotateTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
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

  const clearRotateTimer = useCallback(() => {
    if (rotateTimerRef.current) {
      clearTimeout(rotateTimerRef.current);
      rotateTimerRef.current = null;
    }
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

  // Forward declarations so the message/close handlers can schedule
  // rotation/reconnect without circular useCallback dependencies.
  const scheduleRotationRef = useRef<(token: string | null) => void>(() => {});
  const scheduleReconnectRef = useRef<(unauthorized: boolean) => void>(() => {});
  const rotateRef = useRef<() => Promise<void>>(async () => {});

  // Wire up the lifecycle handlers for a freshly created socket
  // belonging to `gen`.  The `isRotation` flag controls UI side
  // effects: a rotation should not flip status to "connecting" or
  // "closed" because the *previous* socket is still serving traffic
  // until this one opens.
  const attachHandlers = useCallback(
    (ws: WebSocket, gen: number, token: string | null, isRotation: boolean) => {
      ws.onopen = () => {
        if (gen !== generationRef.current) return;
        attemptRef.current = 0;
        lastUnauthorizedRef.current = false;
        setStatus("open");
        setCloseCode(null);
        teardownTimers();
        scheduleRotationRef.current(token);
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
        // Server signals auth failure with a structured frame just
        // before closing with 4401.  Surface it explicitly so the UI
        // can react.
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
        // During rotation, the old socket is still live; don't flip
        // status to "error" on its behalf.  The error will surface
        // via the close handler if it actually drops.
        if (!isRotation) setStatus("error");
      };
      ws.onclose = (ev) => {
        if (gen !== generationRef.current) return;
        wsRef.current = null;
        clearRotateTimer();
        setCloseCode(ev.code);
        const unauthorized =
          ev.code === 4401 || lastUnauthorizedRef.current;
        lastUnauthorizedRef.current = unauthorized;
        setStatus(unauthorized ? "unauthorized" : "closed");
        if (stoppedRef.current) return;
        scheduleReconnectRef.current(unauthorized);
      };
    },
    [bufferSize, clearRotateTimer, teardownTimers],
  );

  const connect = useCallback(async () => {
    if (stoppedRef.current) return;
    // Cancel any pending timers for the previous attempt.
    teardownTimers();
    clearRotateTimer();
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
      scheduleReconnectRef.current(false);
      return;
    }
    wsRef.current = ws;
    attachHandlers(ws, gen, token, false);
  }, [path, getToken, teardownTimers, clearRotateTimer, detach, attachHandlers]);

  // Open a fresh socket alongside the existing one and only swap when
  // the new one is open.  This is the "smart rotation" path that
  // avoids a visible disconnect: the old socket keeps serving events
  // until the moment we hand traffic over.
  const noteRotationFailure = useCallback((reason: string) => {
    setRotationFailureCount((n) => n + 1);
    setLastRotationError(reason);
    // console.warn so it's visible in browser devtools and any log
    // aggregator (Sentry breadcrumbs, LogRocket, etc.) that captures
    // warn-level entries — no need for the operator to be watching
    // the raw console.
    console.warn(`[ws] background rotation failed: ${reason}`);
  }, []);

  const rotate = useCallback(async () => {
    if (stoppedRef.current) return;
    const oldWs = wsRef.current;
    // Only rotate a fully-open socket.  If the current socket is
    // anything else, the regular reconnect path will handle it.
    if (!oldWs || oldWs.readyState !== WebSocket.OPEN) return;

    let token: string | null = null;
    let tokenError = false;
    try {
      token = (await getToken()) ?? null;
    } catch {
      token = null;
      tokenError = true;
    }
    if (stoppedRef.current) return;
    // If the current socket was closed/replaced while we were
    // awaiting the new token, abort: a more recent generation owns
    // the connection now.
    if (wsRef.current !== oldWs) return;
    if (tokenError) {
      noteRotationFailure("token fetch threw");
    }

    let nextWs: WebSocket;
    try {
      nextWs = new WebSocket(buildWsUrl(path, token));
    } catch {
      noteRotationFailure("failed to construct replacement socket");
      // Couldn't even construct a new socket; try again shortly.
      // The old socket is still live, so the user sees no disruption.
      clearRotateTimer();
      rotateTimerRef.current = setTimeout(() => {
        void rotateRef.current();
      }, ROTATION_MIN_MS);
      return;
    }

    const newGen = generationRef.current + 1;
    let swapped = false;

    nextWs.onopen = () => {
      if (stoppedRef.current) {
        try {
          nextWs.close();
        } catch {
          // ignore
        }
        return;
      }
      // Promote the new socket: bump generation so the old socket's
      // handlers (still bound to the previous gen) become no-ops, then
      // detach + close it without triggering the reconnect flow.
      generationRef.current = newGen;
      swapped = true;
      detach(oldWs);
      try {
        oldWs.close();
      } catch {
        // ignore
      }
      wsRef.current = nextWs;
      attemptRef.current = 0;
      lastUnauthorizedRef.current = false;
      setCloseCode(null);
      setStatus("open");
      // Surface a successful background refresh so the UI can flash
      // a brief acknowledgement and operators have an inspectable
      // counter to watch on long-lived sessions.
      setRotationCount((n) => n + 1);
      setLastRotationAt(Date.now());
      setRotationFailureCount(0);
      setLastRotationError(null);
      console.info("[ws] background rotation succeeded");
      // Re-bind handlers under the new generation so subsequent
      // messages/closes route through the same logic.
      attachHandlers(nextWs, newGen, token, false);
      // attachHandlers only schedules rotation from its onopen, which
      // won't fire again on this already-open socket — so kick off the
      // next rotation explicitly here. Otherwise long-lived sessions
      // would rotate exactly once and then drift toward expiry.
      scheduleRotationRef.current(token);
    };

    // Until swap, the new socket needs minimal handlers so we don't
    // disturb UI state for the still-live old socket.
    nextWs.onmessage = () => {
      // Discard messages until swap; they'd be duplicates of the
      // old socket anyway, since both authenticated as the same user.
    };
    nextWs.onerror = () => {
      // Suppress; rely on onclose to decide what to do.
    };
    nextWs.onclose = (ev) => {
      if (swapped) return; // Post-swap closes are handled by attachHandlers.
      // Rotation attempt failed before opening.  Old socket is still
      // live; just retry rotation soon.  If the old socket also drops,
      // its onclose will trigger the normal reconnect path.
      if (stoppedRef.current) return;
      noteRotationFailure(
        ev.code === 4401
          ? "replacement socket rejected (unauthorized)"
          : `replacement socket closed before swap (code ${ev.code || "n/a"})`,
      );
      if (wsRef.current === oldWs && oldWs.readyState === WebSocket.OPEN) {
        clearRotateTimer();
        rotateTimerRef.current = setTimeout(() => {
          void rotateRef.current();
        }, ROTATION_MIN_MS);
      }
    };
  }, [path, getToken, detach, attachHandlers, clearRotateTimer, noteRotationFailure]);

  const scheduleRotation = useCallback(
    (token: string | null) => {
      clearRotateTimer();
      const delay = rotationDelayMs(token);
      rotateTimerRef.current = setTimeout(() => {
        void rotateRef.current();
      }, delay);
    },
    [clearRotateTimer],
  );

  const scheduleReconnect = useCallback(
    (unauthorized: boolean) => {
      if (stoppedRef.current) return;
      teardownTimers();
      clearRotateTimer();
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
    [connect, teardownTimers, clearRotateTimer],
  );

  // Keep the forward-declared refs pointed at the latest closures so
  // handlers attached to long-lived sockets always invoke up-to-date
  // logic (e.g. after a deps change in `path`).
  useEffect(() => {
    scheduleRotationRef.current = scheduleRotation;
  }, [scheduleRotation]);
  useEffect(() => {
    scheduleReconnectRef.current = scheduleReconnect;
  }, [scheduleReconnect]);
  useEffect(() => {
    rotateRef.current = rotate;
  }, [rotate]);

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
      clearRotateTimer();
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
      clearRotateTimer();
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
  }, [enabled, isSignedIn, connect, teardownTimers, clearRotateTimer, detach]);

  const lastEvent = events.length > 0 ? events[events.length - 1] : null;

  return {
    status,
    events,
    lastEvent,
    reconnectIn,
    closeCode,
    reconnectNow,
    clear,
    rotationCount,
    lastRotationAt,
    rotationFailureCount,
    lastRotationError,
  };
}
