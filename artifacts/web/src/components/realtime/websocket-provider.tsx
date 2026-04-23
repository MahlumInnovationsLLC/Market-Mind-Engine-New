import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import { useWebSocket, type WSStatus } from "@/hooks/use-websocket";

type Listener = (event: Record<string, unknown>) => void;

interface PipelineSocketContextValue {
  status: WSStatus;
  reconnectIn: number | null;
  closeCode: number | null;
  lastEvent: Record<string, unknown> | null;
  reconnectNow: () => void;
  /** Subscribe to incoming pipeline events. Returns an unsubscribe fn. */
  subscribe: (handler: Listener) => () => void;
}

const PipelineSocketContext = createContext<PipelineSocketContextValue | null>(
  null,
);

interface Props {
  children: ReactNode;
  /** Cap on the rolling buffer kept by the underlying hook. Most consumers
   *  only care about lastEvent / live subscribe; small buffer is fine. */
  bufferSize?: number;
}

/**
 * Single shared WebSocket to `/api/ws/pipeline`. Mounted once near the
 * shell root so every page consumer (sidebar dot, pipeline tracker,
 * top-counters, recent-trades, model-health-alerts, realtime toaster)
 * shares one connection instead of opening their own. Reconnect /
 * backoff / token rotation is owned by `useWebSocket`.
 */
export function WebSocketProvider({ children, bufferSize = 50 }: Props) {
  const listenersRef = useRef<Set<Listener>>(new Set());

  const onMessage = useCallback((event: Record<string, unknown>) => {
    listenersRef.current.forEach((handler) => {
      try {
        handler(event);
      } catch {
        // swallow per-handler errors so one bad subscriber can't break others
      }
    });
  }, []);

  const ws = useWebSocket({
    path: "/api/ws/pipeline",
    onMessage,
    bufferSize,
  });

  const subscribe = useCallback((handler: Listener) => {
    listenersRef.current.add(handler);
    return () => {
      listenersRef.current.delete(handler);
    };
  }, []);

  const value = useMemo<PipelineSocketContextValue>(
    () => ({
      status: ws.status,
      reconnectIn: ws.reconnectIn,
      closeCode: ws.closeCode,
      lastEvent: ws.lastEvent,
      reconnectNow: ws.reconnectNow,
      subscribe,
    }),
    [
      ws.status,
      ws.reconnectIn,
      ws.closeCode,
      ws.lastEvent,
      ws.reconnectNow,
      subscribe,
    ],
  );

  return (
    <PipelineSocketContext.Provider value={value}>
      {children}
    </PipelineSocketContext.Provider>
  );
}

export function usePipelineSocket(): PipelineSocketContextValue {
  const ctx = useContext(PipelineSocketContext);
  if (!ctx) {
    throw new Error("usePipelineSocket must be used inside <WebSocketProvider>");
  }
  return ctx;
}

/**
 * Convenience hook: subscribe to incoming pipeline frames with a stable
 * cleanup. Mirrors the `useWebSocket({ onMessage })` ergonomics so
 * existing call-sites can be ported by swapping the import.
 */
export function usePipelineSubscription(handler: Listener): void {
  const { subscribe } = usePipelineSocket();
  const handlerRef = useRef(handler);
  useEffect(() => {
    handlerRef.current = handler;
  }, [handler]);
  useEffect(() => {
    return subscribe((event) => handlerRef.current(event));
  }, [subscribe]);
}
