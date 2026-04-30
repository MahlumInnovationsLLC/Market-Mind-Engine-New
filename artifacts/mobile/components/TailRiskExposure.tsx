import { useQuery } from "@tanstack/react-query";
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { usePlacedBy } from "@/lib/placed-by-filter";
import { useEffectiveVerticals } from "@/lib/vertical-filter";

import { Card, SectionHeader } from "./Card";

/**
 * Mobile tail-risk exposure tile (task #744).
 *
 * Mirrors the web `TailRiskExposurePanel` against the same
 * `/api/public/tail-risk-exposure` endpoint so the operator sees the
 * same headline tone (RED / WATCH / CONTAINED / NO EXPOSURE) and
 * bucket roll-up on phone as on desktop.  Bucket boundaries + tone
 * thresholds are owned by the server — no client-side bucketing or
 * tone classification, so a future taxonomy change flows through with
 * zero client redeploy.
 *
 * Threads `placedBy` from the existing module-level pub/sub state and
 * the `useEffectiveVerticals` derived list (= explicit chip selection
 * intersected with the visibility set so a non-admin operator never sees
 * exposure from an admin-hidden vertical) so the tile narrows in
 * lock-step with the web `TailRiskExposurePanel` and never silently
 * leaks hidden P&L.
 */

type BucketRow = {
  vertical: string;
  open_count: number;
  open_cost_usd: number;
  open_max_loss_usd: number;
  realized_count: number;
  realized_wins: number;
  realized_pnl_usd: number;
};

type Bucket = {
  label: string;
  lo: number;
  hi: number;
  rows: BucketRow[];
  totals: {
    open_count: number;
    open_cost_usd: number;
    open_max_loss_usd: number;
    realized_count: number;
    realized_wins: number;
    realized_pnl_usd: number;
  };
};

type TailRiskExposure = {
  days: number;
  buckets: Bucket[];
  totals: Bucket["totals"];
  tail_concentration: {
    tail_max_loss_usd: number;
    total_max_loss_usd: number;
    pct: number | null;
    tone: "red" | "amber" | "green" | "empty";
  };
};

function fmtUsd(n: number): string {
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, {
    maximumFractionDigits: 0,
  })}`;
}

function fmtPct(n: number | null, digits = 1): string {
  if (n === null || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

const TONE_LABEL: Record<TailRiskExposure["tail_concentration"]["tone"], string> = {
  red: "ELEVATED",
  amber: "WATCH",
  green: "CONTAINED",
  empty: "NO EXPOSURE",
};

export default function TailRiskExposure() {
  const colors = useColors();
  const { placedBy } = usePlacedBy();
  // Same source as the web `TailRiskExposurePanel` — combine the
  // operator's chip selection with admin-managed hidden-vertical
  // suppression so a non-admin can never see exposure from a hidden
  // vertical (parity with `useVerticalFilter().effectiveVerticals` on
  // web).  Sort before joining so the cache key is stable regardless of
  // click order.
  const effective = useEffectiveVerticals();
  const verticalsCsv = effective.slice().sort().join(",");

  const q = useQuery<TailRiskExposure>({
    queryKey: ["public-tail-risk-exposure", placedBy, verticalsCsv],
    queryFn: () => {
      const params = new URLSearchParams({
        days: "30",
        placed_by: placedBy,
      });
      if (verticalsCsv) params.set("vertical", verticalsCsv);
      return apiFetch<TailRiskExposure>(
        `/api/public/tail-risk-exposure?${params.toString()}`,
      );
    },
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const data = q.data;
  const concentration = data?.tail_concentration;

  // Tone → palette mapping picks tokens that already exist in the
  // mobile theme so the tile keys off the same colours as P&L cards.
  const toneColor = (() => {
    if (!concentration) return colors.mutedForeground;
    switch (concentration.tone) {
      case "red":
        return colors.destructive;
      case "amber":
        return colors.warning;
      case "green":
        return colors.foreground;
      case "empty":
      default:
        return colors.mutedForeground;
    }
  })();

  const pnlColor = (n: number) => {
    if (n < 0) return colors.destructive;
    if (n > 0) return colors.foreground;
    return colors.mutedForeground;
  };

  return (
    <>
      <SectionHeader>RISK :: TAIL EXPOSURE</SectionHeader>
      <Card testID="card-tail-risk-exposure">
        {q.isLoading ? (
          <Text
            style={[styles.muted, { color: colors.mutedForeground }]}
            testID="tail-risk-loading"
          >
            Loading exposure…
          </Text>
        ) : q.isError ? (
          <Text
            style={[styles.muted, { color: colors.destructive }]}
            testID="tail-risk-error"
          >
            Failed to load tail-risk exposure
          </Text>
        ) : data && concentration ? (
          <>
            {/* Headline — server-classified tone keeps every surface
                in agreement on what counts as "elevated". */}
            <View style={styles.headlineRow} testID="tail-risk-headline">
              <Text
                style={[styles.eyebrow, { color: colors.mutedForeground }]}
              >
                TAIL CONCENTRATION
              </Text>
              <View style={styles.headlineValueRow}>
                <Text
                  style={[styles.headlineValue, { color: toneColor }]}
                  testID="tail-risk-headline-value"
                  // RNTL queries data-* via the `accessibilityLabel`
                  // hint slot — but for headless jest we expose the
                  // tone via testID + a visible badge instead.
                >
                  {fmtPct(concentration.pct)}
                </Text>
                <Text
                  style={[styles.headlineTone, { color: toneColor }]}
                  testID={`tail-risk-headline-tone-${concentration.tone}`}
                >
                  {TONE_LABEL[concentration.tone]}
                </Text>
              </View>
              <Text
                style={[styles.subtle, { color: colors.mutedForeground }]}
              >
                {fmtUsd(concentration.tail_max_loss_usd)} of{" "}
                {fmtUsd(concentration.total_max_loss_usd)} open max-loss in price &lt;$0.25
              </Text>
            </View>

            {/* Bucket rollup — only render buckets with any open OR
                realized activity to keep the mobile tile compact. */}
            {data.buckets
              .filter(
                (b) =>
                  b.totals.open_count > 0 || b.totals.realized_count > 0,
              )
              .map((b, idx) => {
                const isTail = b.lo < 0.20;
                const winRate =
                  b.totals.realized_count > 0
                    ? b.totals.realized_wins / b.totals.realized_count
                    : null;
                return (
                  <View
                    key={b.label}
                    style={[
                      styles.bucketRow,
                      {
                        borderTopColor: colors.border,
                        borderTopWidth: idx === 0 ? 0 : 1,
                      },
                    ]}
                    testID={`tail-risk-bucket-${b.label}`}
                  >
                    <View style={styles.bucketHeaderRow}>
                      <Text
                        style={[styles.bucketLabel, { color: colors.foreground }]}
                      >
                        {b.label}
                        {isTail ? (
                          <Text
                            style={[styles.tailBadge, { color: colors.destructive }]}
                          >
                            {" "}
                            TAIL
                          </Text>
                        ) : null}
                      </Text>
                      <Text
                        style={[
                          styles.bucketMaxLoss,
                          {
                            color:
                              isTail && b.totals.open_max_loss_usd > 0
                                ? colors.destructive
                                : colors.foreground,
                          },
                        ]}
                      >
                        {fmtUsd(b.totals.open_max_loss_usd)}
                      </Text>
                    </View>
                    <View style={styles.bucketMetaRow}>
                      <Text
                        style={[styles.muted, { color: colors.mutedForeground }]}
                      >
                        {b.totals.open_count} open · 30d{" "}
                        <Text
                          style={{ color: pnlColor(b.totals.realized_pnl_usd) }}
                        >
                          {fmtUsd(b.totals.realized_pnl_usd)}
                        </Text>
                        {winRate !== null
                          ? ` · ${fmtPct(winRate, 0)} win`
                          : ""}
                      </Text>
                    </View>
                  </View>
                );
              })}

            {/* When there's no activity at all, give the operator a
                clear empty-state instead of an invisible card. */}
            {data.totals.open_count === 0 &&
            data.totals.realized_count === 0 ? (
              <Text
                style={[styles.muted, { color: colors.mutedForeground }]}
                testID="tail-risk-empty"
              >
                No open exposure or recent fills in the selected slice.
              </Text>
            ) : null}
          </>
        ) : null}
      </Card>
    </>
  );
}

const styles = StyleSheet.create({
  muted: {
    fontSize: 12,
    fontFamily: "Inter_400Regular",
    paddingVertical: 8,
  },
  eyebrow: {
    fontSize: 10,
    letterSpacing: 2,
    fontFamily: "Inter_500Medium",
  },
  headlineRow: {
    paddingVertical: 6,
    gap: 4,
  },
  headlineValueRow: {
    flexDirection: "row",
    alignItems: "baseline",
    gap: 8,
  },
  headlineValue: {
    fontSize: 28,
    fontFamily: "Inter_700Bold",
    letterSpacing: 0.5,
  },
  headlineTone: {
    fontSize: 11,
    letterSpacing: 1.8,
    fontFamily: "Inter_700Bold",
  },
  subtle: {
    fontSize: 11,
    fontFamily: "Inter_400Regular",
    marginTop: 2,
  },
  bucketRow: {
    paddingVertical: 8,
  },
  bucketHeaderRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "baseline",
  },
  bucketLabel: {
    fontSize: 13,
    fontFamily: "Inter_600SemiBold",
  },
  tailBadge: {
    fontSize: 10,
    letterSpacing: 1.4,
    fontFamily: "Inter_700Bold",
  },
  bucketMaxLoss: {
    fontSize: 13,
    fontFamily: "Inter_700Bold",
  },
  bucketMetaRow: {
    marginTop: 2,
  },
});
