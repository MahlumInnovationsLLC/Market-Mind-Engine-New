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

type Cycle = {
  id: number;
  vertical?: string | null;
  picks_evaluated?: number | null;
  trades_placed?: number | null;
  duration_seconds?: number | null;
  started_at?: string | null;
  completed_at?: string | null;
};
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

export default function PipelineScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();

  const cycles = useQuery({
    queryKey: ["dashboard", "recent-cycles"],
    queryFn: () => apiFetch<{ items: Cycle[] }>("/api/dashboard/recent-cycles?limit=10"),
    refetchInterval: 30_000,
  });
  const debates = useQuery({
    queryKey: ["dashboard", "recent-debates"],
    queryFn: () =>
      apiFetch<{
        items: Debate[];
        delayed?: boolean;
        delay_minutes?: number;
      }>("/api/dashboard/recent-debates?limit=10"),
    refetchInterval: 30_000,
  });

  const isLoading = cycles.isLoading || debates.isLoading;
  const isError = cycles.isError || debates.isError;
  const status =
    (cycles.error as { status?: number } | null)?.status ??
    (debates.error as { status?: number } | null)?.status;

  const onRefresh = () => {
    cycles.refetch();
    debates.refetch();
  };
  const refreshing =
    (cycles.isFetching && !cycles.isLoading) ||
    (debates.isFetching && !debates.isLoading);

  if (isLoading) {
    return (
      <View style={[styles.center, { backgroundColor: colors.background }]}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }

  if (isError) {
    return (
      <View style={[styles.center, { backgroundColor: colors.background, padding: 24 }]}>
        <Text style={[styles.muted, { color: colors.destructive }]}>
          {status === 403 ? "Awaiting operator approval" : `Failed to load (HTTP ${status ?? "?"})`}
        </Text>
      </View>
    );
  }

  return (
    <ScrollView
      style={{ backgroundColor: colors.background }}
      contentContainerStyle={[styles.container, { paddingBottom: insets.bottom + 100 }]}
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.primary} />
      }
    >
      {debates.data?.delayed ? (
        <DelayedSignalsBanner
          delayMinutes={debates.data.delay_minutes}
          testID="banner-pipeline-delayed"
        />
      ) : null}
      <SectionHeader>CYCLE PIPELINE</SectionHeader>
      <Card style={styles.pipelineCard}>
        <View style={styles.stageRow}>
          {[
            { id: "ingest", label: "INGEST" },
            { id: "score", label: "SCORE" },
            { id: "debate", label: "DEBATE" },
            { id: "risk", label: "RISK" },
            { id: "execute", label: "EXEC" },
            { id: "settle", label: "SETTLE" },
          ].map((stage, idx, arr) => (
            <React.Fragment key={stage.id}>
              <View style={styles.stage}>
                <View
                  style={[
                    styles.stageDot,
                    { borderColor: colors.primary },
                  ]}
                >
                  <Text
                    style={[
                      styles.stageNum,
                      { color: colors.primary },
                    ]}
                  >
                    {idx + 1}
                  </Text>
                </View>
                <Text
                  style={[
                    styles.stageLabel,
                    { color: colors.mutedForeground },
                  ]}
                >
                  {stage.label}
                </Text>
              </View>
              {idx < arr.length - 1 ? (
                <View
                  style={[
                    styles.stageConnector,
                    { backgroundColor: colors.border },
                  ]}
                />
              ) : null}
            </React.Fragment>
          ))}
        </View>
      </Card>

      <View style={{ height: 16 }} />
      <SectionHeader>RECENT CYCLES</SectionHeader>
      {(cycles.data?.items ?? []).length === 0 ? (
        <Card>
          <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>
            No cycles yet
          </Text>
        </Card>
      ) : (
        (cycles.data?.items ?? []).map((c) => (
          <Card key={c.id} style={styles.card}>
            <View style={styles.rowHead}>
              <Text style={[styles.title, { color: colors.foreground }]}>
                #{c.id} · {(c.vertical ?? "—").toUpperCase()}
              </Text>
              <Text style={[styles.time, { color: colors.mutedForeground }]}>
                {fmtRel(c.started_at)}
              </Text>
            </View>
            <View style={styles.statsRow}>
              <Text style={[styles.stat, { color: colors.primary }]}>
                {c.picks_evaluated ?? 0} picks
              </Text>
              <Text style={[styles.stat, { color: colors.success }]}>
                {c.trades_placed ?? 0} trades
              </Text>
              <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                {(c.duration_seconds ?? 0).toFixed(1)}s
              </Text>
            </View>
          </Card>
        ))
      )}

      <View style={{ height: 16 }} />
      <SectionHeader>RECENT DEBATES</SectionHeader>
      {(debates.data?.items ?? []).length === 0 ? (
        <Card>
          <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>
            No debates yet
          </Text>
        </Card>
      ) : (
        (debates.data?.items ?? []).map((d) => (
          <Card key={d.id} style={styles.card}>
            <View style={styles.rowHead}>
              <Text style={[styles.title, { color: colors.foreground }]} numberOfLines={1}>
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
                {(d.league ?? "—").toUpperCase()} · {(d.market_type ?? "—").toUpperCase()}
              </Text>
              <Text style={[styles.stat, { color: colors.primary }]}>
                p={(d.consensus_probability ?? 0).toFixed(2)}
              </Text>
              <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                agree {((d.agreement_rate ?? 0) * 100).toFixed(0)}%
              </Text>
            </View>
            <Text style={[styles.time, { color: colors.mutedForeground }]}>
              {(d.recommended_action ?? "").toUpperCase()} · {fmtRel(d.created_at)}
            </Text>
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
  pipelineCard: { paddingVertical: 16 },
  stageRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  stage: { alignItems: "center", gap: 6, minWidth: 36 },
  stageDot: {
    width: 28,
    height: 28,
    borderRadius: 14,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  stageNum: {
    fontSize: 11,
    fontFamily: "Inter_700Bold",
  },
  stageLabel: {
    fontSize: 9,
    letterSpacing: 1.2,
    fontFamily: "Inter_600SemiBold",
  },
  stageConnector: {
    flex: 1,
    height: 1,
    marginHorizontal: 4,
    marginBottom: 16,
  },
  rowHead: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  title: { fontSize: 13, fontFamily: "Inter_600SemiBold", flex: 1, marginRight: 8 },
  time: { fontSize: 11, fontFamily: "Inter_400Regular" },
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
