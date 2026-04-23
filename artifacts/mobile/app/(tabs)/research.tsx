import { useQuery } from "@tanstack/react-query";
import React from "react";
import {
  ActivityIndicator,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Card, SectionHeader } from "@/components/Card";
import DelayedSignalsBanner from "@/components/DelayedSignalsBanner";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";

type Debate = {
  id: number;
  matchup?: string | null;
  league?: string | null;
  market_type?: string | null;
  consensus_probability?: number | null;
  agreement_rate?: number | null;
  approved?: boolean | null;
  recommended_action?: string | null;
  created_at?: string | null;
};

type UwSignal = {
  id: number;
  signal_type: string;
  ticker?: string | null;
  occurred_at?: string | null;
  fetched_at?: string | null;
  summary?: string | null;
  size?: number | string | null;
  price?: number | string | null;
};

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

function fmtNum(v: number | string | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (Number.isNaN(n)) return String(v);
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function isToday(iso: string | null | undefined): boolean {
  if (!iso) return false;
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return false;
  const now = new Date();
  return (
    t.getFullYear() === now.getFullYear() &&
    t.getMonth() === now.getMonth() &&
    t.getDate() === now.getDate()
  );
}

export default function ResearchScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();

  const debates = useQuery({
    queryKey: ["dashboard", "recent-debates", "research"],
    queryFn: () =>
      apiFetch<{
        items: Debate[];
        delayed?: boolean;
        delay_minutes?: number;
      }>("/api/dashboard/recent-debates?limit=25"),
    refetchInterval: 60_000,
  });
  const uw = useQuery({
    queryKey: ["dashboard", "uw-signals"],
    queryFn: () =>
      apiFetch<{ items: UwSignal[] }>(
        "/api/dashboard/uw-signals?limit=25&since_hours=24",
      ),
    refetchInterval: 60_000,
  });

  const isLoading = debates.isLoading || uw.isLoading;
  const isError = debates.isError || uw.isError;
  const status =
    (debates.error as { status?: number } | null)?.status ??
    (uw.error as { status?: number } | null)?.status;

  const onRefresh = () => {
    debates.refetch();
    uw.refetch();
  };
  const refreshing =
    (debates.isFetching && !debates.isLoading) ||
    (uw.isFetching && !uw.isLoading);

  if (isLoading) {
    return (
      <View style={[styles.center, { backgroundColor: colors.background }]}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }

  if (isError) {
    return (
      <View
        style={[styles.center, { backgroundColor: colors.background, padding: 24 }]}
      >
        <Text style={[styles.muted, { color: colors.destructive }]}>
          {status === 403
            ? "Awaiting operator approval"
            : `Failed to load (HTTP ${status ?? "?"})`}
        </Text>
      </View>
    );
  }

  // Today's top AutoResearch picks: highest consensus, approved-first.
  const todays = (debates.data?.items ?? []).filter((d) => isToday(d.created_at));
  const debateList = (todays.length > 0 ? todays : debates.data?.items ?? [])
    .slice()
    .sort((a, b) => {
      const ap = a.approved ? 1 : 0;
      const bp = b.approved ? 1 : 0;
      if (ap !== bp) return bp - ap;
      return (b.consensus_probability ?? 0) - (a.consensus_probability ?? 0);
    })
    .slice(0, 10);

  const signalList = uw.data?.items ?? [];

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
          onRefresh={onRefresh}
          tintColor={colors.primary}
        />
      }
      testID="research-scroll"
    >
      {debates.data?.delayed ? (
        <DelayedSignalsBanner
          delayMinutes={debates.data.delay_minutes}
          testID="banner-research-delayed"
        />
      ) : null}
      <SectionHeader>TODAY'S TOP AUTORESEARCH</SectionHeader>
      {debateList.length === 0 ? (
        <Card>
          <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>
            No research picks yet today
          </Text>
        </Card>
      ) : (
        debateList.map((d) => (
          <Card key={`d-${d.id}`} style={styles.card}>
            <View style={styles.rowHead}>
              <Text
                style={[styles.title, { color: colors.foreground }]}
                numberOfLines={1}
              >
                {d.matchup ?? `#${d.id}`}
              </Text>
              <Text
                style={[
                  styles.badge,
                  {
                    color: d.approved ? colors.success : colors.mutedForeground,
                    borderColor: d.approved ? colors.success : colors.border,
                  },
                ]}
              >
                {d.approved ? "APPROVED" : "REJECTED"}
              </Text>
            </View>
            <View style={styles.statsRow}>
              <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                {(d.league ?? "—").toUpperCase()} ·{" "}
                {(d.market_type ?? "—").toUpperCase()}
              </Text>
              <Text style={[styles.stat, { color: colors.primary }]}>
                p={(d.consensus_probability ?? 0).toFixed(2)}
              </Text>
              <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                agree {((d.agreement_rate ?? 0) * 100).toFixed(0)}%
              </Text>
            </View>
            <Text style={[styles.time, { color: colors.mutedForeground }]}>
              {(d.recommended_action ?? "").toUpperCase()} ·{" "}
              {fmtRel(d.created_at)}
            </Text>
          </Card>
        ))
      )}

      <View style={{ height: 16 }} />
      <SectionHeader>UNUSUAL WHALES · LAST 24h</SectionHeader>
      {signalList.length === 0 ? (
        <Card>
          <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>
            No UW signals in the last 24 hours
          </Text>
        </Card>
      ) : (
        signalList.map((s) => (
          <Card key={`uw-${s.id}`} style={styles.card}>
            <View style={styles.rowHead}>
              <Text
                style={[styles.title, { color: colors.foreground }]}
                numberOfLines={1}
              >
                {(s.ticker ?? "—").toUpperCase()}{" "}
                <Text style={{ color: colors.mutedForeground }}>
                  · {s.signal_type.toUpperCase()}
                </Text>
              </Text>
              <Text style={[styles.time, { color: colors.mutedForeground }]}>
                {fmtRel(s.occurred_at ?? s.fetched_at)}
              </Text>
            </View>
            {s.summary ? (
              <Text
                style={[styles.summary, { color: colors.foreground }]}
                numberOfLines={2}
              >
                {s.summary}
              </Text>
            ) : null}
            <View style={styles.statsRow}>
              <Text style={[styles.stat, { color: colors.primary }]}>
                size {fmtNum(s.size)}
              </Text>
              <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                px {fmtNum(s.price)}
              </Text>
            </View>
          </Card>
        ))
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { padding: 16, gap: 8 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  muted: { fontSize: 13, fontFamily: "Inter_400Regular" },
  card: { gap: 8 },
  rowHead: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  title: {
    fontSize: 13,
    fontFamily: "Inter_600SemiBold",
    flex: 1,
    marginRight: 8,
  },
  time: { fontSize: 11, fontFamily: "Inter_400Regular" },
  summary: { fontSize: 12, fontFamily: "Inter_400Regular" },
  statsRow: { flexDirection: "row", gap: 12, flexWrap: "wrap" },
  stat: { fontSize: 12, fontFamily: "Inter_500Medium" },
  badge: {
    fontSize: 10,
    letterSpacing: 1,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderWidth: 1,
    fontFamily: "Inter_700Bold",
  },
});
