import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import React from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Switch,
  Text,
  View,
} from "react-native";

import { Card, SectionHeader } from "@/components/Card";
import { TimezonePicker } from "@/components/TimezonePicker";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";
import { findTimezone, offsetForZone } from "@/lib/timezones";

type PrefsBody = {
  notify_live_trade: boolean;
  notify_halt: boolean;
  notify_settlement: boolean;
  email_trade_confirmation: boolean;
  digest_partials: boolean;
};
type QuietHours = {
  quiet_hours_start_hour: number | null;
  quiet_hours_end_hour: number | null;
  quiet_hours_tz: string | null;
};
type PrefsResponse = {
  prefs: PrefsBody & QuietHours & Record<string, unknown>;
  tier?: string;
  elite_unlocked?: boolean;
  elite_only_toggles?: string[];
};

type QuietHoursPatch = {
  quiet_hours_start_hour?: number | null;
  quiet_hours_start_hour_present?: boolean;
  quiet_hours_end_hour?: number | null;
  quiet_hours_end_hour_present?: boolean;
  quiet_hours_tz?: string | null;
  quiet_hours_tz_present?: boolean;
};

function detectDeviceTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function formatHour(h: number): string {
  const hh = ((h % 24) + 24) % 24;
  const suffix = hh < 12 ? "AM" : "PM";
  const display = hh === 0 ? 12 : hh > 12 ? hh - 12 : hh;
  return `${display} ${suffix}`;
}

function tzDisplayLine(zone: string | null): string {
  if (!zone) return "";
  const opt = findTimezone(zone);
  const off = offsetForZone(zone);
  const city = opt ? opt.city : zone;
  return off ? `${city} (${off}) · ${zone}` : `${city} · ${zone}`;
}

export function NotificationPrefs(): React.ReactElement {
  const colors = useColors();
  const qc = useQueryClient();
  const [tzPickerOpen, setTzPickerOpen] = React.useState(false);

  const prefs = useQuery<PrefsResponse>({
    queryKey: ["notification-prefs"],
    queryFn: () => apiFetch<PrefsResponse>("/api/me/notification-prefs"),
  });

  const mutation = useMutation({
    mutationFn: (patch: Partial<PrefsBody> & QuietHoursPatch) =>
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
    notify_settlement:
      (prefs.data?.prefs?.notify_settlement as boolean | undefined) ?? true,
    email_trade_confirmation:
      (prefs.data?.prefs?.email_trade_confirmation as boolean | undefined) ?? true,
    digest_partials:
      (prefs.data?.prefs?.digest_partials as boolean | undefined) ?? false,
  };
  const quiet: QuietHours = {
    quiet_hours_start_hour:
      (prefs.data?.prefs?.quiet_hours_start_hour as number | null | undefined) ?? null,
    quiet_hours_end_hour:
      (prefs.data?.prefs?.quiet_hours_end_hour as number | null | undefined) ?? null,
    quiet_hours_tz:
      (prefs.data?.prefs?.quiet_hours_tz as string | null | undefined) ?? null,
  };
  const quietEnabled =
    quiet.quiet_hours_start_hour !== null &&
    quiet.quiet_hours_end_hour !== null &&
    !!quiet.quiet_hours_tz &&
    quiet.quiet_hours_start_hour !== quiet.quiet_hours_end_hour;
  const busy = prefs.isLoading || mutation.isPending;

  const enableQuietHours = (on: boolean) => {
    if (on) {
      // Default to 10pm -> 7am in the device's timezone. If the saved
      // hours collapse to a zero-length range (start == end) — which
      // would re-enter the disabled state — fall back to the defaults
      // so users can always recover.
      const savedStart = quiet.quiet_hours_start_hour;
      const savedEnd = quiet.quiet_hours_end_hour;
      const usableSaved =
        savedStart !== null && savedEnd !== null && savedStart !== savedEnd;
      mutation.mutate({
        quiet_hours_start_hour: usableSaved ? savedStart : 22,
        quiet_hours_start_hour_present: true,
        quiet_hours_end_hour: usableSaved ? savedEnd : 7,
        quiet_hours_end_hour_present: true,
        quiet_hours_tz: quiet.quiet_hours_tz ?? detectDeviceTz(),
        quiet_hours_tz_present: true,
      });
    } else {
      // Disable by clearing the timezone — keeps the saved start/end
      // values around so re-enabling restores them.
      mutation.mutate({
        quiet_hours_tz: null,
        quiet_hours_tz_present: true,
      });
    }
  };

  const bumpHour = (which: "start" | "end", delta: number) => {
    const cur = which === "start"
      ? (quiet.quiet_hours_start_hour ?? 22)
      : (quiet.quiet_hours_end_hour ?? 7);
    const next = ((cur + delta) % 24 + 24) % 24;
    if (which === "start") {
      mutation.mutate({
        quiet_hours_start_hour: next,
        quiet_hours_start_hour_present: true,
      });
    } else {
      mutation.mutate({
        quiet_hours_end_hour: next,
        quiet_hours_end_hour_present: true,
      });
    }
  };

  const useDeviceTz = () => {
    mutation.mutate({
      quiet_hours_tz: detectDeviceTz(),
      quiet_hours_tz_present: true,
    });
  };

  const pickTz = (id: string) => {
    setTzPickerOpen(false);
    mutation.mutate({
      quiet_hours_tz: id,
      quiet_hours_tz_present: true,
    });
  };

  const pickDeviceTz = () => {
    setTzPickerOpen(false);
    useDeviceTz();
  };

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
          label="Position settlements"
          hint="Push with realized P&L when a position resolves."
          value={data.notify_settlement}
          disabled={busy}
          onChange={(v) => mutation.mutate({ notify_settlement: v })}
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
        <View style={[styles.divider, { backgroundColor: colors.border }]} />
        <Row
          label="Quiet hours"
          hint={
            quietEnabled
              ? `Mute pushes ${formatHour(quiet.quiet_hours_start_hour!)} – ${formatHour(quiet.quiet_hours_end_hour!)} (${quiet.quiet_hours_tz}). Email alerts are unaffected.`
              : "Mute live-trade and halt pushes during a daily window. Email alerts are unaffected."
          }
          value={quietEnabled}
          disabled={busy}
          onChange={enableQuietHours}
          colors={colors}
        />
        {quietEnabled ? (
          <View style={styles.quietBlock}>
            <HourPicker
              label="Start"
              hour={quiet.quiet_hours_start_hour!}
              disabled={busy}
              onUp={() => bumpHour("start", 1)}
              onDown={() => bumpHour("start", -1)}
              colors={colors}
            />
            <HourPicker
              label="End"
              hour={quiet.quiet_hours_end_hour!}
              disabled={busy}
              onUp={() => bumpHour("end", 1)}
              onDown={() => bumpHour("end", -1)}
              colors={colors}
            />
            <Pressable
              disabled={busy}
              onPress={() => setTzPickerOpen(true)}
              testID="open-tz-picker"
              accessibilityRole="button"
              accessibilityLabel="Change time zone"
              style={({ pressed }) => [
                styles.tzRow,
                {
                  opacity: busy ? 0.5 : pressed ? 0.7 : 1,
                },
              ]}
            >
              <View style={{ flex: 1 }}>
                <Text style={[styles.label, { color: colors.foreground }]}>
                  Time zone
                </Text>
                <Text style={[styles.hint, { color: colors.mutedForeground }]}>
                  {tzDisplayLine(quiet.quiet_hours_tz)}
                </Text>
              </View>
              <Text style={[styles.tzButtonText, { color: colors.primary }]}>
                Change
              </Text>
            </Pressable>
          </View>
        ) : null}
        {prefs.isLoading ? (
          <ActivityIndicator color={colors.primary} style={{ marginTop: 8 }} />
        ) : null}
      </Card>
      <TimezonePicker
        visible={tzPickerOpen}
        selected={quiet.quiet_hours_tz}
        onClose={() => setTzPickerOpen(false)}
        onSelect={pickTz}
        onUseDevice={pickDeviceTz}
      />
    </View>
  );
}

function HourPicker({
  label,
  hour,
  disabled,
  onUp,
  onDown,
  colors,
}: {
  label: string;
  hour: number;
  disabled: boolean;
  onUp: () => void;
  onDown: () => void;
  colors: ReturnType<typeof useColors>;
}): React.ReactElement {
  return (
    <View style={styles.hourRow}>
      <Text style={[styles.label, { color: colors.foreground, flex: 1 }]}>
        {label}
      </Text>
      <Pressable
        disabled={disabled}
        onPress={onDown}
        style={({ pressed }) => [
          styles.stepButton,
          { borderColor: colors.border, opacity: disabled ? 0.4 : pressed ? 0.6 : 1 },
        ]}
      >
        <Text style={[styles.stepText, { color: colors.foreground }]}>−</Text>
      </Pressable>
      <Text style={[styles.hourValue, { color: colors.foreground }]}>
        {formatHour(hour)}
      </Text>
      <Pressable
        disabled={disabled}
        onPress={onUp}
        style={({ pressed }) => [
          styles.stepButton,
          { borderColor: colors.border, opacity: disabled ? 0.4 : pressed ? 0.6 : 1 },
        ]}
      >
        <Text style={[styles.stepText, { color: colors.foreground }]}>+</Text>
      </Pressable>
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
  quietBlock: { paddingTop: 4, paddingBottom: 4, gap: 8 },
  hourRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 4,
    gap: 8,
  },
  stepButton: {
    width: 32,
    height: 32,
    borderRadius: 6,
    borderWidth: StyleSheet.hairlineWidth,
    alignItems: "center",
    justifyContent: "center",
  },
  stepText: { fontSize: 18, fontFamily: "Inter_600SemiBold" },
  hourValue: {
    minWidth: 64,
    textAlign: "center",
    fontSize: 14,
    fontFamily: "Inter_600SemiBold",
  },
  tzRow: { flexDirection: "row", alignItems: "center", paddingVertical: 8 },
  tzButtonText: { fontSize: 13, fontFamily: "Inter_600SemiBold" },
});
