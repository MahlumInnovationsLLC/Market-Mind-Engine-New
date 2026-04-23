import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import React, { useEffect } from "react";
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { deepLinkFromNotificationData } from "@/lib/push";

type NotificationItem = {
  id: number;
  type: string;
  title: string;
  body: string;
  deep_link?: string | null;
  data?: Record<string, unknown> | null;
  created_at?: string | null;
  read_at?: string | null;
};

type NotificationsResponse = {
  items: NotificationItem[];
  unread_count: number;
};

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const now = Date.now();
  const diff = now - d.getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function badgeColor(type: string, colors: ReturnType<typeof useColors>): string {
  switch (type) {
    case "halt":
      return colors.destructive;
    case "live_trade":
      return colors.success;
    default:
      return colors.primary;
  }
}

export default function NotificationsScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const queryClient = useQueryClient();

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["me", "notifications"],
    queryFn: () =>
      apiFetch<NotificationsResponse>("/api/me/notifications?limit=50"),
    refetchInterval: 30_000,
  });

  const markRead = useMutation({
    mutationFn: (ids?: number[]) =>
      apiFetch<{ unread_count: number }>("/api/me/notifications/read", {
        method: "POST",
        body: ids && ids.length ? { ids } : {},
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["me", "notifications"] });
    },
  });

  const items = data?.items ?? [];
  const unread = data?.unread_count ?? 0;

  // Auto-mark all as read when the user opens the screen and there are
  // unread items. Lightweight POST, no UI surface needed.
  useEffect(() => {
    if (unread > 0 && !markRead.isPending) {
      markRead.mutate(undefined);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unread]);

  if (isLoading) {
    return (
      <View style={[styles.center, { backgroundColor: colors.background }]}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }

  if (isError) {
    const status = (error as { status?: number } | null)?.status;
    return (
      <View
        style={[
          styles.center,
          { backgroundColor: colors.background, padding: 24 },
        ]}
      >
        <Text style={[styles.muted, { color: colors.destructive }]}>
          {status === 403
            ? "Awaiting operator approval"
            : `Failed to load (HTTP ${status ?? "?"})`}
        </Text>
      </View>
    );
  }

  return (
    <FlatList
      style={{ backgroundColor: colors.background }}
      contentContainerStyle={{
        padding: 16,
        paddingBottom: insets.bottom + 100,
        gap: 8,
        flexGrow: 1,
      }}
      data={items}
      keyExtractor={(n) => String(n.id)}
      refreshControl={
        <RefreshControl
          refreshing={isFetching && !isLoading}
          onRefresh={refetch}
          tintColor={colors.primary}
        />
      }
      ListEmptyComponent={
        <View style={styles.empty}>
          <Text style={[styles.muted, { color: colors.mutedForeground }]}>
            No notifications yet
          </Text>
          <Text
            style={[
              styles.mutedSmall,
              { color: colors.mutedForeground, marginTop: 6 },
            ]}
          >
            Live trades and trading halts will appear here.
          </Text>
        </View>
      }
      renderItem={({ item }) => {
        const link =
          item.deep_link ??
          deepLinkFromNotificationData(
            (item.data ?? null) as Record<string, unknown> | null,
          );
        const unreadRow = !item.read_at;
        return (
          <Pressable
            onPress={() => {
              if (unreadRow) markRead.mutate([item.id]);
              if (link) router.push(link as never);
            }}
            testID={`notification-${item.id}`}
            style={[
              styles.row,
              {
                backgroundColor: colors.card,
                borderColor: unreadRow ? colors.primary : colors.border,
              },
            ]}
          >
            <View style={styles.rowHead}>
              <View style={styles.rowHeadLeft}>
                <View
                  style={[
                    styles.dot,
                    { backgroundColor: badgeColor(item.type, colors) },
                  ]}
                />
                <Text
                  style={[styles.title, { color: colors.foreground }]}
                  numberOfLines={1}
                >
                  {item.title}
                </Text>
              </View>
              <Text
                style={[styles.timestamp, { color: colors.mutedForeground }]}
              >
                {fmtTime(item.created_at)}
              </Text>
            </View>
            <Text
              style={[styles.body, { color: colors.mutedForeground }]}
              numberOfLines={3}
            >
              {item.body}
            </Text>
            <View style={styles.metaRow}>
              <Text
                style={[
                  styles.tag,
                  {
                    color: badgeColor(item.type, colors),
                    borderColor: colors.border,
                  },
                ]}
              >
                {item.type.replace(/_/g, " ").toUpperCase()}
              </Text>
              {link ? (
                <Text style={[styles.link, { color: colors.primary }]}>
                  OPEN →
                </Text>
              ) : null}
            </View>
          </Pressable>
        );
      }}
    />
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  muted: { fontSize: 13, fontFamily: "Inter_400Regular" },
  mutedSmall: { fontSize: 11, fontFamily: "Inter_400Regular" },
  empty: { paddingVertical: 60, alignItems: "center" },
  row: {
    borderWidth: 1,
    padding: 12,
    gap: 6,
  },
  rowHead: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  rowHeadLeft: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    flex: 1,
    marginRight: 8,
  },
  dot: { width: 8, height: 8, borderRadius: 4 },
  title: { fontFamily: "Inter_600SemiBold", fontSize: 13, flex: 1 },
  timestamp: { fontSize: 11, fontFamily: "Inter_400Regular" },
  body: { fontSize: 12, fontFamily: "Inter_400Regular", lineHeight: 17 },
  metaRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
  },
  tag: {
    fontSize: 10,
    letterSpacing: 1,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderWidth: 1,
    fontFamily: "Inter_600SemiBold",
  },
  link: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
});
