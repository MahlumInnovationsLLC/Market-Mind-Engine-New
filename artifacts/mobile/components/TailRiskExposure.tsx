import { useQuery } from "@tanstack/react-query";
import React from "react";
import { StyleSheet, Text, View } from "react-native";

import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { usePlacedBy } from "@/lib/placed-by-filter";
import { useEffectiveVerticals } from "@/lib/vertical-filter";

import { Card, SectionHeader } from "./Card";

// See replit.md "Tail-risk exposure tile".

type BucketRow = {
  vertical: string;
  open_count: number;
  open_cost_usd: number;
  open_max_loss_usd: number;
  realized_count: number;
  realized_wins: number;
  realized_pnl_usd: number;
  ev_per_trade: number | null;
};

type BucketTotals = {
  open_count: number;
  open_cost_usd: number;
  open_max_loss_usd: number;
  realized_count: number;
  realized_wins: number;
  realized_pnl_usd: number;
  ev_per_trade: number | null;
};

type Bucket = {
  label: string;
  lo: number;
  hi: number;
  is_tail: boolean;
  rows: BucketRow[];
  totals: BucketTotals;
};

type TailRiskExposure = {
  days: number;
  buckets: Bucket[];
  totals: BucketTotals;
  tail_concentration: {
    tail_max_loss_usd: number;
    total_max_loss_usd: number;
    pct: number | null;
    tone: "red" | "amber" | "green" | "empty";
    threshold_price: number;
  };
  source?: string;
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

function fmtEv(n: number | null): string {
  if (n === null || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function fmtPriceThreshold(p: number): string {
  return `$${p.toFixed(2)}`;
}

const DEFAULT_TAIL_THRESHOLD = 0.2;

const TONE_LABEL: Record<TailRiskExposure["tail_concentration"]["tone"], string> = {
  red: "ELEVATED",
  amber: "WATCH",
  green: "CONTAINED",
  empty: "NO EXPOSURE",
};

type TailRiskExposureProps = {
  days?: number;
  period?: number;
};

const PERIOD_LABEL: Record<number, string> = {
  1: "1D",
  7: "7D",
  30: "30D",
  365: "YTD",
};

export default function TailRiskExposure({
  days = 30,
  period,
}: TailRiskExposureProps = {}) {
  const colors = useColors();
  const { placedBy } = usePlacedBy();
  const effective = useEffectiveVerticals();
  const verticalsCsv = effective.slice().sort().join(",");
  const periodLabel = PERIOD_LABEL[period ?? days] ?? `${days}D`;

  const q = useQuery<TailRiskExposure>({
    queryKey: ["public-tail-risk-exposure", placedBy, verticalsCsv, days],
    queryFn: () => {
      const params = new URLSearchParams({
        days: String(days),
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
      <SectionHeader>{`RISK · ${periodLabel} :: TAIL EXPOSURE`}</SectionHeader>
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
                testID="tail-risk-headline-detail"
              >
                {fmtUsd(concentration.tail_max_loss_usd)} of{" "}
                {fmtUsd(concentration.total_max_loss_usd)} open max-loss
                in trades priced &lt;
                {fmtPriceThreshold(
                  concentration.threshold_price ?? DEFAULT_TAIL_THRESHOLD,
                )}
              </Text>
            </View>

            {data.buckets
              .filter(
                (b) =>
                  b.totals.open_count > 0 || b.totals.realized_count > 0,
              )
              .map((b, idx) => {
                const isTail = b.is_tail;
                const winRate =
                  b.totals.realized_count > 0
                    ? b.totals.realized_wins / b.totals.realized_count
                    : null;
                const cells = b.rows
                  .slice()
                  .sort((a, c) => c.open_max_loss_usd - a.open_max_loss_usd);
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
                        {`${b.totals.open_count} open · `}
                        <Text
                          testID={`tail-risk-bucket-${b.label}-notional`}
                        >
                          {fmtUsd(b.totals.open_cost_usd)} notional
                        </Text>
                        {` · ${periodLabel} `}
                        <Text
                          style={{ color: pnlColor(b.totals.realized_pnl_usd) }}
                        >
                          {fmtUsd(b.totals.realized_pnl_usd)}
                        </Text>
                        {winRate !== null
                          ? ` · ${fmtPct(winRate, 0)} win`
                          : ""}
                        {b.totals.ev_per_trade !== null
                          ? (
                              <Text
                                testID={`tail-risk-bucket-${b.label}-ev`}
                                style={{ color: pnlColor(b.totals.ev_per_trade) }}
                              >
                                {" · "}
                                {fmtEv(b.totals.ev_per_trade)}/trade
                              </Text>
                            )
                          : null}
                      </Text>
                    </View>
                    {cells.map((cell) => (
                      <View
                        key={`${b.label}-${cell.vertical}`}
                        style={styles.cellRow}
                        testID={`tail-risk-cell-${b.label}-${cell.vertical}`}
                      >
                        <Text
                          style={[
                            styles.cellLabel,
                            { color: colors.mutedForeground },
                          ]}
                        >
                          ↳ {cell.vertical}
                        </Text>
                        <Text
                          style={[
                            styles.cellMeta,
                            { color: colors.mutedForeground },
                          ]}
                        >
                          {cell.open_count}× ·{" "}
                          <Text
                            testID={`tail-risk-cell-${b.label}-${cell.vertical}-notional`}
                          >
                            {fmtUsd(cell.open_cost_usd)}
                          </Text>
                          {" notional · "}
                          {fmtUsd(cell.open_max_loss_usd)} max-loss ·{" "}
                          <Text
                            style={{ color: pnlColor(cell.realized_pnl_usd) }}
                          >
                            {fmtUsd(cell.realized_pnl_usd)}
                          </Text>
                          {cell.ev_per_trade !== null ? (
                            <Text
                              style={{
                                color: pnlColor(cell.ev_per_trade),
                              }}
                            >
                              {" · "}
                              {fmtEv(cell.ev_per_trade)}/t
                            </Text>
                          ) : null}
                        </Text>
                      </View>
                    ))}
                  </View>
                );
              })}

            {(data.totals.open_count > 0 || data.totals.realized_count > 0) ? (
              <View
                style={[
                  styles.bucketRow,
                  styles.totalsRow,
                  { borderTopColor: colors.border },
                ]}
                testID="tail-risk-totals-row"
              >
                <View style={styles.bucketHeaderRow}>
                  <Text
                    style={[styles.bucketLabel, { color: colors.foreground }]}
                  >
                    Total
                  </Text>
                  <Text
                    style={[styles.bucketMaxLoss, { color: colors.foreground }]}
                  >
                    {fmtUsd(data.totals.open_max_loss_usd)}
                  </Text>
                </View>
                <View style={styles.bucketMetaRow}>
                  <Text
                    style={[styles.muted, { color: colors.mutedForeground }]}
                    testID="tail-risk-totals-meta"
                  >
                    {`${data.totals.open_count} open · `}
                    <Text testID="tail-risk-totals-notional">
                      {fmtUsd(data.totals.open_cost_usd)} notional
                    </Text>
                    {` · ${periodLabel} `}
                    <Text
                      style={{ color: pnlColor(data.totals.realized_pnl_usd) }}
                    >
                      {fmtUsd(data.totals.realized_pnl_usd)}
                    </Text>
                    {data.totals.realized_count > 0
                      ? ` · ${fmtPct(
                          data.totals.realized_wins / data.totals.realized_count,
                          0,
                        )} win`
                      : ""}
                    {data.totals.ev_per_trade !== null ? (
                      <Text
                        style={{ color: pnlColor(data.totals.ev_per_trade) }}
                      >
                        {" · "}
                        {fmtEv(data.totals.ev_per_trade)}/trade
                      </Text>
                    ) : null}
                  </Text>
                </View>
              </View>
            ) : null}

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
  totalsRow: {
    borderTopWidth: 1,
    marginTop: 4,
    paddingTop: 8,
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
  cellRow: {
    marginTop: 4,
    paddingLeft: 12,
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "baseline",
    gap: 8,
  },
  cellLabel: {
    fontSize: 11,
    fontFamily: "Inter_500Medium",
  },
  cellMeta: {
    fontSize: 11,
    fontFamily: "Inter_400Regular",
    flexShrink: 1,
    textAlign: "right",
  },
});
