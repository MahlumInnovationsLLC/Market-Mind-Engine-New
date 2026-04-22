import { useQuery } from "@tanstack/react-query";
import React from "react";
import {
  ActivityIndicator,
  FlatList,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";

type LastCycle = {
  id: number;
  picks_evaluated?: number | null;
  trades_placed?: number | null;
  errors?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
};

type VerticalEntry = {
  vertical: string;
  description?: string | null;
  pinned: boolean;
  hidden: boolean;
  trades: number;
  wins: number;
  losses: number;
  win_rate?: number | null;
  pnl: number;
  brier?: number | null;
  sharpe?: number | null;
  open_positions: number;
  edge_predicted_bps?: number | null;
  edge_realized_bps?: number | null;
  last_cycle?: LastCycle | null;
};

type Overview = {
  days: number;
  verticals: VerticalEntry[];
};

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
  const query = useQuery({
    queryKey: ["dashboard", "verticals", 7],
    queryFn: () => apiFetch<Overview>("/api/dashboard/verticals?days=7"),
    refetchInterval: 60_000,
  });

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

  const items = (query.data?.verticals ?? []).filter((e) => !e.hidden);

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.headerText}>
          {items.length} verticals · 7-day window
        </Text>
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
