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
  const apiBase = (import.meta.env.VITE_API_URL ?? "").toString().trim().replace(/\/+$/, "");

  // Same-origin fallback — used when `VITE_API_URL` is empty, malformed,
  // or carries a protocol other than http/https. Mirrors the
  // `resolveApiUrl` allowlist so a typo'd env var never silently routes
  // a WebSocket (which inherits the same auth boundary as REST) to an
  // unintended origin.
  const sameOrigin = (): string => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}${basePath}${cleanPath}`;
  };

  let url: string;
  if (apiBase) {
    try {
      const parsed = new URL(apiBase);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        // Mirrors `resolveApiUrl` — only http/https are valid backend
        // origins for the WebSocket upgrade. Anything else (file:,
        // javascript:, ftp:, …) falls back to same-origin.
        url = sameOrigin();
      } else {
        const wsProto = parsed.protocol === "https:" ? "wss:" : "ws:";
        const apiPathPrefix = parsed.pathname.replace(/\/+$/, "");
        url = `${wsProto}//${parsed.host}${apiPathPrefix}${cleanPath}`;
      }
    } catch {
      url = sameOrigin();
    }
  } else {
    url = sameOrigin();
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
// How long to wait for the server's `{"type":"hello"}` frame on a
// rotation replacement socket before declaring the handshake failed.
// The server sends the hello immediately after subscribing to the
// hub, so a healthy LAN round-trip lands well under 1s.  5s leaves
// generous slack for slow networks while still bounding the time we
// hold an extra socket open per rotation.
const ROTATION_ACK_TIMEOUT_MS = 5_000;

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
  // In-flight rotation replacement socket + ack timer.  Tracked in
  // refs (not just inside the `rotate()` closure) so that unmount,
  // disable, and reconnect cleanups can force-close an opened-but-
  // unacked replacement instead of leaking it.  See task #926.
  const pendingRotationWsRef = useRef<WebSocket | null>(null);
  const pendingAckTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
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

  // Abort an in-flight rotation attempt: clear the ack-timeout timer
  // and force-close the replacement socket if it was already opened
  // but hasn't acked / swapped in.  Safe to call when no rotation is
  // pending.  Caller is responsible for any failure-accounting it
  // wants to record before calling (cleanup paths intentionally do
  // not record a failure — the socket is being torn down deliberately).
  const abortPendingRotation = useCallback(() => {
    if (pendingAckTimerRef.current) {
      clearTimeout(pendingAckTimerRef.current);
      pendingAckTimerRef.current = null;
    }
    const pending = pendingRotationWsRef.current;
    if (pending) {
      pendingRotationWsRef.current = null;
      // Detach handlers first so the imminent close doesn't try to
      // run noteRotationFailure / scheduleRetry on a hook that's
      // either unmounting or about to start a fresh attempt.
      pending.onopen = null;
      pending.onmessage = null;
      pending.onerror = null;
      pending.onclose = null;
      try {
        pending.close();
      } catch {
        // ignore
      }
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
        // `hello` is the server-side rotation handshake frame (see
        // `ws_send_hello` in api-server/auth.py).  On the initial
        // connection it carries no user-visible payload — swallow it
        // so it never lands in the events buffer or fires onMessage.
        if (payload.type === "hello") {
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
    // Drop any in-flight rotation replacement — we're about to do a
    // hard reconnect, so its eventual onclose must not retry against
    // a stale generation.
    abortPendingRotation();
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
  }, [path, getToken, teardownTimers, clearRotateTimer, abortPendingRotation, detach, attachHandlers]);

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
    // A prior rotation attempt may still have a replacement socket
    // hanging around (e.g. opened-but-unacked, then we retried after
    // ack-timeout).  Drop it before starting a fresh attempt so we
    // don't end up holding two competing replacements.
    abortPendingRotation();

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
    pendingRotationWsRef.current = nextWs;

    const newGen = generationRef.current + 1;
    let swapped = false;
    // Single-shot guard for failure accounting: the ack-timeout path
    // closes the socket itself, which fires onclose, which would
    // otherwise call `noteRotationFailure` a second time and
    // double-increment `rotationFailureCount` / clobber
    // `lastRotationError`.
    let failed = false;
    const markFailed = (reason: string) => {
      if (failed || swapped) return;
      failed = true;
      noteRotationFailure(reason);
    };

    const clearAckTimer = () => {
      if (pendingAckTimerRef.current) {
        clearTimeout(pendingAckTimerRef.current);
        pendingAckTimerRef.current = null;
      }
    };

    const scheduleRetry = () => {
      if (stoppedRef.current) return;
      if (wsRef.current === oldWs && oldWs.readyState === WebSocket.OPEN) {
        clearRotateTimer();
        rotateTimerRef.current = setTimeout(() => {
          void rotateRef.current();
        }, ROTATION_MIN_MS);
      }
    };

    // Promote the replacement socket to live: bump generation, detach
    // + close the old socket, rebind handlers, schedule next rotation.
    // Only invoked once we have proof the replacement is fully wired
    // through (i.e. a hello/event frame was received from the server).
    const performSwap = () => {
      if (swapped || stoppedRef.current) return;
      swapped = true;
      clearAckTimer();
      // Hand ownership off the "pending" slot — the replacement is
      // now the live socket and should be tracked via wsRef instead.
      if (pendingRotationWsRef.current === nextWs) {
        pendingRotationWsRef.current = null;
      }
      generationRef.current = newGen;
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
      // next rotation explicitly here.
      scheduleRotationRef.current(token);
    };

    nextWs.onopen = () => {
      if (stoppedRef.current) {
        try {
          nextWs.close();
        } catch {
          // ignore
        }
        return;
      }
      // DON'T swap yet — wait for the server's `{"type":"hello"}` ack
      // (or any other valid frame) so we know the connection survived
      // past the TCP/TLS handshake into the producing handler.
      // Without this, a load-balancer or proxy that kills the socket
      // immediately after upgrade would leave us with a closed
      // replacement AND a torn-down old socket — the exact race that
      // logged `code 1006` from production.
      clearAckTimer();
      pendingAckTimerRef.current = setTimeout(() => {
        pendingAckTimerRef.current = null;
        if (swapped || stoppedRef.current) return;
        markFailed(
          `replacement socket opened but did not ack within ${
            ROTATION_ACK_TIMEOUT_MS / 1000
          }s`,
        );
        try {
          nextWs.close();
        } catch {
          // ignore
        }
        scheduleRetry();
      }, ROTATION_ACK_TIMEOUT_MS);
    };

    nextWs.onmessage = (msg) => {
      if (swapped || stoppedRef.current) return;
      // Any well-formed JSON frame counts as the handshake ack — the
      // server sends `{"type":"hello"}` first, but a producer that
      // races ahead with a real event would also be valid evidence
      // the connection is healthy.  Malformed payloads are ignored so
      // a stray garbage byte can't trigger a premature swap.
      let payload: Record<string, unknown> | null = null;
      try {
        payload = JSON.parse((msg as MessageEvent).data as string);
      } catch {
        return;
      }
      if (!payload || typeof payload !== "object") return;
      performSwap();
      // If this frame is something the consumer cares about (i.e. not
      // the hello), forward it through the normal path so we don't
      // drop the very first event delivered immediately after swap.
      if (payload.type !== "hello") {
        setEvents((prev) => {
          const next = [...prev, payload!];
          return next.length > bufferSize
            ? next.slice(next.length - bufferSize)
            : next;
        });
        try {
          onMessageRef.current?.(payload);
        } catch {
          // swallow handler errors
        }
      }
    };
    nextWs.onerror = () => {
      // Suppress; rely on onclose to decide what to do.
    };
    nextWs.onclose = (ev) => {
      if (swapped) return; // Post-swap closes are handled by attachHandlers.
      clearAckTimer();
      if (pendingRotationWsRef.current === nextWs) {
        pendingRotationWsRef.current = null;
      }
      // Rotation attempt failed before swap.  Old socket is still
      // live; just retry rotation soon.  If the old socket also drops,
      // its onclose will trigger the normal reconnect path.
      if (stoppedRef.current) return;
      // `markFailed` is single-shot: if ack-timeout already recorded
      // the failure and then closed the socket itself, this onclose
      // won't double-count.
      markFailed(
        ev.code === 4401
          ? "replacement socket rejected (unauthorized)"
          : `replacement socket closed before swap (code ${ev.code || "n/a"})`,
      );
      scheduleRetry();
    };
  }, [path, bufferSize, getToken, abortPendingRotation, detach, attachHandlers, clearRotateTimer, noteRotationFailure]);

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
      // Drop any opened-but-unacked rotation replacement BEFORE we
      // bump the generation, so its detached handlers don't get a
      // chance to interact with the soon-to-be-stale state.
      abortPendingRotation();
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
      // Critical: an opened-but-unacked rotation replacement is held
      // in `pendingRotationWsRef`, NOT `wsRef`.  Without this call the
      // socket would leak across unmount (task #926).
      abortPendingRotation();
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
  }, [enabled, isSignedIn, connect, teardownTimers, clearRotateTimer, abortPendingRotation, detach]);

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
