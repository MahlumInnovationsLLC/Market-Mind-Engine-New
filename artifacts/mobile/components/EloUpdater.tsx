/**
 * Elo updater scheduler panel for the mobile operator console.
 *
 * Mirrors the web operator dashboard's `EloUpdaterPanel`
 * (`artifacts/web/src/pages/admin.tsx`) so admins on the move can
 * confirm team ratings are flowing and trigger a catch-up cycle
 * without dropping into a laptop.
 *
 * Endpoints (`/api/admin/elo/status` + `/api/admin/elo/run-cycle`)
 * are admin-only on the API server and already exist.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import React from "react";
import {
  ActivityIndicator,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from "react-native";

import { Card } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { ApiError, apiFetch } from "@/lib/api";

export type EloUpdaterCycle = {
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number;
  games_examined: number;
  games_updated: number;
  games_skipped: number;
  teams_touched: number;
  errors: string[];
};

export type EloUpdaterStatus = {
  running: boolean;
  cycle_seconds: number;
  last_cycle: EloUpdaterCycle | null;
};

function fmtTime(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toISOString().replace("T", " ").slice(0, 19);
  } catch {
    return String(s);
  }
}

function fmtInterval(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = seconds / 60;
  if (m < 60) {
    return Number.isInteger(m) ? `${m}m` : `${m.toFixed(1)}m`;
  }
  const h = m / 60;
  return Number.isInteger(h) ? `${h}h` : `${h.toFixed(1)}h`;
}

function fmtCount(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toLocaleString();
}

function fmtRelative(seconds: number): string {
  const s = Math.floor(Math.abs(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) {
    const rem = s % 60;
    return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
  }
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return remM > 0 ? `${h}h ${remM}m` : `${h}h`;
}

export function computeEloNextRun(
  status: EloUpdaterStatus,
  nowMs: number,
): { label: string; overdue: boolean } | null {
  if (!status.running) return null;
  if (!Number.isFinite(status.cycle_seconds) || status.cycle_seconds <= 0) {
    return null;
  }
  const last = status.last_cycle;
  if (!last || !last.completed_at) return null;
  const completedMs = Date.parse(last.completed_at);
  if (!Number.isFinite(completedMs)) return null;
  const nextMs = completedMs + status.cycle_seconds * 1000;
  const diffSec = Math.round((nextMs - nowMs) / 1000);
  if (diffSec >= 0) {
    return { label: `in ${fmtRelative(diffSec)}`, overdue: false };
  }
  return { label: `overdue ${fmtRelative(diffSec)}`, overdue: true };
}

function useNowTick(intervalMs: number = 1000): number {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function EloUpdaterDisplay({
  status,
  onRunNow,
  running,
  error,
  result,
}: {
  status: EloUpdaterStatus;
  onRunNow: () => void;
  running: boolean;
  error?: string | null;
  result?: EloUpdaterCycle | null;
}) {
  const colors = useColors();
  const last = status.last_cycle ?? null;
  const stateLabel = !status.running
    ? "STOPPED"
    : last && last.errors.length > 0
      ? "ERRORS"
      : last
        ? "OK"
        : "PENDING";
  const stateColor = !status.running
    ? colors.mutedForeground
    : stateLabel === "ERRORS"
      ? colors.destructive
      : stateLabel === "PENDING"
        ? colors.warning
        : colors.primary;
  const now = useNowTick(1000);
  const nextRun = computeEloNextRun(status, now);

  const styles = StyleSheet.create({
    card: { gap: 12 },
    header: {
      flexDirection: "row",
      justifyContent: "space-between",
      alignItems: "flex-start",
      gap: 8,
    },
    desc: {
      flex: 1,
      color: colors.mutedForeground,
      fontSize: 11,
      fontFamily: "Inter_400Regular",
      lineHeight: 16,
    },
    badge: {
      fontSize: 10,
      letterSpacing: 1,
      paddingHorizontal: 6,
      paddingVertical: 2,
      borderWidth: 1,
      fontFamily: "Inter_700Bold",
      color: stateColor,
      borderColor: stateColor,
    },
    grid: { flexDirection: "row", flexWrap: "wrap" },
    cell: { width: "50%", paddingVertical: 4 },
    label: {
      color: colors.mutedForeground,
      fontSize: 9,
      letterSpacing: 1.5,
      textTransform: "uppercase",
      fontFamily: "Inter_500Medium",
    },
    value: {
      color: colors.foreground,
      fontSize: 14,
      fontFamily: "Inter_600SemiBold",
      marginTop: 2,
    },
    valueAccent: { color: colors.primary },
    valueWarning: { color: colors.warning },
    hint: {
      color: colors.mutedForeground,
      fontSize: 10,
      fontFamily: "Inter_400Regular",
      marginTop: 1,
    },
    empty: {
      borderWidth: 1,
      borderColor: colors.border,
      backgroundColor: colors.background,
      padding: 8,
    },
    emptyText: {
      color: colors.mutedForeground,
      fontSize: 11,
      fontFamily: "Inter_400Regular",
      lineHeight: 16,
    },
    errorBox: {
      borderWidth: 1,
      borderColor: colors.destructive,
      padding: 8,
      gap: 4,
    },
    errorTitle: {
      color: colors.destructive,
      fontSize: 10,
      letterSpacing: 1.5,
      textTransform: "uppercase",
      fontFamily: "Inter_700Bold",
    },
    errorItem: {
      color: colors.destructive,
      fontSize: 11,
      fontFamily: "Inter_400Regular",
    },
    actionsRow: {
      flexDirection: "row",
      flexWrap: "wrap",
      alignItems: "center",
      gap: 12,
      borderTopWidth: 1,
      borderTopColor: colors.border,
      paddingTop: 10,
    },
    btn: {
      borderWidth: 1,
      borderColor: colors.border,
      paddingVertical: 8,
      paddingHorizontal: 12,
    },
    btnDisabled: { opacity: 0.5 },
    btnText: {
      color: colors.foreground,
      fontSize: 11,
      letterSpacing: 1.5,
      textTransform: "uppercase",
      fontFamily: "Inter_700Bold",
    },
    runResult: {
      color: colors.primary,
      fontSize: 11,
      fontFamily: "Inter_400Regular",
      flexShrink: 1,
    },
    runError: {
      color: colors.destructive,
      fontSize: 11,
      fontFamily: "Inter_400Regular",
      flexShrink: 1,
    },
  });

  return (
    <Card style={styles.card} testID="panel-elo-updater">
      <View style={styles.header}>
        <Text style={styles.desc}>
          The periodic Elo updater scans the games table for finished
          matchups that haven&apos;t been fed into record_game() yet and
          updates per-league team ratings. Counters below come from the
          most recent completed cycle.
        </Text>
        <Text style={styles.badge} testID="elo-updater-state">
          {stateLabel}
        </Text>
      </View>

      <View style={styles.grid}>
        <View style={styles.cell}>
          <Text style={styles.label}>Cycle interval</Text>
          <Text style={styles.value} testID="elo-updater-interval">
            {fmtInterval(status.cycle_seconds)}
          </Text>
        </View>
        <View style={styles.cell}>
          <Text style={styles.label}>Last run</Text>
          <Text style={styles.value} testID="elo-updater-last-run">
            {last ? fmtTime(last.completed_at ?? last.started_at) : "—"}
          </Text>
          {last ? (
            <Text style={styles.hint}>
              took {last.duration_seconds.toFixed(2)}s
            </Text>
          ) : null}
        </View>
        <View style={styles.cell}>
          <Text style={styles.label}>Next run</Text>
          <Text
            style={[
              styles.value,
              nextRun?.overdue ? styles.valueWarning : null,
            ]}
            testID="elo-updater-next-run"
          >
            {nextRun ? nextRun.label : "—"}
          </Text>
        </View>
        <View style={styles.cell}>
          <Text style={styles.label}>Games examined</Text>
          <Text style={styles.value} testID="elo-updater-examined">
            {last ? fmtCount(last.games_examined) : "—"}
          </Text>
          {last ? (
            <Text style={styles.hint}>
              skipped {fmtCount(last.games_skipped)}
            </Text>
          ) : null}
        </View>
        <View style={styles.cell}>
          <Text style={styles.label}>Games updated</Text>
          <Text
            style={[
              styles.value,
              last && last.games_updated > 0 ? styles.valueAccent : null,
            ]}
            testID="elo-updater-updated"
          >
            {last ? fmtCount(last.games_updated) : "—"}
          </Text>
          {last ? (
            <Text style={styles.hint}>
              {fmtCount(last.teams_touched)} team-rows touched
            </Text>
          ) : null}
        </View>
      </View>

      {!last ? (
        <View style={styles.empty} testID="elo-updater-empty">
          <Text style={styles.emptyText}>
            The scheduler has not completed a cycle yet — counters will
            appear after the first run finishes (default every 5 minutes),
            or use &quot;Run cycle now&quot; below to kick one immediately.
          </Text>
        </View>
      ) : null}

      {last && last.errors.length > 0 ? (
        <View style={styles.errorBox} testID="elo-updater-errors">
          <Text style={styles.errorTitle}>
            Last cycle errors ({last.errors.length})
          </Text>
          {last.errors.slice(0, 5).map((e, i) => (
            <Text
              key={i}
              style={styles.errorItem}
              testID={`elo-updater-error-${i}`}
            >
              • {e}
            </Text>
          ))}
          {last.errors.length > 5 ? (
            <Text style={styles.hint}>
              …and {last.errors.length - 5} more
            </Text>
          ) : null}
        </View>
      ) : null}

      <View style={styles.actionsRow}>
        <TouchableOpacity
          onPress={onRunNow}
          disabled={running}
          style={[styles.btn, running ? styles.btnDisabled : null]}
          testID="button-elo-run-now"
        >
          <Text style={styles.btnText}>
            {running ? "Running…" : "Run cycle now"}
          </Text>
        </TouchableOpacity>
        {error ? (
          <Text style={styles.runError} testID="elo-updater-run-error">
            {error}
          </Text>
        ) : null}
        {!error && result ? (
          <Text style={styles.runResult} testID="elo-updater-run-result">
            Examined {fmtCount(result.games_examined)} · updated{" "}
            {fmtCount(result.games_updated)} · skipped{" "}
            {fmtCount(result.games_skipped)} ·{" "}
            {result.duration_seconds.toFixed(2)}s
            {result.errors.length > 0
              ? ` · ${result.errors.length} error(s)`
              : ""}
          </Text>
        ) : null}
      </View>
    </Card>
  );
}

export default function EloUpdaterPanel() {
  const colors = useColors();
  const queryClient = useQueryClient();
  const queryKey = ["admin", "elo", "status"] as const;

  const { data, isLoading, error } = useQuery<EloUpdaterStatus>({
    queryKey,
    queryFn: () => apiFetch<EloUpdaterStatus>("/api/admin/elo/status"),
    refetchInterval: 30_000,
  });

  const runNow = useMutation({
    mutationFn: () =>
      apiFetch<EloUpdaterCycle>("/api/admin/elo/run-cycle", {
        method: "POST",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
    },
  });

  if (isLoading) {
    return (
      <Card testID="elo-updater-loading">
        <ActivityIndicator color={colors.primary} />
      </Card>
    );
  }
  if (error || !data) {
    const apiErr = error as ApiError | undefined;
    return (
      <Card testID="elo-updater-error">
        <Text
          style={{
            color: colors.destructive,
            fontSize: 12,
            fontFamily: "Inter_400Regular",
          }}
        >
          {`Failed to load Elo updater status (HTTP ${apiErr?.status ?? "?"})`}
          {apiErr?.requestId ? `\nrequest_id=${apiErr.requestId}` : ""}
        </Text>
      </Card>
    );
  }

  return (
    <EloUpdaterDisplay
      status={data}
      onRunNow={() => runNow.mutate()}
      running={runNow.isPending}
      error={
        runNow.isError
          ? ((runNow.error as Error)?.message ?? "Run cycle failed.")
          : null
      }
      result={runNow.isSuccess ? (runNow.data ?? null) : null}
    />
  );
}
