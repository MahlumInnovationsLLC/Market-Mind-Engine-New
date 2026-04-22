import { useQuery } from "@tanstack/react-query";
import React from "react";
import { ActivityIndicator, RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import type { VerticalsOverview } from "@workspace/api-client-react";

import { Card, SectionHeader, StatTile } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { useSelectedVerticals } from "@/lib/vertical-filter";

type Counts = {
  trading_cycles?: number | null;
  trades?: number | null;
  swarm_debates?: number | null;
  predictions?: number | null;
  positions?: number | null;
};

type Overview = {
  counts: Counts;
  latest_cycle_at?: string | null;
  latest_trade_at?: string | null;
  realized_pnl?: number | null;
  win_rate?: number | null;
  open_positions?: number | null;
};

type SubInfo = {
  tier: string;
  status: string;
  is_founder: boolean;
  email?: string | null;
};

function fmtCurrency(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function fmtRel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = Date.now() - t;
  if (diff < 60_000) return `${Math.max(1, Math.floor(diff / 1000))}s ago`;
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

export default function DashboardScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();

  const overview = useQuery({
    queryKey: ["dashboard", "overview"],
    queryFn: () => apiFetch<Overview>("/api/dashboard/overview"),
    refetchInterval: 30_000,
  });

  const sub = useQuery({
    queryKey: ["me", "subscription"],
    queryFn: () => apiFetch<SubInfo>("/api/me/subscription"),
  });
  const verticals = useQuery({
    queryKey: ["dashboard", "verticals", 7],
    queryFn: () =>
      apiFetch<VerticalsOverview>("/api/dashboard/verticals?days=7"),
    refetchInterval: 60_000,
  });
  const { selected, matches, clear } = useSelectedVerticals();
  const filterActive = selected.length > 0;

  const refreshing = overview.isFetching && overview.isFetched;

  if (overview.isLoading) {
    return (
      <View style={[styles.center, { backgroundColor: colors.background }]}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }

  if (overview.isError) {
    const status = (overview.error as { status?: number } | null)?.status;
    if (status === 403) {
      return (
        <View style={[styles.center, { backgroundColor: colors.background, padding: 24 }]}>
          <Text style={[styles.eyebrow, { color: colors.mutedForeground }]}>AUTH</Text>
          <Text style={[styles.title, { color: colors.primary, textAlign: "center" }]}>
            AWAITING APPROVAL
          </Text>
          <Text style={[styles.muted, { color: colors.mutedForeground, textAlign: "center", marginTop: 12 }]}>
            Your account is signed in but not yet on the operator allow-list. An admin
            will grant access.
          </Text>
        </View>
      );
    }
    return (
      <View style={[styles.center, { backgroundColor: colors.background, padding: 24 }]}>
        <Text style={[styles.muted, { color: colors.destructive }]}>
          Failed to load dashboard (HTTP {status ?? "?"})
        </Text>
      </View>
    );
  }

  const data = overview.data!;
  const counts = data.counts ?? {};

  const filteredRows = (verticals.data?.verticals ?? []).filter((v) =>
    matches(v.vertical),
  );
  const aggTrades = filteredRows.reduce((s, r) => s + (r.trades ?? 0), 0);
  const aggWins = filteredRows.reduce((s, r) => s + (r.wins ?? 0), 0);
  const aggLosses = filteredRows.reduce((s, r) => s + (r.losses ?? 0), 0);
  const aggPnl = filteredRows.reduce((s, r) => s + (r.pnl ?? 0), 0);
  const aggOpen = filteredRows.reduce(
    (s, r) => s + (r.open_positions ?? 0),
    0,
  );
  const decided = aggWins + aggLosses;
  const aggWinRate = decided > 0 ? aggWins / decided : null;

  const realizedPnl = filterActive ? aggPnl : data.realized_pnl;
  const winRate = filterActive ? aggWinRate : data.win_rate;
  const openPositions = filterActive
    ? aggOpen
    : (data.open_positions ?? counts.positions ?? 0);
  const tradesCount = filterActive ? aggTrades : (counts.trades ?? 0);

  return (
    <ScrollView
      style={{ backgroundColor: colors.background }}
      contentContainerStyle={[
        styles.container,
        { paddingBottom: insets.bottom + 100 },
      ]}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={() => overview.refetch()}
          tintColor={colors.primary}
        />
      }
    >
      <SectionHeader>OPERATOR :: {sub.data?.email ?? "—"}</SectionHeader>

      {filterActive ? (
        <Card>
          <View style={styles.kvRow}>
            <Text
              style={[styles.kvKey, { color: colors.primary }]}
              testID="badge-vertical-filtered"
            >
              FILTERED · {selected.join(", ").toUpperCase()}
            </Text>
            <Text
              onPress={() => clear()}
              testID="button-clear-vertical-filter"
              style={[styles.kvVal, { color: colors.mutedForeground }]}
            >
              CLEAR
            </Text>
          </View>
        </Card>
      ) : null}

      <View style={styles.row}>
        <StatTile label="REALIZED P&L" value={fmtCurrency(realizedPnl)} />
        <StatTile label="WIN RATE" value={fmtPct(winRate)} />
      </View>
      <View style={styles.row}>
        <StatTile label="OPEN POSITIONS" value={String(openPositions)} />
        <StatTile label="TRADES" value={String(tradesCount)} />
      </View>

      <SectionHeader>SWARM</SectionHeader>
      <View style={styles.row}>
        <StatTile label="DEBATES" value={String(counts.swarm_debates ?? 0)} />
        <StatTile label="CYCLES" value={String(counts.trading_cycles ?? 0)} />
      </View>

      <SectionHeader>HEARTBEAT</SectionHeader>
      <Card>
        <View style={styles.kvRow}>
          <Text style={[styles.kvKey, { color: colors.mutedForeground }]}>LAST CYCLE</Text>
          <Text style={[styles.kvVal, { color: colors.foreground }]}>
            {fmtRel(data.latest_cycle_at)}
          </Text>
        </View>
        <View style={[styles.kvRow, { borderTopColor: colors.border, borderTopWidth: 1 }]}>
          <Text style={[styles.kvKey, { color: colors.mutedForeground }]}>LAST TRADE</Text>
          <Text style={[styles.kvVal, { color: colors.foreground }]}>
            {fmtRel(data.latest_trade_at)}
          </Text>
        </View>
        <View style={[styles.kvRow, { borderTopColor: colors.border, borderTopWidth: 1 }]}>
          <Text style={[styles.kvKey, { color: colors.mutedForeground }]}>TIER</Text>
          <Text style={[styles.kvVal, { color: colors.primary }]}>
            {(sub.data?.tier ?? "—").toUpperCase()}
          </Text>
        </View>
      </Card>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { padding: 16, gap: 8 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  row: { flexDirection: "row", gap: 8, marginBottom: 8 },
  kvRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 12,
  },
  kvKey: { fontSize: 11, letterSpacing: 1.5, fontFamily: "Inter_500Medium" },
  kvVal: { fontSize: 13, fontFamily: "Inter_600SemiBold" },
  eyebrow: { fontSize: 11, letterSpacing: 3, fontFamily: "Inter_500Medium" },
  title: { fontSize: 22, fontFamily: "Inter_700Bold", letterSpacing: 1.5, marginTop: 8 },
  muted: { fontSize: 13, fontFamily: "Inter_400Regular" },
});
