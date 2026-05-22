/**
 * Mobile parity for the operator dashboard's Top-Edges-Today +
 * Bookmaker-Metrics tiles (task #832).
 *
 * Both tiles refetch every 30s, surface a "computed at" stamp, and
 * use the same JSON contract as the web tiles via the shared API.
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import React, { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { Card, SectionHeader } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";

type TopEdgeItem = {
  prediction_id: number;
  matchup: string;
  league: string | null;
  vertical: string;
  vertical_halted: boolean;
  model_prob: number | null;
  market_price: number | null;
  edge_pct: number | null;
  confidence: number | null;
  planned_stake: number | null;
  kalshi_market_id: string | null;
};

type TopEdgesResponse = {
  limit: number;
  generated_at: string;
  items: TopEdgeItem[];
  halted_verticals: string[];
  error: string | null;
};

type BookmakerResponse = {
  window: "7d" | "30d" | "ytd";
  generated_at: string;
  profit_factor: number | null;
  equivalent_unit_pnl: number | null;
  unit_dollars: number;
  win_rate: number | null;
  n_settled: number;
  top_confidence_today: number | null;
  highest_edge_today: number | null;
  picks_today: { total: number; by_vertical: { vertical: string; n: number }[] };
  error: string | null;
};

const WINDOWS: BookmakerResponse["window"][] = ["7d", "30d", "ytd"];

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return s;
    return d.toLocaleTimeString();
  } catch {
    return s;
  }
}
function fmtProb(n: number | null | undefined, d = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(Number(n) * 100).toFixed(d)}%`;
}
function fmtPct(n: number | null | undefined, d = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${Number(n).toFixed(d)}%`;
}
function fmtUnits(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}u`;
}

export function TopEdgesTile() {
  const colors = useColors();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["dashboard", "top-edges"],
    queryFn: () => apiFetch<TopEdgesResponse>("/api/dashboard/top-edges?limit=25"),
    refetchInterval: 30_000,
  });
  const [pending, setPending] = useState<number | null>(null);

  const snooze = useMutation({
    mutationFn: (id: number) =>
      apiFetch(`/api/dashboard/candidates/${id}/snooze`, {
        method: "POST",
        body: JSON.stringify({ hours: 1, reason: "operator snooze (1h)" }),
        headers: { "content-type": "application/json" },
      }),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: ["dashboard", "top-edges"] }),
  });
  const fire = useMutation({
    mutationFn: (id: number) =>
      apiFetch(`/api/dashboard/candidates/${id}/fire`, { method: "POST" }),
    onSettled: () =>
      qc.invalidateQueries({ queryKey: ["dashboard", "top-edges"] }),
  });

  const data = q.data;
  return (
    <Card testID="card-top-edges">
      <SectionHeader>
        TOP EDGES TODAY · {data?.items?.length ?? 0}
      </SectionHeader>
      <Text style={[styles.subtle, { color: colors.mutedForeground }]}>
        Computed {fmtTime(data?.generated_at)} · cap {data?.limit ?? 25}
      </Text>
      {q.isError ? (
        <Text style={[styles.err, { color: colors.destructive }]}
              testID="top-edges-error">
          Failed to load top edges.
        </Text>
      ) : null}
      {data?.error ? (
        <Text style={[styles.err, { color: colors.destructive }]}
              testID="top-edges-server-error">
          {data.error}
        </Text>
      ) : null}
      {!q.isLoading && (data?.items?.length ?? 0) === 0 ? (
        <Text style={[styles.empty, { color: colors.mutedForeground }]}
              testID="top-edges-empty">
          NO PENDING CANDIDATES
        </Text>
      ) : null}
      {(data?.items ?? []).map((row) => {
        const halted = row.vertical_halted;
        const busy = pending === row.prediction_id;
        return (
          <View
            key={row.prediction_id}
            testID={`top-edge-row-${row.prediction_id}`}
            style={[
              styles.row,
              {
                borderTopColor: colors.border,
                opacity: halted ? 0.5 : 1,
              },
            ]}
          >
            <View style={{ flex: 1 }}>
              <Text style={[styles.matchup, { color: colors.foreground }]}>
                {row.matchup}
              </Text>
              <Text style={[styles.meta, { color: colors.mutedForeground }]}>
                {row.vertical.toUpperCase()}
                {row.league ? ` · ${row.league}` : ""}
                {halted ? " · HALTED" : ""}
              </Text>
            </View>
            <Text
              style={[styles.edge, { color: colors.foreground }]}
              testID={`top-edge-edge-${row.prediction_id}`}
            >
              {fmtPct(row.edge_pct)}
            </Text>
            <Pressable
              testID={`top-edge-fire-${row.prediction_id}`}
              disabled={halted || busy}
              onPress={async () => {
                setPending(row.prediction_id);
                try { await fire.mutateAsync(row.prediction_id); }
                finally { setPending(null); }
              }}
              style={[styles.btn, {
                borderColor: colors.border,
                opacity: halted ? 0.4 : 1,
              }]}
            >
              <Text style={[styles.btnTxt, { color: colors.foreground }]}>FIRE</Text>
            </Pressable>
            <Pressable
              testID={`top-edge-snooze-${row.prediction_id}`}
              disabled={halted || busy}
              onPress={async () => {
                setPending(row.prediction_id);
                try { await snooze.mutateAsync(row.prediction_id); }
                finally { setPending(null); }
              }}
              style={[styles.btn, {
                borderColor: colors.border,
                opacity: halted ? 0.4 : 1,
              }]}
            >
              <Text style={[styles.btnTxt, { color: colors.foreground }]}>SNOOZE</Text>
            </Pressable>
          </View>
        );
      })}
      {data?.halted_verticals?.length ? (
        <Text style={[styles.subtle, { color: colors.mutedForeground }]}>
          Halted: {data.halted_verticals.join(", ")}
        </Text>
      ) : null}
    </Card>
  );
}

export function BookmakerMetricsTile() {
  const colors = useColors();
  const [w, setW] = useState<BookmakerResponse["window"]>("7d");
  const q = useQuery({
    queryKey: ["dashboard", "bookmaker-metrics", w],
    queryFn: () =>
      apiFetch<BookmakerResponse>(`/api/dashboard/bookmaker-metrics?window=${w}`),
    refetchInterval: 30_000,
  });
  const data = q.data;
  return (
    <Card testID="card-bookmaker-metrics">
      <SectionHeader>BOOKMAKER METRICS · {w.toUpperCase()}</SectionHeader>
      <Text style={[styles.subtle, { color: colors.mutedForeground }]}>
        Computed {fmtTime(data?.generated_at)}
      </Text>
      <View style={styles.windowRow}>
        {WINDOWS.map((it) => {
          const active = it === w;
          return (
            <Pressable
              key={it}
              testID={`bm-window-${it}`}
              onPress={() => setW(it)}
              style={[
                styles.windowChip,
                {
                  borderColor: colors.border,
                  backgroundColor: active ? colors.primary : "transparent",
                },
              ]}
            >
              <Text style={[styles.windowLbl, {
                color: active ? colors.primaryForeground : colors.mutedForeground,
              }]}>
                {it.toUpperCase()}
              </Text>
            </Pressable>
          );
        })}
      </View>
      {q.isError ? (
        <Text style={[styles.err, { color: colors.destructive }]}
              testID="bm-error">
          Failed to load bookmaker metrics.
        </Text>
      ) : null}
      <View style={styles.grid}>
        <Cell label="PROFIT FACTOR" value={
          data?.profit_factor == null ? "—" : data.profit_factor.toFixed(2)
        } testID="bm-profit-factor" />
        <Cell label={`EQUIV UNIT P&L (1u=$${data?.unit_dollars ?? 100})`}
              value={fmtUnits(data?.equivalent_unit_pnl)}
              testID="bm-unit-pnl" />
        <Cell label={`HIT RATE (n=${data?.n_settled ?? 0})`}
              value={fmtProb(data?.win_rate)}
              testID="bm-hit-rate" />
        <Cell label="TOP CONFIDENCE (TODAY)"
              value={fmtProb(data?.top_confidence_today)}
              testID="bm-top-confidence" />
        <Cell label="HIGHEST EDGE (TODAY)"
              value={fmtPct(data?.highest_edge_today)}
              testID="bm-highest-edge" />
        <Cell label="PICKS TODAY"
              value={String(data?.picks_today?.total ?? 0)}
              hint={
                (data?.picks_today?.by_vertical ?? [])
                  .map((b) => `${b.vertical}:${b.n}`).join(" ") || "—"
              }
              testID="bm-picks-today" />
      </View>
    </Card>
  );
}

function Cell({ label, value, hint, testID }: {
  label: string; value: string; hint?: string; testID?: string;
}) {
  const colors = useColors();
  return (
    <View style={styles.cell} testID={testID}>
      <Text style={[styles.cellLbl, { color: colors.mutedForeground }]}>{label}</Text>
      <Text style={[styles.cellVal, { color: colors.foreground }]}>{value}</Text>
      {hint ? (
        <Text style={[styles.cellHint, { color: colors.mutedForeground }]}>{hint}</Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  subtle: { fontSize: 10, fontFamily: "Inter_400Regular", marginBottom: 6 },
  empty: { fontSize: 12, fontFamily: "Inter_500Medium", marginTop: 8 },
  err: { fontSize: 11, fontFamily: "Inter_500Medium", marginTop: 6 },
  row: {
    flexDirection: "row", alignItems: "center", gap: 6,
    paddingVertical: 8, borderTopWidth: 1,
  },
  matchup: { fontSize: 13, fontFamily: "Inter_600SemiBold" },
  meta: { fontSize: 10, fontFamily: "Inter_400Regular", marginTop: 2 },
  edge: {
    fontSize: 14, fontFamily: "Inter_700Bold",
    minWidth: 56, textAlign: "right",
  },
  btn: { borderWidth: 1, paddingHorizontal: 8, paddingVertical: 4 },
  btnTxt: { fontSize: 10, letterSpacing: 1, fontFamily: "Inter_600SemiBold" },
  windowRow: { flexDirection: "row", gap: 4, marginVertical: 6 },
  windowChip: { borderWidth: 1, paddingVertical: 4, paddingHorizontal: 10 },
  windowLbl: { fontSize: 10, letterSpacing: 1.5, fontFamily: "Inter_600SemiBold" },
  grid: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 6 },
  cell: { minWidth: 130, flexGrow: 1 },
  cellLbl: { fontSize: 9, letterSpacing: 1, fontFamily: "Inter_500Medium" },
  cellVal: { fontSize: 18, fontFamily: "Inter_700Bold", marginTop: 2 },
  cellHint: { fontSize: 10, fontFamily: "Inter_400Regular" },
});
