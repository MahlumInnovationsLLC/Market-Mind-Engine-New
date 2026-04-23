import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import {
  useMobileWebSocket,
  type WSStatus,
} from "@/lib/use-websocket";

type Listener = (event: Record<string, unknown>) => void;

interface PipelineSocketContextValue {
  status: WSStatus;
  reconnectIn: number | null;
  closeCode: number | null;
  reconnectNow: () => void;
  subscribe: (handler: Listener) => () => void;
}

const PipelineSocketContext = createContext<PipelineSocketContextValue | null>(
  null,
);

interface Props {
  children: ReactNode;
  enabled?: boolean;
}

/**
 * Mobile WebSocket provider — mirrors the web shell's
 * `<WebSocketProvider>`. One connection to /api/ws/pipeline, fan-out
 * to subscribers via context. Disabled until the user signs in
 * (driven by the `enabled` prop from the auth gate in _layout.tsx).
 */
export function WebSocketProvider({ children, enabled = true }: Props) {
  const listenersRef = useRef<Set<Listener>>(new Set());

  const onMessage = useCallback((event: Record<string, unknown>) => {
    listenersRef.current.forEach((handler) => {
      try {
        handler(event);
      } catch {
        // ignore per-handler errors
      }
    });
  }, []);

  const ws = useMobileWebSocket({
    path: "/api/ws/pipeline",
    onMessage,
    enabled,
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
      reconnectNow: ws.reconnectNow,
      subscribe,
    }),
    [
      ws.status,
      ws.reconnectIn,
      ws.closeCode,
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
    // Soft-fall back so screens rendered before the provider mounts
    // (e.g. unauthenticated splash) don't crash.
    return {
      status: "idle",
      reconnectIn: null,
      closeCode: null,
      reconnectNow: () => {},
      subscribe: () => () => {},
    };
  }
  return ctx;
}

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
