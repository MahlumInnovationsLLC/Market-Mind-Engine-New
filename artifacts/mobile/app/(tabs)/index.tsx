import { useQuery } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import React, { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import type { VerticalsOverview } from "@workspace/api-client-react";

import { Card, SectionHeader, StatTile } from "@/components/Card";
import DelayedSignalsBanner from "@/components/DelayedSignalsBanner";
import PerLeagueCalibration from "@/components/PerLeagueCalibration";
import { PlacedByToggle } from "@/components/PlacedByToggle";
import TailRiskExposure from "@/components/TailRiskExposure";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { usePlacedBy } from "@/lib/placed-by-filter";
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
  // Set by the backend for free-tier callers so the dashboard tab can
  // render the same upsell banner the list tabs (positions / research /
  // pipeline) already show. Pro+ responses set ``delayed: false``.
  tier?: string | null;
  delayed?: boolean;
  delay_minutes?: number;
};

type SubInfo = {
  tier: string;
  status: string;
  is_founder: boolean;
  email?: string | null;
  quota?: {
    viewed_today: number;
    limit: number;
    resets_at: string;
  } | null;
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

type Period = 1 | 7 | 30 | 365;
const PERIOD_LABELS: Record<Period, string> = {
  1: "1D",
  7: "7D",
  30: "30D",
  365: "YTD",
};

export default function DashboardScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const [period, setPeriod] = useState<Period>(7);
  // Trade-provenance filter (T005).  Module-level pub/sub state so the
  // selection survives navigation between the dashboard / positions /
  // verticals tabs.  Threaded into every trade-derived query below.
  const { placedBy } = usePlacedBy();

  const overview = useQuery({
    queryKey: ["dashboard", "overview", placedBy],
    queryFn: () =>
      apiFetch<Overview>(`/api/dashboard/overview?placed_by=${placedBy}`),
    refetchInterval: 30_000,
  });

  const sub = useQuery({
    queryKey: ["me", "subscription"],
    queryFn: () => apiFetch<SubInfo>("/api/me/subscription"),
    // Keep the free-tier quota indicator current. The counter is
    // server-authoritative and increments whenever the user opens
    // a market, so a periodic refetch (and pull-to-refresh below)
    // is enough to keep the badge honest without an explicit
    // invalidation hook on every market view.
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });
  const verticals = useQuery({
    queryKey: ["dashboard", "verticals", period, placedBy],
    queryFn: () =>
      apiFetch<VerticalsOverview>(
        `/api/dashboard/verticals?days=${period}&placed_by=${placedBy}`,
      ),
    refetchInterval: 60_000,
  });
  const { selected, matches, clear, filterActive, hiddenActive } =
    useSelectedVerticals();

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

      {data.delayed ? (
        <DelayedSignalsBanner
          delayMinutes={data.delay_minutes}
          testID="banner-dashboard-delayed"
        />
      ) : null}

      {sub.data?.quota ? (
        <View
          style={[
            styles.quotaPill,
            {
              borderColor:
                sub.data.quota.viewed_today >= sub.data.quota.limit
                  ? colors.destructive
                  : sub.data.quota.limit - sub.data.quota.viewed_today <= 1
                    ? colors.warning
                    : colors.primary,
              backgroundColor: colors.card,
            },
          ]}
          testID="pill-free-quota"
        >
          <Text style={[styles.quotaText, { color: colors.foreground }]}>
            {sub.data.quota.viewed_today} / {sub.data.quota.limit} MARKETS TODAY
          </Text>
          <Text style={[styles.quotaResets, { color: colors.mutedForeground }]}>
            RESETS {sub.data.quota.resets_at}
          </Text>
        </View>
      ) : null}

      {sub.data?.tier === "free" &&
      sub.data?.quota &&
      sub.data.quota.viewed_today >= sub.data.quota.limit ? (
        <Pressable
          onPress={() => router.push("/(tabs)/subscription")}
          testID="cta-upgrade-quota"
          style={({ pressed }) => [
            styles.upgradeCta,
            {
              backgroundColor: colors.primary,
              opacity: pressed ? 0.85 : 1,
            },
          ]}
        >
          <Text style={[styles.upgradeCtaText, { color: colors.primaryForeground }]}>
            UPGRADE FOR UNLIMITED MARKETS
          </Text>
        </Pressable>
      ) : null}

      <View
        style={[
          styles.periodRow,
          { borderColor: colors.border, backgroundColor: colors.card },
        ]}
        testID="overview-period-toggle"
      >
        {(Object.keys(PERIOD_LABELS) as unknown as Period[])
          .map((k) => Number(k) as Period)
          .map((p) => {
            const active = p === period;
            return (
              <Pressable
                key={p}
                onPress={() => setPeriod(p)}
                style={[
                  styles.periodChip,
                  {
                    backgroundColor: active ? colors.primary : "transparent",
                  },
                ]}
                testID={`overview-period-${p}`}
              >
                <Text
                  style={[
                    styles.periodLabel,
                    {
                      color: active
                        ? colors.primaryForeground
                        : colors.mutedForeground,
                    },
                  ]}
                >
                  {PERIOD_LABELS[p]}
                </Text>
              </Pressable>
            );
          })}
      </View>

      <PlacedByToggle testID="overview-placed-by-toggle" />

      {filterActive ? (
        <Card>
          <View style={styles.kvRow}>
            <Text
              style={[styles.kvKey, { color: colors.primary }]}
              testID="badge-vertical-filtered"
            >
              FILTERED ·{" "}
              {selected.length > 0
                ? selected.join(", ").toUpperCase()
                : hiddenActive
                  ? "HIDDEN SUPPRESSED"
                  : ""}
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
        <StatTile
          label="TRADES"
          value={String(tradesCount)}
          testID="card-recent-trades"
        />
      </View>

      <TailRiskExposure />

      <SectionHeader>SWARM</SectionHeader>
      <View style={styles.row}>
        <StatTile label="DEBATES" value={String(counts.swarm_debates ?? 0)} />
        <StatTile
          label="CYCLES"
          value={String(counts.trading_cycles ?? 0)}
          testID="card-trading-cycles"
        />
      </View>

      <SectionHeader>HEARTBEAT</SectionHeader>
      <Card testID="card-health-snapshot">
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

      <View style={{ height: 16 }} />
      <PerLeagueCalibration />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { padding: 16, gap: 8 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  periodRow: {
    flexDirection: "row",
    borderWidth: 1,
    padding: 4,
    marginBottom: 12,
    gap: 4,
  },
  periodChip: {
    flex: 1,
    paddingVertical: 8,
    alignItems: "center",
  },
  periodLabel: {
    fontSize: 11,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
  row: { flexDirection: "row", gap: 8, marginBottom: 8 },
  kvRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 12,
  },
  kvKey: { fontSize: 11, letterSpacing: 1.5, fontFamily: "Inter_500Medium" },
  quotaPill: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    borderWidth: 1,
    paddingVertical: 8,
    paddingHorizontal: 12,
    marginBottom: 12,
  },
  quotaText: { fontSize: 11, letterSpacing: 1.5, fontFamily: "Inter_700Bold" },
  quotaResets: {
    fontSize: 10,
    letterSpacing: 1,
    fontFamily: "Inter_400Regular",
  },
  upgradeCta: {
    paddingVertical: 12,
    paddingHorizontal: 12,
    alignItems: "center",
    marginBottom: 12,
    marginTop: -4,
  },
  upgradeCtaText: {
    fontSize: 12,
    letterSpacing: 2,
    fontFamily: "Inter_700Bold",
  },
  kvVal: { fontSize: 13, fontFamily: "Inter_600SemiBold" },
  eyebrow: { fontSize: 11, letterSpacing: 3, fontFamily: "Inter_500Medium" },
  title: { fontSize: 22, fontFamily: "Inter_700Bold", letterSpacing: 1.5, marginTop: 8 },
  muted: { fontSize: 13, fontFamily: "Inter_400Regular" },
});
