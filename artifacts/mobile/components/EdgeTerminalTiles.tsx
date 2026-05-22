/**
 * Edge Terminal scraper UI (task #844): input-mode toggle and P&L tile.
 * Mirrors the web components in artifacts/web/src/components/dashboard/
 * (edge-terminal-pnl-tile.tsx, input-mode-toggle.tsx).
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import React from "react";
import { Pressable, StyleSheet, Switch, Text, View } from "react-native";

import { Card, SectionHeader } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";

export type InputMode = "pipeline" | "edge_terminal";

type InputModeResponse = { mode: InputMode; valid_modes: string[] };
type EdgeTerminalStatus = {
  last_poll_at: string | null;
  last_ok_at: string | null;
  last_error: string | null;
  age_seconds: number | null;
  stale: boolean;
  running: boolean;
};
type EdgeTerminalPnl = {
  generated_at: string;
  since_date: string;
  n_trades: number;
  n_settled: number;
  cumulative_pnl: number | null;
  win_rate: number | null;
  error?: string | null;
};

const INPUT_MODE_KEY = ["edge-terminal", "input-mode"] as const;
const STATUS_KEY = ["edge-terminal", "status"] as const;
const PNL_KEY = ["dashboard", "edge-terminal-pnl"] as const;

export function useInputMode() {
  const q = useQuery({
    queryKey: INPUT_MODE_KEY,
    queryFn: () => apiFetch<InputModeResponse>("/api/edge-terminal/input-mode"),
    staleTime: 15_000,
    refetchInterval: 60_000,
  });
  const mode = q.data?.mode;
  return {
    mode,
    isEdgeTerminal: mode === "edge_terminal",
    isLoading: q.isLoading,
  };
}

function fmtUSD(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = Number(n);
  return `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`;
}
function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${(Number(n) * 100).toFixed(1)}%`;
}

export function InputModeToggle({
  showPipelineData,
  onTogglePipelineData,
}: {
  showPipelineData: boolean;
  onTogglePipelineData: (next: boolean) => void;
}) {
  const colors = useColors();
  const qc = useQueryClient();
  const { mode } = useInputMode();
  const m = useMutation({
    mutationFn: (next: InputMode) =>
      apiFetch<{ mode: InputMode }>("/api/edge-terminal/input-mode", {
        method: "POST",
        body: JSON.stringify({ mode: next }),
        headers: { "content-type": "application/json" },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: INPUT_MODE_KEY }),
  });
  const flip = (next: InputMode) => {
    if (next === mode || m.isPending) return;
    m.mutate(next);
  };
  return (
    <Card testID="input-mode-toggle">
      <SectionHeader>INPUT MODE</SectionHeader>
      <View style={styles.row}>
        {(["edge_terminal", "pipeline"] as InputMode[]).map((m2) => {
          const active = m2 === mode;
          return (
            <Pressable
              key={m2}
              testID={`input-mode-${m2.replace("_", "-")}`}
              disabled={m.isPending}
              onPress={() => flip(m2)}
              style={[
                styles.chip,
                {
                  borderColor: colors.border,
                  backgroundColor: active ? colors.primary : "transparent",
                },
              ]}
            >
              <Text
                style={[
                  styles.chipLbl,
                  {
                    color: active
                      ? colors.primaryForeground
                      : colors.mutedForeground,
                  },
                ]}
              >
                {m2 === "edge_terminal" ? "EDGE TERMINAL" : "PIPELINE"}
              </Text>
            </Pressable>
          );
        })}
      </View>
      {mode === "edge_terminal" ? (
        <View style={[styles.row, { justifyContent: "space-between", marginTop: 8 }]}>
          <Text style={{ color: colors.mutedForeground, fontSize: 11 }}>
            Show pipeline data
          </Text>
          <Switch
            testID="show-pipeline-data-toggle"
            value={showPipelineData}
            onValueChange={onTogglePipelineData}
          />
        </View>
      ) : null}
    </Card>
  );
}

export function EdgeTerminalPnlTile() {
  const colors = useColors();
  const pnl = useQuery({
    queryKey: PNL_KEY,
    queryFn: () => apiFetch<EdgeTerminalPnl>("/api/dashboard/edge-terminal-pnl"),
    refetchInterval: 30_000,
  });
  const status = useQuery({
    queryKey: STATUS_KEY,
    queryFn: () =>
      apiFetch<EdgeTerminalStatus>("/api/edge-terminal/status"),
    refetchInterval: 30_000,
  });
  const stale = !!status.data?.stale;
  const value = pnl.data?.cumulative_pnl ?? null;
  const valueColor =
    value === null
      ? colors.foreground
      : value >= 0
        ? "#34d399"
        : "#fb7185";
  return (
    <Card testID="card-edge-terminal-pnl">
      <SectionHeader>EDGE TERMINAL P&L</SectionHeader>
      {stale ? (
        <Text
          testID="edge-terminal-stale-badge"
          style={{
            color: "#fbbf24",
            fontSize: 10,
            marginBottom: 4,
            textTransform: "uppercase",
            letterSpacing: 1,
          }}
        >
          Stale poller
          {typeof status.data?.age_seconds === "number"
            ? ` · ${Math.round(status.data.age_seconds)}s`
            : ""}
        </Text>
      ) : null}
      {pnl.isError || pnl.data?.error ? (
        <Text
          testID="edge-terminal-pnl-error"
          style={{ color: colors.destructive, fontSize: 11 }}
        >
          {pnl.data?.error ?? "Failed to load."}
        </Text>
      ) : (
        <View style={[styles.row, { opacity: stale ? 0.6 : 1 }]}>
          <View style={styles.cell}>
            <Text style={[styles.cellLbl, { color: colors.mutedForeground }]}>
              CUMULATIVE P&L
            </Text>
            <Text
              testID="edge-terminal-pnl-cumulative"
              style={[styles.cellVal, { color: valueColor }]}
            >
              {fmtUSD(value)}
            </Text>
          </View>
          <View style={styles.cell}>
            <Text style={[styles.cellLbl, { color: colors.mutedForeground }]}>
              HIT RATE
            </Text>
            <Text style={[styles.cellVal, { color: colors.foreground }]}>
              {fmtPct(pnl.data?.win_rate)}
            </Text>
            <Text style={{ color: colors.mutedForeground, fontSize: 10 }}>
              {pnl.data?.n_settled ?? 0} settled · {pnl.data?.n_trades ?? 0} placed
            </Text>
          </View>
        </View>
      )}
      <Text style={{ color: colors.mutedForeground, fontSize: 10, marginTop: 6 }}>
        Since {pnl.data?.since_date ?? "2026-05-21"}
      </Text>
    </Card>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", gap: 8, alignItems: "center" },
  chip: {
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderWidth: 1,
    borderRadius: 4,
  },
  chipLbl: { fontSize: 10, letterSpacing: 1 },
  cell: { flex: 1 },
  cellLbl: { fontSize: 10, letterSpacing: 1, marginBottom: 2 },
  cellVal: { fontSize: 22, fontWeight: "700" },
});
