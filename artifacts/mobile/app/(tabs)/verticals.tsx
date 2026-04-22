import { useQuery } from "@tanstack/react-query";
import type {
  MyAccess,
  VerticalsOverview,
  VerticalsOverviewEntry,
} from "@workspace/api-client-react";
import React, { useState } from "react";
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

import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";

function fmtMoney(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
}

function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function fmt(n: number | null | undefined, digits = 3): string {
  if (n === null || n === undefined) return "—";
  return n.toFixed(digits);
}

function fmtBps(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${n.toFixed(0)}bps`;
}

export default function VerticalsScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const [selected, setSelected] = useState<string[]>([]);
  const query = useQuery({
    queryKey: ["dashboard", "verticals", 7],
    queryFn: () =>
      apiFetch<VerticalsOverview>("/api/dashboard/verticals?days=7"),
    refetchInterval: 60_000,
  });
  const access = useQuery({
    queryKey: ["me", "access"],
    queryFn: () => apiFetch<MyAccess>("/api/me/access"),
    staleTime: 30_000,
  });
  const isAdmin = !!access.data?.is_admin;
  const toggle = (v: string) => {
    const lower = v.toLowerCase();
    setSelected((prev) =>
      prev.includes(lower) ? prev.filter((x) => x !== lower) : [...prev, lower],
    );
  };
  const matches = (v: string) => {
    const lower = v.toLowerCase();
    if (selected.length === 0) return true;
    return selected.includes(lower);
  };

  const styles = StyleSheet.create({
    container: { flex: 1, backgroundColor: colors.background },
    header: {
      paddingHorizontal: 16,
      paddingTop: 12,
      paddingBottom: 8,
      borderBottomWidth: 1,
      borderBottomColor: colors.border,
    },
    headerText: {
      color: colors.mutedForeground,
      fontSize: 10,
      letterSpacing: 1.5,
      fontFamily: "Inter_600SemiBold",
      textTransform: "uppercase",
    },
    card: {
      backgroundColor: colors.card,
      borderWidth: 1,
      borderColor: colors.border,
      padding: 12,
      marginHorizontal: 16,
      marginVertical: 6,
    },
    hidden: { opacity: 0.55, borderStyle: "dashed" },
    cardHeader: {
      flexDirection: "row",
      justifyContent: "space-between",
      alignItems: "baseline",
      marginBottom: 6,
    },
    title: {
      color: colors.primary,
      fontSize: 13,
      letterSpacing: 1.5,
      fontFamily: "Inter_700Bold",
      textTransform: "uppercase",
    },
    desc: {
      color: colors.mutedForeground,
      fontSize: 11,
      marginBottom: 8,
    },
    row: { flexDirection: "row", flexWrap: "wrap" },
    cell: { width: "50%", paddingVertical: 4 },
    label: {
      color: colors.mutedForeground,
      fontSize: 9,
      letterSpacing: 1.5,
      textTransform: "uppercase",
    },
    value: {
      color: colors.foreground,
      fontSize: 13,
      fontFamily: "Inter_600SemiBold",
      marginTop: 2,
    },
    pos: { color: "#22c55e" },
    neg: { color: "#ef4444" },
    footer: {
      borderTopWidth: 1,
      borderTopColor: colors.border,
      marginTop: 8,
      paddingTop: 8,
    },
    chipRow: { paddingTop: 8, gap: 6, paddingRight: 16 },
    chip: {
      borderWidth: 1,
      paddingHorizontal: 10,
      paddingVertical: 4,
      marginRight: 6,
    },
    chipText: {
      fontSize: 10,
      letterSpacing: 1.5,
      textTransform: "uppercase",
      fontFamily: "Inter_600SemiBold",
    },
    empty: {
      color: colors.mutedForeground,
      textAlign: "center",
      marginTop: 40,
      fontSize: 12,
    },
  });

  if (query.isLoading) {
    return (
      <View style={[styles.container, { justifyContent: "center" }]}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }

  const all: VerticalsOverviewEntry[] = query.data?.verticals ?? [];
  const visible = isAdmin ? all : all.filter((e) => !e.hidden);
  const items = visible.filter((e) => matches(e.vertical));
  const chips = visible;

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.headerText}>
          {items.length} verticals · 7-day window
        </Text>
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.chipRow}
        >
          {chips.map((c) => {
            const active = selected.includes(c.vertical.toLowerCase());
            return (
              <Pressable
                key={c.vertical}
                onPress={() => toggle(c.vertical)}
                testID={`chip-vertical-${c.vertical}`}
                style={[
                  styles.chip,
                  {
                    borderColor: active ? colors.primary : colors.border,
                    backgroundColor: active
                      ? colors.primary + "22"
                      : "transparent",
                  },
                ]}
              >
                <Text
                  style={[
                    styles.chipText,
                    { color: active ? colors.primary : colors.foreground },
                  ]}
                >
                  {c.vertical}
                  {c.pinned ? " ★" : ""}
                </Text>
              </Pressable>
            );
          })}
          {selected.length > 0 ? (
            <Pressable
              onPress={() => setSelected([])}
              testID="chip-vertical-clear"
              style={[styles.chip, { borderColor: colors.border }]}
            >
              <Text
                style={[styles.chipText, { color: colors.mutedForeground }]}
              >
                clear
              </Text>
            </Pressable>
          ) : null}
        </ScrollView>
      </View>
      <FlatList
        data={items}
        keyExtractor={(e) => e.vertical}
        contentContainerStyle={{ paddingBottom: insets.bottom + 24 }}
        refreshControl={
          <RefreshControl
            refreshing={query.isRefetching}
            onRefresh={() => query.refetch()}
            tintColor={colors.primary}
          />
        }
        ListEmptyComponent={
          <Text style={styles.empty}>No verticals registered.</Text>
        }
        renderItem={({ item }) => (
          <View
            style={[styles.card, item.hidden && styles.hidden]}
            testID={`card-vertical-${item.vertical}`}
          >
            <View style={styles.cardHeader}>
              <Text style={styles.title}>
                {item.vertical}
                {item.pinned ? "  ★" : ""}
              </Text>
              <Text style={styles.label}>{item.trades} trades</Text>
            </View>
            {item.description ? (
              <Text style={styles.desc}>{item.description}</Text>
            ) : null}
            <View style={styles.row}>
              <View style={styles.cell}>
                <Text style={styles.label}>7d P&L</Text>
                <Text
                  style={[
                    styles.value,
                    item.pnl > 0 ? styles.pos : item.pnl < 0 ? styles.neg : null,
                  ]}
                >
                  {fmtMoney(item.pnl)}
                </Text>
              </View>
              <View style={styles.cell}>
                <Text style={styles.label}>Win rate</Text>
                <Text style={styles.value}>{fmtPct(item.win_rate)}</Text>
              </View>
              <View style={styles.cell}>
                <Text style={styles.label}>24h Brier</Text>
                <Text style={styles.value}>{fmt(item.brier)}</Text>
              </View>
              <View style={styles.cell}>
                <Text style={styles.label}>Sharpe</Text>
                <Text style={styles.value}>{fmt(item.sharpe, 2)}</Text>
              </View>
              <View style={styles.cell}>
                <Text style={styles.label}>Open positions</Text>
                <Text style={styles.value}>{item.open_positions}</Text>
              </View>
              <View style={styles.cell}>
                <Text style={styles.label}>Edge p/r</Text>
                <Text style={styles.value}>
                  {fmtBps(item.edge_predicted_bps)} / {fmtBps(item.edge_realized_bps)}
                </Text>
              </View>
            </View>
            {item.last_cycle ? (
              <View style={styles.footer}>
                <Text style={styles.label}>
                  Last cycle #{item.last_cycle.id} ·{" "}
                  {item.last_cycle.trades_placed ?? 0} trades
                </Text>
              </View>
            ) : null}
          </View>
        )}
      />
    </View>
  );
}
