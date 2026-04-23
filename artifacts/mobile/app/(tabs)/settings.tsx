import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Redirect } from "expo-router";
import React, { useState } from "react";
import {
  ActivityIndicator,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Card, SectionHeader } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { ApiError, apiFetch } from "@/lib/api";

type AccessInfo = {
  approved: boolean;
  is_admin: boolean;
  email?: string | null;
  user_id: string;
};

type LiveExecuteState = {
  enabled: boolean;
  effective_mode: string;
  env_mode: string;
  db_mode: string | null;
  source: string;
  caller_tier: string;
  can_toggle: boolean;
};

type EmergencyStopResult = {
  ok: boolean;
  canceled_orders: number;
  trading_mode: string;
  reason: string;
};

export default function SettingsScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const queryClient = useQueryClient();

  const access = useQuery({
    queryKey: ["me", "access"],
    queryFn: () => apiFetch<AccessInfo>("/api/me/access"),
  });

  const liveExec = useQuery({
    queryKey: ["orchestrator", "live-execute"],
    queryFn: () =>
      apiFetch<LiveExecuteState>("/api/orchestrator/live-execute"),
    refetchInterval: 30_000,
    enabled: access.data?.is_admin === true,
  });

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) =>
      apiFetch<LiveExecuteState>("/api/orchestrator/live-execute", {
        method: "POST",
        body: { enabled },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["orchestrator", "live-execute"],
      });
    },
  });

  const stopMutation = useMutation({
    mutationFn: (reason: string) =>
      apiFetch<EmergencyStopResult>("/api/admin/emergency-stop", {
        method: "POST",
        body: { reason: reason.trim() || undefined },
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["orchestrator", "live-execute"],
      });
    },
  });

  const [pendingArm, setPendingArm] = useState(false);
  const [confirmingStop, setConfirmingStop] = useState(false);
  const [reason, setReason] = useState("");
  const [stopResult, setStopResult] = useState<EmergencyStopResult | null>(null);

  if (access.isLoading) {
    return (
      <View style={[styles.center, { backgroundColor: colors.background }]}>
        <ActivityIndicator color={colors.primary} />
      </View>
    );
  }

  if (access.isError || !access.data) {
    return (
      <View
        style={[styles.center, { backgroundColor: colors.background, padding: 24 }]}
      >
        <Text style={[styles.muted, { color: colors.destructive }]}>
          Could not load access info
        </Text>
      </View>
    );
  }

  // Admin-only tab. Non-admins get bounced back to the dashboard.
  if (!access.data.is_admin) {
    return <Redirect href="/(tabs)" />;
  }

  const live = liveExec.data;
  const enabled = !!live?.enabled;
  const mode = (live?.effective_mode ?? "—").toUpperCase();
  const halted = (live?.effective_mode ?? "").toLowerCase() === "halt";
  const toggleBusy = toggleMutation.isPending;
  const stopBusy = stopMutation.isPending;
  const toggleErr = toggleMutation.error as ApiError | undefined;
  const stopErr = stopMutation.error as ApiError | undefined;

  function onArmToggle() {
    if (toggleBusy) return;
    if (!enabled) {
      if (!pendingArm) {
        setPendingArm(true);
        return;
      }
    }
    setPendingArm(false);
    toggleMutation.mutate(!enabled);
  }

  function onConfirmStop() {
    if (stopBusy) return;
    stopMutation.mutate(reason, {
      onSuccess: (data) => {
        setStopResult(data);
        setConfirmingStop(false);
        setReason("");
      },
    });
  }

  return (
    <ScrollView
      style={{ backgroundColor: colors.background }}
      contentContainerStyle={[
        styles.container,
        { paddingBottom: insets.bottom + 100 },
      ]}
      refreshControl={
        <RefreshControl
          refreshing={liveExec.isFetching && !liveExec.isLoading}
          onRefresh={() => liveExec.refetch()}
          tintColor={colors.primary}
        />
      }
      testID="settings-scroll"
    >
      <SectionHeader>TRADING MODE</SectionHeader>
      <Card style={styles.card}>
        <View style={styles.rowHead}>
          <Text style={[styles.title, { color: colors.foreground }]}>
            LIVE_EXECUTE
          </Text>
          <Text
            style={[
              styles.badge,
              {
                color: enabled
                  ? colors.destructive
                  : halted
                    ? colors.destructive
                    : colors.mutedForeground,
                borderColor: enabled
                  ? colors.destructive
                  : halted
                    ? colors.destructive
                    : colors.border,
              },
            ]}
            testID="live-execute-state"
          >
            {mode}
          </Text>
        </View>
        <Text style={[styles.muted, { color: colors.mutedForeground }]}>
          Flips risk.trading_mode between paper and live. Same setting the
          web operator console writes.
        </Text>
        {liveExec.isLoading ? (
          <ActivityIndicator color={colors.primary} />
        ) : liveExec.isError || !live ? (
          <Text style={[styles.muted, { color: colors.destructive }]}>
            {`Could not load trading mode (HTTP ${
              (liveExec.error as ApiError | undefined)?.status ?? "?"
            })`}
            {(liveExec.error as ApiError | undefined)?.requestId
              ? `\nrequest_id=${(liveExec.error as ApiError).requestId}`
              : ""}
          </Text>
        ) : !live?.can_toggle ? (
          <Text style={[styles.muted, { color: colors.mutedForeground }]}>
            Toggle requires Elite tier (current: {live?.caller_tier ?? "?"}).
          </Text>
        ) : (
          <>
            <TouchableOpacity
              onPress={onArmToggle}
              disabled={toggleBusy}
              style={[
                styles.btn,
                {
                  borderColor: enabled
                    ? colors.mutedForeground
                    : pendingArm
                      ? colors.destructive
                      : colors.primary,
                  opacity: toggleBusy ? 0.5 : 1,
                },
              ]}
              testID="button-live-execute-toggle"
            >
              <Text
                style={[
                  styles.btnText,
                  {
                    color: enabled
                      ? colors.mutedForeground
                      : pendingArm
                        ? colors.destructive
                        : colors.primary,
                  },
                ]}
              >
                {toggleBusy
                  ? "…"
                  : enabled
                    ? "Switch to PAPER"
                    : pendingArm
                      ? "Confirm: ARM LIVE"
                      : "Arm LIVE execution"}
              </Text>
            </TouchableOpacity>
            {pendingArm && !enabled ? (
              <TouchableOpacity
                onPress={() => setPendingArm(false)}
                style={styles.cancelBtn}
              >
                <Text style={[styles.cancelText, { color: colors.mutedForeground }]}>
                  cancel
                </Text>
              </TouchableOpacity>
            ) : null}
          </>
        )}
        {toggleErr ? (
          <Text style={[styles.muted, { color: colors.destructive }]}>
            {toggleErr.status === 403
              ? "Forbidden — Elite tier required"
              : `Toggle failed (HTTP ${toggleErr.status ?? "?"})`}
            {toggleErr.requestId
              ? `\nrequest_id=${toggleErr.requestId}`
              : ""}
          </Text>
        ) : null}
      </Card>

      <View style={{ height: 16 }} />
      <SectionHeader>EMERGENCY STOP</SectionHeader>
      <Card
        style={[
          styles.card,
          { borderColor: colors.destructive },
        ]}
      >
        <View style={styles.rowHead}>
          <Text style={[styles.title, { color: colors.destructive }]}>
            KILL ALL
          </Text>
          <Text
            style={[
              styles.badge,
              { color: colors.destructive, borderColor: colors.destructive },
            ]}
          >
            ADMIN
          </Text>
        </View>
        <Text style={[styles.muted, { color: colors.mutedForeground }]}>
          Cancels every resting Kalshi order, halts the trading loop, and
          emails the on-call operator. Persists across restarts until an
          admin re-arms LIVE.
        </Text>

        {!confirmingStop ? (
          <TouchableOpacity
            onPress={() => {
              setStopResult(null);
              setConfirmingStop(true);
            }}
            disabled={stopBusy}
            style={[
              styles.btn,
              { borderColor: colors.destructive, opacity: stopBusy ? 0.5 : 1 },
            ]}
            testID="button-emergency-stop-arm"
          >
            <Text style={[styles.btnText, { color: colors.destructive }]}>
              Emergency Stop
            </Text>
          </TouchableOpacity>
        ) : (
          <View style={[styles.confirmBox, { borderColor: colors.destructive }]}>
            <Text
              style={[
                styles.label,
                { color: colors.mutedForeground },
              ]}
            >
              REASON (OPTIONAL)
            </Text>
            <TextInput
              value={reason}
              onChangeText={setReason}
              placeholder="e.g. settlement feed lag"
              placeholderTextColor={colors.mutedForeground}
              maxLength={200}
              editable={!stopBusy}
              style={[
                styles.input,
                {
                  color: colors.foreground,
                  borderColor: colors.border,
                  backgroundColor: colors.background,
                },
              ]}
              testID="input-emergency-stop-reason"
            />
            <View style={styles.confirmRow}>
              <TouchableOpacity
                onPress={onConfirmStop}
                disabled={stopBusy}
                style={[
                  styles.btn,
                  {
                    flex: 1,
                    borderColor: colors.destructive,
                    backgroundColor: colors.destructive,
                    opacity: stopBusy ? 0.5 : 1,
                  },
                ]}
                testID="button-emergency-stop-confirm"
              >
                <Text style={[styles.btnText, { color: colors.background }]}>
                  {stopBusy ? "…" : "CONFIRM: KILL ALL"}
                </Text>
              </TouchableOpacity>
              <TouchableOpacity
                onPress={() => setConfirmingStop(false)}
                disabled={stopBusy}
                style={styles.cancelBtn}
              >
                <Text
                  style={[styles.cancelText, { color: colors.mutedForeground }]}
                >
                  cancel
                </Text>
              </TouchableOpacity>
            </View>
          </View>
        )}

        {stopErr ? (
          <Text style={[styles.muted, { color: colors.destructive }]}>
            {`Emergency stop failed (HTTP ${stopErr.status ?? "?"})`}
            {stopErr.requestId ? `\nrequest_id=${stopErr.requestId}` : ""}
          </Text>
        ) : null}
        {stopResult ? (
          <Text
            style={[styles.muted, { color: colors.mutedForeground }]}
            testID="text-emergency-stop-result"
          >
            Halted. {stopResult.canceled_orders} order
            {stopResult.canceled_orders === 1 ? "" : "s"} canceled.
          </Text>
        ) : null}
      </Card>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { padding: 16, gap: 8 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
  card: { gap: 12 },
  rowHead: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  title: {
    fontSize: 13,
    fontFamily: "Inter_700Bold",
    letterSpacing: 1.5,
    flex: 1,
    marginRight: 8,
  },
  muted: { fontSize: 12, fontFamily: "Inter_400Regular", lineHeight: 16 },
  btn: {
    borderWidth: 1,
    paddingVertical: 10,
    paddingHorizontal: 14,
    alignItems: "center",
  },
  btnText: {
    fontSize: 12,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
    textTransform: "uppercase",
  },
  cancelBtn: { paddingVertical: 6, alignItems: "center" },
  cancelText: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_500Medium",
    textTransform: "uppercase",
  },
  badge: {
    fontSize: 10,
    letterSpacing: 1,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderWidth: 1,
    fontFamily: "Inter_700Bold",
  },
  confirmBox: {
    borderWidth: 1,
    padding: 12,
    gap: 8,
  },
  label: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_500Medium",
  },
  input: {
    borderWidth: 1,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontFamily: "Inter_400Regular",
    fontSize: 13,
  },
  confirmRow: { flexDirection: "row", alignItems: "center", gap: 8 },
});
