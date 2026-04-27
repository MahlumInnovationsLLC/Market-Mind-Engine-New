import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import React, { useCallback, useEffect } from "react";
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import ReanimatedSwipeable from "react-native-gesture-handler/ReanimatedSwipeable";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { deepLinkFromNotificationData } from "@/lib/push";
import { isInQuietHours, offsetForZone } from "@/lib/timezones";

type NotificationItem = {
  id: number;
  type: string;
  title: string;
  body: string;
  deep_link?: string | null;
  data?: Record<string, unknown> | null;
  created_at?: string | null;
  read_at?: string | null;
  // Snapshot of the recipient's quiet-hours window at dispatch time
  // (task #630). All three are nullable: legacy rows that predate the
  // column carry no zone, and any row whose recipient hadn't
  // configured DND simply has nothing to annotate against.
  quiet_hours_tz?: string | null;
  quiet_hours_start_hour?: number | null;
  quiet_hours_end_hour?: number | null;
};

type NotificationsResponse = {
  items: NotificationItem[];
  unread_count: number;
};

/**
 * "(Europe/London, GMT+1)"-style suffix for the quiet-hours annotation
 * shown under muted history rows. Drops the offset gracefully when the
 * runtime can't resolve it.
 */
function quietZoneSuffix(zone: string): string {
  const off = offsetForZone(zone);
  return off ? `${zone} (${off})` : zone;
}

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

  const dismissOne = useMutation({
    mutationFn: (id: number) =>
      apiFetch<{ unread_count: number }>(`/api/me/notifications/${id}`, {
        method: "DELETE",
      }),
    // Optimistic remove so the row vanishes immediately when the swipe
    // commits. The server roundtrip then reconciles unread counts on
    // success or restores the cache on error.
    onMutate: async (id: number) => {
      await queryClient.cancelQueries({ queryKey: ["me", "notifications"] });
      const previous = queryClient.getQueryData<NotificationsResponse>([
        "me",
        "notifications",
      ]);
      if (previous) {
        const removed = previous.items.find((n) => n.id === id);
        const wasUnread = removed && !removed.read_at;
        queryClient.setQueryData<NotificationsResponse>(
          ["me", "notifications"],
          {
            items: previous.items.filter((n) => n.id !== id),
            unread_count: Math.max(
              0,
              (previous.unread_count ?? 0) - (wasUnread ? 1 : 0),
            ),
          },
        );
      }
      return { previous };
    },
    onError: (_err, _id, ctx) => {
      if (ctx?.previous) {
        queryClient.setQueryData(["me", "notifications"], ctx.previous);
      }
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["me", "notifications"] });
    },
  });

  const clearAll = useMutation({
    mutationFn: () =>
      apiFetch<{ deleted: number; unread_count: number }>(
        "/api/me/notifications",
        { method: "DELETE" },
      ),
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ["me", "notifications"] });
      const previous = queryClient.getQueryData<NotificationsResponse>([
        "me",
        "notifications",
      ]);
      queryClient.setQueryData<NotificationsResponse>(
        ["me", "notifications"],
        { items: [], unread_count: 0 },
      );
      return { previous };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.previous) {
        queryClient.setQueryData(["me", "notifications"], ctx.previous);
      }
    },
    onSettled: () => {
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

  const onConfirmClearAll = useCallback(() => {
    if (!items.length || clearAll.isPending) return;
    Alert.alert(
      "Clear all notifications?",
      "This permanently removes every notification from this feed.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Clear all",
          style: "destructive",
          onPress: () => clearAll.mutate(),
        },
      ],
    );
  }, [items.length, clearAll]);

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

  const renderRightActions = () => (
    <View
      style={[
        styles.swipeAction,
        { backgroundColor: colors.destructive },
      ]}
    >
      <Text style={styles.swipeActionText}>DISMISS</Text>
    </View>
  );

  return (
    <FlatList
      style={{ backgroundColor: colors.background }}
      contentContainerStyle={{
        paddingHorizontal: 16,
        paddingTop: 12,
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
      ListHeaderComponent={
        <View
          style={[
            styles.header,
            { borderBottomColor: colors.border },
          ]}
        >
          <Text style={[styles.headerLabel, { color: colors.mutedForeground }]}>
            {items.length === 0
              ? "EMPTY"
              : `${items.length} ${items.length === 1 ? "ALERT" : "ALERTS"}`}
          </Text>
          <Pressable
            onPress={onConfirmClearAll}
            disabled={items.length === 0 || clearAll.isPending}
            testID="clear-all-notifications"
            hitSlop={8}
            style={({ pressed }) => [
              styles.clearAllBtn,
              {
                borderColor:
                  items.length === 0
                    ? colors.border
                    : colors.destructive,
                opacity: items.length === 0 || clearAll.isPending
                  ? 0.4
                  : pressed ? 0.6 : 1,
              },
            ]}
          >
            <Text
              style={[
                styles.clearAllText,
                {
                  color:
                    items.length === 0
                      ? colors.mutedForeground
                      : colors.destructive,
                },
              ]}
            >
              CLEAR ALL
            </Text>
          </Pressable>
        </View>
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
        // Annotate rows that landed inside the recipient's quiet
        // hours window at dispatch time so it's obvious why a push
        // was silent (task #526). The window is read from the
        // per-row snapshot persisted by the API (task #630) instead
        // of recomputed against current prefs, so editing the saved
        // zone or hours later doesn't rewrite history. Falls back to
        // no annotation when the timestamp is unparseable, the row
        // predates the snapshot column, or the recipient hadn't
        // configured a window at the time.
        const itemTz = item.quiet_hours_tz ?? null;
        const createdAt = item.created_at ? new Date(item.created_at) : null;
        const quietHit =
          createdAt && !Number.isNaN(createdAt.getTime()) && itemTz
            ? isInQuietHours(
                item.quiet_hours_start_hour ?? null,
                item.quiet_hours_end_hour ?? null,
                itemTz,
                createdAt,
              )
            : false;
        return (
          <ReanimatedSwipeable
            friction={2}
            rightThreshold={48}
            overshootRight={false}
            renderRightActions={renderRightActions}
            // Fires once the swipe crosses the open threshold. The
            // optimistic mutation immediately drops the row from the
            // cache, so by the time the animation settles the FlatList
            // has already removed this Swipeable from the tree — no
            // need to call `close()` ourselves.
            onSwipeableOpen={() => {
              dismissOne.mutate(item.id);
            }}
            testID={`notification-swipe-${item.id}`}
          >
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
              {quietHit ? (
                <Text
                  testID={`notification-quiet-${item.id}`}
                  style={[
                    styles.quietNote,
                    { color: colors.mutedForeground },
                  ]}
                >
                  Muted by quiet hours · {quietZoneSuffix(itemTz!)}
                </Text>
              ) : null}
            </Pressable>
          </ReanimatedSwipeable>
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
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingBottom: 10,
    marginBottom: 4,
    borderBottomWidth: 1,
  },
  headerLabel: {
    fontSize: 11,
    letterSpacing: 1.5,
    fontFamily: "Inter_600SemiBold",
  },
  clearAllBtn: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderWidth: 1,
  },
  clearAllText: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
  row: {
    borderWidth: 1,
    padding: 12,
    gap: 6,
  },
  swipeAction: {
    justifyContent: "center",
    alignItems: "flex-end",
    paddingHorizontal: 20,
    width: "100%",
  },
  swipeActionText: {
    color: "#fff",
    fontFamily: "Inter_700Bold",
    fontSize: 12,
    letterSpacing: 1.5,
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
  quietNote: {
    fontSize: 10,
    fontFamily: "Inter_400Regular",
    fontStyle: "italic",
    marginTop: 2,
  },
});
