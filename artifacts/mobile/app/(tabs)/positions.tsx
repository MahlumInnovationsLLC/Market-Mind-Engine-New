import { useQuery } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
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
import { PlacedByToggle } from "@/components/PlacedByToggle";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { usePlacedBy } from "@/lib/placed-by-filter";

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
  const router = useRouter();
  const [filter, setFilter] = useState<Filter>("all");
  // Trade-provenance filter (T005). Shared module-level state so the
  // selection persists across tab switches.
  const { placedBy } = usePlacedBy();

  // Bucket drill-in from the tail-risk tile. lo/hi are fractional
  // dollars (0..1), matching trade.price units, so we apply them
  // directly. We also accept a single vertical (CSV is ignored — the
  // tile sends them comma-joined when multiple are selected).
  const params = useLocalSearchParams<{
    bucket_lo?: string;
    bucket_hi?: string;
    bucket_label?: string;
    vertical?: string;
    days?: string;
    sort?: string;
    placed_by?: string;
  }>();
  const bucketLo =
    typeof params.bucket_lo === "string"
      ? Number.parseFloat(params.bucket_lo)
      : NaN;
  const bucketHi =
    typeof params.bucket_hi === "string"
      ? Number.parseFloat(params.bucket_hi)
      : NaN;
  const bucket =
    Number.isFinite(bucketLo) && Number.isFinite(bucketHi) && bucketHi > bucketLo
      ? {
          lo: bucketLo,
          hi: bucketHi,
          label:
            (typeof params.bucket_label === "string" && params.bucket_label) ||
            `$${bucketLo.toFixed(2)}–$${bucketHi.toFixed(2)}`,
        }
      : null;
  // Treat the `vertical` param as a CSV inclusion set so multi-vertical
  // drill-ins from the tile actually scope the trade list.
  const verticalSet = useMemo(() => {
    if (typeof params.vertical !== "string" || params.vertical.length === 0)
      return null;
    const parts = params.vertical
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter((s) => s.length > 0);
    return parts.length > 0 ? new Set(parts) : null;
  }, [params.vertical]);
  const verticalParam =
    typeof params.vertical === "string" && !params.vertical.includes(",")
      ? params.vertical
      : null;
  // URL placed_by overrides the dashboard provenance toggle so the
  // drill-in honours the same scope the tile was filtered by.
  const placedByOverride =
    params.placed_by === "manual" || params.placed_by === "system"
      ? params.placed_by
      : null;
  const effectivePlacedBy = placedByOverride ?? placedBy;
  const daysParam =
    typeof params.days === "string" && /^\d+$/.test(params.days)
      ? Number.parseInt(params.days, 10)
      : null;
  const sortMaxLoss = params.sort === "max_loss";

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["dashboard", "recent-trades", effectivePlacedBy],
    queryFn: () =>
      apiFetch<TradeList>(
        `/api/dashboard/recent-trades?limit=50&placed_by=${effectivePlacedBy}`,
      ),
    refetchInterval: 30_000,
  });

  const filtered = useMemo(() => {
    let items = data?.items ?? [];
    // Apply the URL drill-in filters first so they compose with the
    // operator's chip selection.
    if (bucket) {
      items = items.filter((t) => {
        if (typeof t.price !== "number") return false;
        return t.price >= bucket.lo && t.price < bucket.hi;
      });
    }
    if (verticalSet) {
      items = items.filter((t) =>
        verticalSet.has((t.vertical ?? "").toLowerCase()),
      );
    } else if (verticalParam) {
      items = items.filter(
        (t) => (t.vertical ?? "").toLowerCase() === verticalParam.toLowerCase(),
      );
    }
    if (daysParam !== null) {
      const cutoffMs = Date.now() - daysParam * 86_400_000;
      items = items.filter((t) => {
        if (!t.executed_at) return true;
        const ts = new Date(t.executed_at).getTime();
        return Number.isNaN(ts) || ts >= cutoffMs;
      });
    }
    switch (filter) {
      case "live":
        items = items.filter(
          (t) => (t.trading_mode ?? "").toLowerCase() === "live",
        );
        break;
      case "paper":
        items = items.filter(
          (t) => (t.trading_mode ?? "").toLowerCase() === "paper",
        );
        break;
      case "wins":
        items = items.filter((t) => (t.pnl ?? 0) > 0);
        break;
      case "losses":
        items = items.filter((t) => (t.pnl ?? 0) < 0);
        break;
    }
    if (sortMaxLoss) {
      // Per-trade max-loss = qty × price (worst case settles at $0).
      items = [...items].sort((a, b) => {
        const al = (a.quantity ?? 0) * (a.price ?? 0);
        const bl = (b.quantity ?? 0) * (b.price ?? 0);
        return bl - al;
      });
    }
    return items;
  }, [data, filter, bucket, verticalSet, verticalParam, daysParam, sortMaxLoss]);

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
          {(bucket ||
            verticalParam ||
            verticalSet ||
            daysParam !== null ||
            placedByOverride) && (
            <View
              style={[
                styles.drillBanner,
                { borderColor: colors.destructive },
              ]}
              testID="positions-bucket-banner"
            >
              <View style={styles.drillBannerRow}>
                <Text
                  style={[
                    styles.drillBannerLabel,
                    { color: colors.destructive },
                  ]}
                  testID="positions-bucket-chip"
                >
                  {bucket
                    ? `BUCKET ${bucket.label}`
                    : "TAIL-RISK DRILL"}
                </Text>
                <Pressable
                  onPress={() => router.push("/(tabs)/positions" as never)}
                  testID="positions-bucket-clear"
                >
                  <Text
                    style={[
                      styles.drillBannerClear,
                      { color: colors.mutedForeground },
                    ]}
                  >
                    CLEAR
                  </Text>
                </Pressable>
              </View>
              <Text
                style={[
                  styles.drillBannerMeta,
                  { color: colors.mutedForeground },
                ]}
              >
                {[
                  daysParam !== null ? `${daysParam}D` : null,
                  verticalSet
                    ? Array.from(verticalSet).join("+").toUpperCase()
                    : verticalParam
                      ? verticalParam.toUpperCase()
                      : null,
                  placedByOverride ? placedByOverride.toUpperCase() : null,
                  sortMaxLoss ? "BY MAX-LOSS" : null,
                ]
                  .filter((s): s is string => !!s)
                  .join(" · ")}
              </Text>
            </View>
          )}
          <PlacedByToggle testID="positions-placed-by-toggle" />
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
  drillBanner: {
    borderWidth: 1,
    paddingHorizontal: 10,
    paddingVertical: 8,
    marginBottom: 8,
    gap: 4,
  },
  drillBannerRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  drillBannerLabel: {
    fontSize: 11,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
  drillBannerClear: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
  drillBannerMeta: {
    fontSize: 10,
    letterSpacing: 1.2,
    fontFamily: "Inter_500Medium",
  },
});
