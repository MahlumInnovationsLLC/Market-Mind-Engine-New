import { useQuery } from "@tanstack/react-query";
import type {
  MyAccess,
  VerticalsOverview,
  VerticalsOverviewEntry,
} from "@workspace/api-client-react";
import React, { useMemo } from "react";
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

import { useRouter } from "expo-router";

import { PlacedByToggle } from "@/components/PlacedByToggle";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { usePlacedBy } from "@/lib/placed-by-filter";
import {
  setSelectedVerticals,
  useSelectedVerticals,
} from "@/lib/vertical-filter";

type PersonaTogglesPayload = {
  items: Array<{
    vertical: string;
    vertical_label?: string;
    persona_profile?: string | null;
    personas: Array<{ id: string; display?: string; enabled: boolean }>;
    orphaned?: boolean;
  }>;
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
  const { selected, toggle, clear, matches } = useSelectedVerticals();
  // Trade-provenance filter (T005). Module-level pub/sub state shared
  // with the dashboard / positions tabs.
  const { placedBy } = usePlacedBy();
  const router = useRouter();
  const drillTo = (vertical: string) => {
    setSelectedVerticals([vertical]);
    router.push("/(tabs)");
  };
  const query = useQuery({
    queryKey: ["dashboard", "verticals", 7, placedBy],
    queryFn: () =>
      apiFetch<VerticalsOverview>(
        `/api/dashboard/verticals?days=7&placed_by=${placedBy}`,
      ),
    refetchInterval: 60_000,
  });
  const access = useQuery({
    queryKey: ["me", "access"],
    queryFn: () => apiFetch<MyAccess>("/api/me/access"),
    staleTime: 30_000,
  });
  const isAdmin = !!access.data?.is_admin;
  // Mirrors the web dashboard's per-vertical persona-override badge
  // (artifacts/web/src/components/dashboard/vertical-cards.tsx). Admin-
  // only — non-admins can't read /api/admin/persona-toggles, so we
  // skip the request entirely instead of letting it 403.
  const personaToggles = useQuery<PersonaTogglesPayload>({
    queryKey: ["admin", "persona-toggles", "summary"],
    queryFn: () =>
      apiFetch<PersonaTogglesPayload>("/api/admin/persona-toggles"),
    enabled: isAdmin,
    staleTime: 60_000,
    refetchInterval: 120_000,
  });
  const disabledByVertical = useMemo<Record<string, string[]>>(() => {
    const out: Record<string, string[]> = {};
    const items = personaToggles.data?.items ?? [];
    for (const v of items) {
      const off = v.personas
        .filter((p) => !p.enabled)
        .map((p) => p.display || p.id);
      if (off.length > 0) out[v.vertical] = off;
    }
    return out;
  }, [personaToggles.data]);
  const overrideVerticals = Object.keys(disabledByVertical).sort();

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
    overrideBanner: {
      marginHorizontal: 16,
      marginTop: 8,
      borderWidth: 1,
      borderColor: colors.warning,
      backgroundColor: colors.warning + "1A",
      paddingHorizontal: 10,
      paddingVertical: 8,
    },
    overrideTitle: {
      color: colors.warning,
      fontSize: 10,
      letterSpacing: 1.5,
      textTransform: "uppercase",
      fontFamily: "Inter_700Bold",
      marginBottom: 4,
    },
    overrideBody: {
      color: colors.mutedForeground,
      fontSize: 11,
      marginBottom: 6,
    },
    overrideChipRow: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
    overrideChip: {
      borderWidth: 1,
      borderColor: colors.warning,
      paddingHorizontal: 6,
      paddingVertical: 2,
      marginRight: 6,
      marginBottom: 4,
    },
    overrideChipText: {
      color: colors.warning,
      fontSize: 10,
      letterSpacing: 1.5,
      textTransform: "uppercase",
      fontFamily: "Inter_600SemiBold",
    },
    cardBadgeRow: {
      flexDirection: "row",
      flexWrap: "wrap",
      gap: 6,
      marginTop: -2,
      marginBottom: 6,
    },
    personaBadge: {
      borderWidth: 1,
      borderColor: colors.warning,
      paddingHorizontal: 6,
      paddingVertical: 2,
    },
    personaBadgeText: {
      color: colors.warning,
      fontSize: 9,
      letterSpacing: 1.5,
      textTransform: "uppercase",
      fontFamily: "Inter_600SemiBold",
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
        <View style={{ marginTop: 8 }}>
          <PlacedByToggle testID="verticals-placed-by-toggle" />
        </View>
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
              onPress={() => clear()}
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
      {isAdmin && overrideVerticals.length > 0 ? (
        <View
          style={styles.overrideBanner}
          testID="persona-override-summary"
        >
          <Text style={styles.overrideTitle}>Persona overrides</Text>
          <Text style={styles.overrideBody}>
            {overrideVerticals.length}{" "}
            {overrideVerticals.length === 1 ? "vertical is" : "verticals are"}{" "}
            running with non-default personas.
          </Text>
          <View style={styles.overrideChipRow}>
            {overrideVerticals.map((v) => (
              <View
                key={v}
                style={styles.overrideChip}
                testID={`persona-override-chip-${v}`}
              >
                <Text style={styles.overrideChipText}>
                  {v} · {disabledByVertical[v].length} OFF
                </Text>
              </View>
            ))}
          </View>
        </View>
      ) : null}
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
          <Pressable
            style={[styles.card, item.hidden && styles.hidden]}
            testID={`card-vertical-${item.vertical}`}
            onPress={() => drillTo(item.vertical)}
          >
            <View style={styles.cardHeader}>
              <Text style={styles.title}>
                {item.vertical}
                {item.pinned ? "  ★" : ""}
              </Text>
              <Text style={styles.label}>{item.trades} trades</Text>
            </View>
            {isAdmin && disabledByVertical[item.vertical] ? (
              <View style={styles.cardBadgeRow}>
                <View
                  style={styles.personaBadge}
                  testID={`badge-persona-override-${item.vertical}`}
                >
                  <Text style={styles.personaBadgeText}>
                    {disabledByVertical[item.vertical].length} persona
                    {disabledByVertical[item.vertical].length === 1
                      ? ""
                      : "s"}{" "}
                    off
                  </Text>
                </View>
              </View>
            ) : null}
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
            <Text
              style={[styles.label, { marginTop: 6, color: colors.primary }]}
              testID={`link-drill-${item.vertical}`}
            >
              TAP TO FILTER DASHBOARD →
            </Text>
          </Pressable>
        )}
      />
    </View>
  );
}
