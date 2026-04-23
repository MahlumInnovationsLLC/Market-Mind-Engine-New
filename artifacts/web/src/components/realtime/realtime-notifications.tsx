import { useEffect, useRef } from "react";
import {
  useListModelHealthAlerts,
  getListModelHealthAlertsQueryKey,
  type ModelHealthAlert,
} from "@workspace/api-client-react";
import { toast } from "@/hooks/use-toast";
import { usePipelineSubscription } from "./websocket-provider";

const ALERTS_PARAMS = { ack: false, limit: 50 } as const;

function severityVariant(
  severity?: string | null,
): "default" | "destructive" {
  const s = (severity ?? "").toLowerCase();
  if (s === "critical" || s === "high") return "destructive";
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

/**
 * Bridge component mounted once near the shell root. It surfaces two
 * realtime streams as toasts:
 *
 *  1. **New model-health alerts** — polls /api/health/model-alerts and
 *     fires a toast for any alert id we haven't seen before. Tied to
 *     the same query the bell uses, so cache invalidation by ack
 *     keeps both in sync.
 *
 *  2. **Live trade fills** — subscribes to the shared pipeline
 *     WebSocket. Frames where a candidate was actually placed (or any
 *     explicit `trade_executed` event) raise a toast so the operator
 *     gets immediate feedback when the bot trades on their behalf.
 *
 * Renders nothing — purely a side-effect component.
 */
export function RealtimeNotifications() {
  // ─── 1. Alert toasts ────────────────────────────────────────────
  const seenAlertIdsRef = useRef<Set<number> | null>(null);
  const alerts = useListModelHealthAlerts(ALERTS_PARAMS, {
    query: {
      queryKey: getListModelHealthAlertsQueryKey(ALERTS_PARAMS),
      staleTime: 30_000,
      refetchInterval: 30_000,
    },
  });

  useEffect(() => {
    const items = alerts.data?.items;
    if (!items) return;
    // Seed on first successful response so we don't blast a toast for
    // every existing alert when the page first loads.
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

  // ─── 2. Trade-fill toasts ───────────────────────────────────────
  // Bounded LRU of stable identifiers we've already toasted. Only
  // server-issued IDs/sequences are accepted as dedupe keys — we never
  // fall back to Date.now() (which would defeat dedupe) or a key
  // composed solely of ticker/side/qty/price (which would suppress a
  // legitimate second fill at the same price).
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

    // Direct executor frame on the bus.
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
      // Require *something* server-issued to dedupe by; ignore frame
      // entirely if neither is present rather than blast duplicate
      // toasts.
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
      });
      return;
    }

    // candidate_complete frames also carry a placement payload when the
    // gate-stack passes and the order actually lands at Kalshi.
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
      if (!stableId) return; // require a server-issued id to dedupe
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
      });
    }
  });

  return null;
}
