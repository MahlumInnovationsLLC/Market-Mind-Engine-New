import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import React from "react";
import { ActivityIndicator, StyleSheet, Switch, Text, View } from "react-native";

import { Card, SectionHeader } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";

type PrefsBody = {
  notify_live_trade: boolean;
  notify_halt: boolean;
  email_trade_confirmation: boolean;
  digest_partials: boolean;
};
type PrefsResponse = {
  prefs: PrefsBody & Record<string, unknown>;
  tier?: string;
  elite_unlocked?: boolean;
  elite_only_toggles?: string[];
};

export function NotificationPrefs(): React.ReactElement {
  const colors = useColors();
  const qc = useQueryClient();

  const prefs = useQuery<PrefsResponse>({
    queryKey: ["notification-prefs"],
    queryFn: () => apiFetch<PrefsResponse>("/api/me/notification-prefs"),
  });

  const mutation = useMutation({
    mutationFn: (patch: Partial<PrefsBody>) =>
      apiFetch<PrefsResponse>("/api/me/notification-prefs", {
        method: "PUT",
        body: patch,
      }),
    onSuccess: (next) => {
      qc.setQueryData(["notification-prefs"], next);
    },
  });

  const data: PrefsBody = {
    notify_live_trade: prefs.data?.prefs?.notify_live_trade ?? true,
    notify_halt: prefs.data?.prefs?.notify_halt ?? true,
    email_trade_confirmation:
      (prefs.data?.prefs?.email_trade_confirmation as boolean | undefined) ?? true,
    digest_partials:
      (prefs.data?.prefs?.digest_partials as boolean | undefined) ?? false,
  };
  const busy = prefs.isLoading || mutation.isPending;

  return (
    <View>
      <SectionHeader>NOTIFICATIONS</SectionHeader>
      <Card>
        <Row
          label="Live trades"
          hint="Push when a live (non-paper) order fills."
          value={data.notify_live_trade}
          disabled={busy}
          onChange={(v) => mutation.mutate({ notify_live_trade: v })}
          colors={colors}
        />
        <View style={[styles.divider, { backgroundColor: colors.border }]} />
        <Row
          label="Trading halts"
          hint="Push when the kill switch is tripped."
          value={data.notify_halt}
          disabled={busy}
          onChange={(v) => mutation.mutate({ notify_halt: v })}
          colors={colors}
        />
        <View style={[styles.divider, { backgroundColor: colors.border }]} />
        <Row
          label="Trade confirmations"
          hint="Email a receipt with market, side, qty, price, and position after each fill."
          value={data.email_trade_confirmation}
          disabled={busy}
          onChange={(v) => mutation.mutate({ email_trade_confirmation: v })}
          colors={colors}
        />
        <View style={[styles.divider, { backgroundColor: colors.border }]} />
        <Row
          label="Consolidate partial fills"
          hint="One email per completed order instead of one per partial fill."
          value={data.digest_partials}
          disabled={busy}
          onChange={(v) => mutation.mutate({ digest_partials: v })}
          colors={colors}
        />
        {prefs.isLoading ? (
          <ActivityIndicator color={colors.primary} style={{ marginTop: 8 }} />
        ) : null}
      </Card>
    </View>
  );
}

function Row({
  label,
  hint,
  value,
  disabled,
  onChange,
  colors,
}: {
  label: string;
  hint: string;
  value: boolean;
  disabled: boolean;
  onChange: (v: boolean) => void;
  colors: ReturnType<typeof useColors>;
}): React.ReactElement {
  return (
    <View style={styles.row}>
      <View style={{ flex: 1, paddingRight: 12 }}>
        <Text style={[styles.label, { color: colors.foreground }]}>{label}</Text>
        <Text style={[styles.hint, { color: colors.mutedForeground }]}>{hint}</Text>
      </View>
      <Switch
        value={value}
        disabled={disabled}
        onValueChange={onChange}
        trackColor={{ true: colors.primary, false: colors.border }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", alignItems: "center", paddingVertical: 6 },
  label: { fontSize: 14, fontFamily: "Inter_600SemiBold" },
  hint: { fontSize: 11, fontFamily: "Inter_400Regular", marginTop: 2 },
  divider: { height: StyleSheet.hairlineWidth, marginVertical: 4 },
});
