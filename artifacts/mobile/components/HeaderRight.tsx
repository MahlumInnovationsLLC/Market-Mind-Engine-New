import { Feather } from "@expo/vector-icons";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import React from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import {
  useListModelHealthAlerts,
  getListModelHealthAlertsQueryKey,
} from "@workspace/api-client-react";
import { useColors } from "@/hooks/useColors";
import { ApiError, apiFetch } from "@/lib/api";
import { usePipelineSocket } from "@/components/realtime/WebSocketProvider";

type LiveExecuteState = {
  enabled: boolean;
  effective_mode: string;
};

type Overview = {
  latest_cycle_at?: string | null;
};

/**
 * Sticky header content shown across every operator tab.
 *
 * Mirrors the web `<Header>` chrome:
 *  - Trading-mode pill (PAPER / LIVE / HALT, color-coded)
 *  - Alert bell with unread badge (placeholder until #236 wires the
 *    realtime alert stream)
 *  - Connection dot — green when the dashboard query is succeeding,
 *    red when it's failing, amber while reconnecting
 */
const ALERTS_PARAMS = { ack: false, limit: 50 } as const;

export function HeaderRight() {
  const c = useColors();
  const router = useRouter();
  const ws = usePipelineSocket();

  const alertsQuery = useListModelHealthAlerts(ALERTS_PARAMS, {
    query: {
      queryKey: getListModelHealthAlertsQueryKey(ALERTS_PARAMS),
      staleTime: 30_000,
      refetchInterval: 60_000,
    },
  });

  const live = useQuery({
    queryKey: ["orchestrator", "live-execute"],
    queryFn: () => apiFetch<LiveExecuteState>("/api/orchestrator/live-execute"),
    refetchInterval: 30_000,
    retry: 0,
  });

  // Use the dashboard overview as a cheap connection probe — if it's
  // failing, the operator wants to know immediately.
  const overview = useQuery({
    queryKey: ["dashboard", "overview"],
    queryFn: () => apiFetch<Overview>("/api/dashboard/overview"),
    refetchInterval: 30_000,
    retry: 0,
  });

  // Resolve trading mode. If the live-execute endpoint isn't available
  // (403 for non-Elite) we still show a neutral PAPER pill so the
  // operator always sees the mode badge.
  const liveErr = live.error as ApiError | undefined;
  const mode = (() => {
    if (live.data) return (live.data.effective_mode || "paper").toUpperCase();
    if (liveErr?.status === 403 || liveErr?.status === 401) return "PAPER";
    if (live.isLoading) return "—";
    return "PAPER";
  })();
  const modeColor =
    mode === "LIVE"
      ? c.destructive
      : mode === "HALT"
        ? c.destructive
        : c.mutedForeground;
  const modeBg = mode === "LIVE" ? c.destructive + "22" : "transparent";

  // Connection dot — prefer the shared pipeline WS state when available
  // since that's the realtime feed everything else hangs off; fall back
  // to the dashboard query as a slow REST probe.
  const connStatus: "ok" | "warn" | "err" = (() => {
    if (ws.status === "open") return "ok";
    if (ws.status === "connecting" || ws.status === "idle") return "warn";
    if (ws.status === "unauthorized") return "warn";
    if (overview.isError) {
      const s = (overview.error as ApiError | undefined)?.status;
      if (s === 401 || s === 403) return "warn";
      return "err";
    }
    if (overview.isLoading) return "warn";
    return "err";
  })();
  const dotColor =
    connStatus === "ok"
      ? c.success
      : connStatus === "warn"
        ? c.warning
        : c.destructive;

  // Alert bell badge — wired to the same /api/health/model-alerts feed
  // the web shell uses. Tapping the bell jumps to the settings screen
  // where the operator can review + acknowledge them.
  const unreadAlerts = alertsQuery.data?.items?.length ?? 0;

  return (
    <View style={styles.row}>
      <View
        style={[
          styles.pill,
          { borderColor: modeColor, backgroundColor: modeBg },
        ]}
        testID="header-mode-pill"
      >
        <View
          style={[styles.pillDot, { backgroundColor: modeColor }]}
        />
        <Text style={[styles.pillText, { color: modeColor }]}>{mode}</Text>
      </View>

      <Pressable
        accessibilityLabel={
          unreadAlerts > 0 ? `${unreadAlerts} unread alerts` : "Alerts"
        }
        testID="header-alert-bell"
        onPress={() => router.push("/(tabs)/settings" as never)}
        style={({ pressed }) => [
          styles.iconBtn,
          { borderColor: c.border, opacity: pressed ? 0.6 : 1 },
        ]}
      >
        <Feather name="bell" size={16} color={c.foreground} />
        {unreadAlerts > 0 ? (
          <View
            style={[
              styles.badge,
              { backgroundColor: c.destructive, borderColor: c.background },
            ]}
          >
            <Text
              style={[styles.badgeText, { color: c.destructiveForeground }]}
            >
              {unreadAlerts > 9 ? "9+" : unreadAlerts}
            </Text>
          </View>
        ) : null}
      </Pressable>

      <View
        style={[styles.dot, { backgroundColor: dotColor }]}
        testID="header-connection-dot"
      />
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingRight: 12,
  },
  pill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderWidth: 1,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 999,
  },
  pillDot: { width: 6, height: 6, borderRadius: 3 },
  pillText: {
    fontSize: 10,
    letterSpacing: 1.2,
    fontFamily: "Inter_700Bold",
  },
  iconBtn: {
    borderWidth: 1,
    width: 30,
    height: 30,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: 999,
    position: "relative",
  },
  badge: {
    position: "absolute",
    top: -4,
    right: -4,
    minWidth: 16,
    height: 16,
    paddingHorizontal: 3,
    borderRadius: 8,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: {
    fontSize: 9,
    fontFamily: "Inter_700Bold",
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
});
