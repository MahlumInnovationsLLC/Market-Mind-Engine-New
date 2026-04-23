import { useEffect, useRef } from "react";
import {
  useListModelHealthAlerts,
  getListModelHealthAlertsQueryKey,
  type ModelHealthAlert,
} from "@workspace/api-client-react";
import { toast } from "./Toaster";
import { usePipelineSubscription } from "./WebSocketProvider";

const ALERTS_PARAMS = { ack: false, limit: 50 } as const;

function severityVariant(
  severity?: string | null,
): "default" | "destructive" | "warning" {
  const s = (severity ?? "").toLowerCase();
  if (s === "critical" || s === "high") return "destructive";
  if (s === "warning" || s === "medium") return "warning";
  return "default";
}

function pickString(
  obj: Record<string, unknown> | null | undefined,
  ...keys: string[]
): string | undefined {
  if (!obj) return undefined;
  for (const key of keys) {
    const v = obj[key];
    if (typeof v === "string" && v.length > 0) return v;
    if (typeof v === "number" && Number.isFinite(v)) return String(v);
  }
  return undefined;
}

function pickObject(
  obj: Record<string, unknown> | null | undefined,
  key: string,
): Record<string, unknown> | null {
  if (!obj) return null;
  const v = obj[key];
  if (v && typeof v === "object" && !Array.isArray(v)) {
    return v as Record<string, unknown>;
  }
  return null;
}

interface Props {
  enabled: boolean;
}

/**
 * Mobile equivalent of the web `RealtimeNotifications` bridge. Surfaces
 * new model-health alerts and live trade fills as native toasts via
 * the `<ToastHost>` mounted at the root.
 *
 * Disabled before sign-in to avoid hitting protected endpoints.
 */
export function RealtimeNotifications({ enabled }: Props) {
  const seenAlertIdsRef = useRef<Set<number> | null>(null);
  const alerts = useListModelHealthAlerts(ALERTS_PARAMS, {
    query: {
      queryKey: getListModelHealthAlertsQueryKey(ALERTS_PARAMS),
      staleTime: 30_000,
      refetchInterval: 60_000,
      enabled,
    },
  });

  useEffect(() => {
    const items = alerts.data?.items;
    if (!items) return;
    if (seenAlertIdsRef.current === null) {
      seenAlertIdsRef.current = new Set(items.map((a) => a.id));
      return;
    }
    const seen = seenAlertIdsRef.current;
    for (const alert of items) {
      if (seen.has(alert.id)) continue;
      seen.add(alert.id);
      const a: ModelHealthAlert = alert;
      const sev = (a.severity ?? "info").toUpperCase();
      const vert = a.vertical ? ` · ${a.vertical}` : "";
      toast({
        variant: severityVariant(a.severity),
        title: `${sev}${vert}`,
        description: a.message ?? a.alert_type ?? "Model health alert",
      });
    }
  }, [alerts.data]);

  // Bounded LRU dedupe — only stable server-issued ids are accepted
  // as keys. We deliberately do *not* fall back to Date.now() (which
  // would defeat dedupe) or to ticker/side/qty/price alone (which
  // would suppress a legitimate second fill at the same price).
  const seenTradeKeysRef = useRef<Set<string>>(new Set());
  const seenTradeQueueRef = useRef<string[]>([]);
  const SEEN_TRADE_CAP = 200;

  const rememberTradeKey = (key: string): boolean => {
    const seen = seenTradeKeysRef.current;
    if (seen.has(key)) return false;
    seen.add(key);
    seenTradeQueueRef.current.push(key);
    if (seenTradeQueueRef.current.length > SEEN_TRADE_CAP) {
      const evicted = seenTradeQueueRef.current.shift();
      if (evicted !== undefined) seen.delete(evicted);
    }
    return true;
  };

  usePipelineSubscription((event) => {
    const type = pickString(event, "type", "event");
    if (!type) return;

    if (type === "trade_executed" || type === "order_filled") {
      const stableId = pickString(
        event,
        "trade_id",
        "order_id",
        "fill_id",
        "id",
        "_seq",
        "seq",
      );
      const tsLike = pickString(event, "ts", "timestamp", "created_at");
      if (!stableId && !tsLike) return;
      const ticker =
        pickString(event, "ticker", "market_ticker", "market") ?? "—";
      const side = (pickString(event, "side") ?? "").toUpperCase();
      const key = `t|${stableId ?? `${ticker}|${side}|${tsLike}`}`;
      if (!rememberTradeKey(key)) return;
      const qty = pickString(event, "qty", "quantity", "size");
      const px =
        pickString(event, "fill_price", "price", "limit_price") ?? "—";
      toast({
        title: `FILL · ${side || "TRADE"}`,
        description: `${ticker}${qty ? ` × ${qty}` : ""} @ ${px}`,
        variant: "success",
      });
      return;
    }

    if (type === "candidate_complete") {
      const placement =
        pickObject(event, "placement") ?? pickObject(event, "trade");
      if (!placement) return;
      const placed =
        placement.placed === true ||
        placement.status === "placed" ||
        placement.status === "filled";
      if (!placed) return;
      const stableId =
        pickString(
          placement,
          "trade_id",
          "order_id",
          "fill_id",
          "id",
        ) ??
        pickString(event, "candidate_id", "cycle_id", "_seq", "seq");
      if (!stableId) return;
      const key = `c|${stableId}`;
      if (!rememberTradeKey(key)) return;
      const ticker =
        pickString(placement, "ticker", "market_ticker") ??
        pickString(event, "ticker", "market_ticker") ??
        "—";
      const side = (pickString(placement, "side") ?? "").toUpperCase();
      const qty = pickString(placement, "qty", "quantity", "size");
      const px = pickString(placement, "limit_price", "price") ?? "—";
      toast({
        title: `ORDER PLACED · ${side || "TRADE"}`,
        description: `${ticker}${qty ? ` × ${qty}` : ""} @ ${px}`,
        variant: "success",
      });
    }
  });

  return null;
}
