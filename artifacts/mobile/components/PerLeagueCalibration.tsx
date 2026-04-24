/**
 * Per-league calibration panel for the mobile dashboard.
 *
 * Mirrors the operator-web component
 * (`artifacts/web/src/components/dashboard/per-league-calibration.tsx`)
 * so an operator on the road sees the same calibrator health from
 * their phone — one tile per supported league with the raw→calibrated
 * curve, the sample count, and the Brier-before/after delta.
 *
 * The endpoint (`GET /api/dashboard/calibration/per-league`) is already
 * live and returns the same payload shape used by the web tile.
 */
import { useQuery } from "@tanstack/react-query";
import React, { useMemo } from "react";
import { StyleSheet, Text, View } from "react-native";
import Svg, { Circle, Line, Path, Rect } from "react-native-svg";

import { Card, SectionHeader } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { ApiError, apiFetch } from "@/lib/api";

interface CurvePoint {
  p_raw: number;
  p_cal: number;
}

interface LeagueItem {
  league: string;
  type: "isotonic" | "platt" | null;
  sample_count: number;
  fit_at: string | null;
  brier_before: number | null;
  brier_after: number | null;
  curve: CurvePoint[];
  knots: { x: number[]; y: number[] } | null;
}

interface PerLeagueResponse {
  items: LeagueItem[];
  supported: string[];
  fitted_count?: number;
  error?: string;
}

function fmt(n: number | null | undefined, digits = 4): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(digits);
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return s;
    return d.toLocaleString();
  } catch {
    return s;
  }
}

function deltaTone(
  before: number | null,
  after: number | null,
  colors: ReturnType<typeof useColors>,
): string {
  if (before == null || after == null) return colors.mutedForeground;
  const d = after - before;
  if (d <= -0.005) return colors.success ?? colors.primary;
  if (d >= 0.005) return colors.destructive;
  return colors.mutedForeground;
}

function CalibrationCurve({
  item,
  stroke,
  axis,
}: {
  item: LeagueItem;
  stroke: string;
  axis: string;
}) {
  // Same drawing maths as the web tile — only the host elements differ
  // (react-native-svg vs DOM SVG). Viewbox stays identical so the curve
  // renders the same shape on every screen size.
  const W = 220;
  const H = 140;
  const PAD = 18;
  const innerW = W - PAD * 2;
  const innerH = H - PAD * 2;
  const px = (v: number): number =>
    PAD + Math.max(0, Math.min(1, v)) * innerW;
  const py = (v: number): number =>
    PAD + (1 - Math.max(0, Math.min(1, v))) * innerH;

  const curve = item.curve ?? [];
  const pathD = curve.length
    ? curve
        .map((pt, i) => `${i === 0 ? "M" : "L"} ${px(pt.p_raw)} ${py(pt.p_cal)}`)
        .join(" ")
    : null;

  const knots = item.knots;
  return (
    <Svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H}>
      <Rect
        x={PAD}
        y={PAD}
        width={innerW}
        height={innerH}
        fill="none"
        stroke={axis}
        strokeOpacity={0.4}
      />
      <Line
        x1={px(0)}
        y1={py(0)}
        x2={px(1)}
        y2={py(1)}
        stroke={axis}
        strokeOpacity={0.5}
        strokeDasharray="3,3"
      />
      {pathD ? (
        <Path d={pathD} fill="none" stroke={stroke} strokeWidth={1.6} />
      ) : null}
      {knots
        ? knots.x.map((xv, i) => (
            <Circle
              key={i}
              cx={px(xv)}
              cy={py(knots.y[i])}
              r={1.8}
              fill={stroke}
            />
          ))
        : null}
    </Svg>
  );
}

function LeagueTile({ item }: { item: LeagueItem }): React.JSX.Element {
  const colors = useColors();
  const fitted = item.type !== null;
  const delta =
    item.brier_before != null && item.brier_after != null
      ? item.brier_after - item.brier_before
      : null;

  return (
    <Card style={styles.tile} testID={`tile-calibration-${item.league}`}>
      <View style={styles.tileHead}>
        <Text style={[styles.league, { color: colors.foreground }]}>
          {item.league}
        </Text>
        <Text style={[styles.type, { color: colors.mutedForeground }]}>
          {item.type ?? "no fit"}
        </Text>
      </View>
      {fitted ? (
        <>
          <CalibrationCurve
            item={item}
            stroke={colors.primary}
            axis={colors.foreground}
          />
          <View style={styles.metricsRow}>
            <Text style={[styles.metric, { color: colors.mutedForeground }]}>
              before:{" "}
              <Text style={{ color: colors.foreground }}>
                {fmt(item.brier_before)}
              </Text>
            </Text>
            <Text style={[styles.metric, { color: colors.mutedForeground }]}>
              after:{" "}
              <Text style={{ color: colors.foreground }}>
                {fmt(item.brier_after)}
              </Text>
            </Text>
          </View>
          <View style={styles.metricsRow}>
            <Text style={[styles.metric, { color: colors.mutedForeground }]}>
              n:{" "}
              <Text style={{ color: colors.foreground }}>
                {item.sample_count}
              </Text>
            </Text>
            <Text
              style={[
                styles.metric,
                { color: deltaTone(item.brier_before, item.brier_after, colors) },
              ]}
              testID={`tile-calibration-${item.league}-delta`}
            >
              Δ:{" "}
              {delta == null
                ? "—"
                : (delta >= 0 ? "+" : "") + delta.toFixed(4)}
            </Text>
          </View>
          <Text style={[styles.fitAt, { color: colors.mutedForeground }]}>
            fit: {fmtTime(item.fit_at)}
          </Text>
        </>
      ) : (
        <View style={styles.emptyTile}>
          <Text
            style={[styles.emptyText, { color: colors.mutedForeground }]}
          >
            No fitted calibrator yet — needs ≥10 settled trades.
          </Text>
        </View>
      )}
    </Card>
  );
}

export default function PerLeagueCalibration(): React.JSX.Element {
  const colors = useColors();
  const q = useQuery({
    queryKey: ["dashboard", "calibration", "per-league"],
    queryFn: () =>
      apiFetch<PerLeagueResponse>("/api/dashboard/calibration/per-league"),
    staleTime: 60_000,
    refetchInterval: 120_000,
  });

  const items = useMemo<LeagueItem[]>(() => q.data?.items ?? [], [q.data]);
  const fittedCount =
    q.data?.fitted_count ?? items.filter((i) => i.type !== null).length;

  return (
    <View testID="card-per-league-calibration">
      <SectionHeader>MODELS · PER-LEAGUE CALIBRATION</SectionHeader>
      <Text style={[styles.subtitle, { color: colors.mutedForeground }]}>
        {q.data?.error
          ? `Failed to load: ${q.data.error}`
          : `${fittedCount}/${items.length} leagues fitted · raw→calibrated curve + Brier delta.`}
      </Text>
      {q.isLoading ? (
        <Card>
          <Text style={[styles.muted, { color: colors.mutedForeground }]}>
            Loading calibrators…
          </Text>
        </Card>
      ) : null}
      {q.isError ? (
        <Card>
          <Text style={[styles.muted, { color: colors.destructive }]}>
            {(() => {
              const err = q.error as ApiError | Error | undefined;
              const status =
                err && (err as ApiError).status
                  ? (err as ApiError).status
                  : null;
              const reqId =
                err && (err as ApiError).requestId
                  ? (err as ApiError).requestId
                  : null;
              const head = `Failed to load calibrators${
                status != null ? ` (HTTP ${status})` : ""
              }`;
              return reqId ? `${head}\nrequest_id=${reqId}` : head;
            })()}
          </Text>
        </Card>
      ) : null}
      {!q.isLoading && !q.isError && items.length === 0 ? (
        <Card>
          <Text style={[styles.muted, { color: colors.mutedForeground }]}>
            No calibrator snapshots yet — the nightly refit will populate
            this view.
          </Text>
        </Card>
      ) : null}
      {!q.isLoading && !q.isError && items.length > 0 ? (
        <View style={styles.grid} testID="grid-per-league-calibration">
          {items.map((it) => (
            <LeagueTile key={it.league} item={it} />
          ))}
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  subtitle: {
    fontSize: 11,
    fontFamily: "Inter_400Regular",
    marginTop: -4,
    marginBottom: 8,
    lineHeight: 14,
  },
  grid: {
    gap: 8,
  },
  tile: {
    gap: 6,
  },
  tileHead: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "baseline",
  },
  league: {
    fontSize: 12,
    fontFamily: "Inter_700Bold",
    letterSpacing: 1.5,
  },
  type: {
    fontSize: 10,
    fontFamily: "Inter_500Medium",
    letterSpacing: 1.5,
    textTransform: "uppercase",
  },
  metricsRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    gap: 8,
  },
  metric: {
    fontSize: 11,
    fontFamily: "Inter_400Regular",
  },
  fitAt: {
    fontSize: 10,
    fontFamily: "Inter_400Regular",
  },
  emptyTile: {
    minHeight: 80,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 12,
  },
  emptyText: {
    fontSize: 11,
    fontFamily: "Inter_400Regular",
    textAlign: "center",
  },
  muted: {
    fontSize: 12,
    fontFamily: "Inter_400Regular",
    textAlign: "center",
    paddingVertical: 8,
  },
});
