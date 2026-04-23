import { useQuery } from "@tanstack/react-query";
import React, { useMemo, useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import DelayedSignalsBanner from "@/components/DelayedSignalsBanner";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";

type Trade = {
  id: number;
  trade_id?: string | null;
  market_id?: string | null;
  market_title?: string | null;
  vertical?: string | null;
  league?: string | null;
  side?: string | null;
  action?: string | null;
  quantity?: number | null;
  price?: number | null;
  pnl?: number | null;
  trading_mode?: string | null;
  executed_at?: string | null;
};

type TradeList = {
  items: Trade[];
  // Set by the backend for free-tier callers when the 15-minute signal
  // delay has been applied. Pro+ responses omit (or set false).
  delayed?: boolean;
  delay_minutes?: number;
};

function fmtPrice(p: number | null | undefined): string {
  if (p === null || p === undefined) return "—";
  if (p > 1) return `${p.toFixed(0)}¢`;
  return `${(p * 100).toFixed(0)}¢`;
}

function fmtPnl(p: number | null | undefined): string {
  if (p === null || p === undefined) return "—";
  const sign = p < 0 ? "-" : p > 0 ? "+" : "";
  return `${sign}$${Math.abs(p).toFixed(2)}`;
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

type Filter = "all" | "live" | "paper" | "wins" | "losses";
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "ALL" },
  { id: "live", label: "LIVE" },
  { id: "paper", label: "PAPER" },
  { id: "wins", label: "WINS" },
  { id: "losses", label: "LOSSES" },
];

export default function PositionsScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const [filter, setFilter] = useState<Filter>("all");

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["dashboard", "recent-trades"],
    queryFn: () => apiFetch<TradeList>("/api/dashboard/recent-trades?limit=50"),
    refetchInterval: 30_000,
  });

  const filtered = useMemo(() => {
    const items = data?.items ?? [];
    switch (filter) {
      case "live":
        return items.filter(
          (t) => (t.trading_mode ?? "").toLowerCase() === "live",
        );
      case "paper":
        return items.filter(
          (t) => (t.trading_mode ?? "").toLowerCase() === "paper",
        );
      case "wins":
        return items.filter((t) => (t.pnl ?? 0) > 0);
      case "losses":
        return items.filter((t) => (t.pnl ?? 0) < 0);
      default:
        return items;
    }
  }, [data, filter]);

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
      <View style={[styles.center, { backgroundColor: colors.background, padding: 24 }]}>
        <Text style={[styles.muted, { color: colors.destructive }]}>
          {status === 403 ? "Awaiting operator approval" : `Failed to load (HTTP ${status ?? "?"})`}
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
      data={filtered}
      keyExtractor={(t) => String(t.id)}
      ListHeaderComponent={
        <View>
          {data?.delayed ? (
            <DelayedSignalsBanner
              delayMinutes={data.delay_minutes}
              testID="banner-positions-delayed"
            />
          ) : null}
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            contentContainerStyle={styles.chipRow}
            style={styles.chipScroller}
          >
          {FILTERS.map((f) => {
            const active = f.id === filter;
            return (
              <Pressable
                key={f.id}
                onPress={() => setFilter(f.id)}
                style={[
                  styles.chip,
                  {
                    borderColor: active ? colors.primary : colors.border,
                    backgroundColor: active
                      ? colors.primary + "22"
                      : "transparent",
                  },
                ]}
                testID={`positions-filter-${f.id}`}
              >
                <Text
                  style={[
                    styles.chipText,
                    {
                      color: active ? colors.primary : colors.mutedForeground,
                    },
                  ]}
                >
                  {f.label}
                </Text>
              </Pressable>
            );
          })}
          </ScrollView>
        </View>
      }
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
            {filter === "all" ? "No trades yet" : `No ${filter} trades`}
          </Text>
        </View>
      }
      renderItem={({ item }) => (
        <View
          style={[
            styles.row,
            { backgroundColor: colors.card, borderColor: colors.border },
          ]}
        >
          <View style={styles.rowHead}>
            <Text style={[styles.market, { color: colors.foreground }]} numberOfLines={1}>
              {item.market_title ?? item.market_id ?? item.trade_id ?? "—"}
            </Text>
            <Text
              style={[
                styles.pnl,
                {
                  color:
                    (item.pnl ?? 0) > 0
                      ? colors.success
                      : (item.pnl ?? 0) < 0
                        ? colors.destructive
                        : colors.mutedForeground,
                },
              ]}
            >
              {fmtPnl(item.pnl)}
            </Text>
          </View>
          <View style={styles.metaRow}>
            <Text style={[styles.tag, { color: colors.primary, borderColor: colors.border }]}>
              {(item.side ?? "—").toUpperCase()}
            </Text>
            <Text style={[styles.tag, { color: colors.mutedForeground, borderColor: colors.border }]}>
              {(item.action ?? "—").toUpperCase()}
            </Text>
            <Text style={[styles.tagMuted, { color: colors.mutedForeground }]}>
              {item.quantity ?? 0} @ {fmtPrice(item.price)}
            </Text>
          </View>
          <View style={styles.metaRow}>
            <Text style={[styles.meta, { color: colors.mutedForeground }]}>
              {(item.league ?? item.vertical ?? "—").toUpperCase()} ·{" "}
              {(item.trading_mode ?? "—").toUpperCase()}
            </Text>
            <Text style={[styles.meta, { color: colors.mutedForeground }]}>
              {fmtTime(item.executed_at)}
            </Text>
          </View>
        </View>
      )}
    />
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  muted: { fontSize: 13, fontFamily: "Inter_400Regular" },
  empty: { paddingVertical: 60, alignItems: "center" },
  chipScroller: { marginBottom: 8 },
  chipRow: { gap: 6, paddingRight: 16 },
  chip: {
    borderWidth: 1,
    paddingHorizontal: 10,
    paddingVertical: 5,
    marginRight: 6,
  },
  chipText: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
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
  market: { flex: 1, fontFamily: "Inter_600SemiBold", fontSize: 13, marginRight: 8 },
  pnl: { fontSize: 14, fontFamily: "Inter_700Bold" },
  metaRow: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  tag: {
    fontSize: 10,
    letterSpacing: 1,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderWidth: 1,
    fontFamily: "Inter_600SemiBold",
  },
  tagMuted: { fontSize: 11, fontFamily: "Inter_500Medium" },
  meta: { fontSize: 11, fontFamily: "Inter_400Regular" },
});
