import AsyncStorage from "@react-native-async-storage/async-storage";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import React from "react";
import {
  ActivityIndicator,
  AppState,
  Pressable,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from "react-native";

import { Card, SectionHeader } from "@/components/Card";
import { TimezonePicker } from "@/components/TimezonePicker";
import { useColors } from "@/hooks/useColors";
import { ApiError, apiFetch } from "@/lib/api";
import { findTimezone, offsetForZone } from "@/lib/timezones";

const TZ_BANNER_DISMISS_KEY = "axiom.notifications.tzBannerDismissedPair.v1";

type DismissedTzPair = { device: string; quiet: string };

function parseDismissedPair(raw: string | null): DismissedTzPair | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<DismissedTzPair> | null;
    if (
      parsed &&
      typeof parsed.device === "string" &&
      typeof parsed.quiet === "string"
    ) {
      return { device: parsed.device, quiet: parsed.quiet };
    }
  } catch {
    /* ignore malformed payload — treat as not dismissed */
  }
  return null;
}

type PrefsBody = {
  notify_live_trade: boolean;
  notify_halt: boolean;
  notify_settlement: boolean;
  email_trade_confirmation: boolean;
  digest_partials: boolean;
  sms_live_trade: boolean;
};
type QuietHours = {
  quiet_hours_start_hour: number | null;
  quiet_hours_end_hour: number | null;
  quiet_hours_tz: string | null;
};
type SmsPhone = {
  sms_phone_number: string | null;
  sms_phone_verified: boolean;
  sms_phone_verification_expires_at: string | null;
};
type PrefsResponse = {
  prefs: PrefsBody & QuietHours & SmsPhone & Record<string, unknown>;
  tier?: string;
  elite_unlocked?: boolean;
  elite_only_toggles?: string[];
};

function errMsg(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { detail?: { error?: string } | string } | null;
    if (body && typeof body === "object" && body.detail) {
      const d = body.detail;
      if (typeof d === "string") return d;
      if (d && typeof d === "object" && typeof d.error === "string") return d.error;
    }
    return e.message;
  }
  if (e instanceof Error) return e.message;
  return "Unknown error";
}

type QuietHoursPatch = {
  quiet_hours_start_hour?: number | null;
  quiet_hours_start_hour_present?: boolean;
  quiet_hours_end_hour?: number | null;
  quiet_hours_end_hour_present?: boolean;
  quiet_hours_tz?: string | null;
  quiet_hours_tz_present?: boolean;
};

type SmsPhonePatch = {
  sms_phone_number?: string | null;
  sms_phone_number_present?: boolean;
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

/**
 * Build the trailing "(Europe/London, GMT+1)" suffix shown in the
 * Quiet Hours hint line so users picking a non-device zone get a
 * quick visual confirmation of the saved zone's *current* offset.
 * Falls back to "(zone)" if the offset can't be resolved.
 */
function quietHintZoneSuffix(zone: string): string {
  const off = offsetForZone(zone);
  return off ? `(${zone}, ${off})` : `(${zone})`;
}

export function NotificationPrefs(): React.ReactElement {
  const colors = useColors();
  const qc = useQueryClient();
  const [tzPickerOpen, setTzPickerOpen] = React.useState(false);
  const [phone, setPhone] = React.useState<string>("");
  const [phoneDirty, setPhoneDirty] = React.useState(false);
  const [verifyCode, setVerifyCode] = React.useState<string>("");
  const [verifyMsg, setVerifyMsg] = React.useState<string | null>(null);
  const [verifyError, setVerifyError] = React.useState<string | null>(null);
  const [verifyBusy, setVerifyBusy] = React.useState(false);
  // Detect the device's IANA zone, and re-check whenever the app comes
  // back to the foreground — that's when a traveler's clock has just
  // flipped (PST → JST, etc.) and the saved `quiet_hours_tz` is most
  // likely to be stale.
  const [deviceTz, setDeviceTz] = React.useState<string>(() => detectDeviceTz());
  React.useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => {
      if (state === "active") {
        setDeviceTz(detectDeviceTz());
      }
    });
    return () => sub.remove();
  }, []);
  // Banner dismissal is sticky per (deviceTz, quietTz) pair so it does
  // not re-appear on every visit. A different pair (e.g. user moves on
  // to a third zone) re-arms the banner.
  const [dismissedPair, setDismissedPair] =
    React.useState<DismissedTzPair | null>(null);
  const [dismissLoaded, setDismissLoaded] = React.useState(false);
  React.useEffect(() => {
    let cancelled = false;
    AsyncStorage.getItem(TZ_BANNER_DISMISS_KEY)
      .then((raw) => {
        if (cancelled) return;
        setDismissedPair(parseDismissedPair(raw));
        setDismissLoaded(true);
      })
      .catch(() => {
        if (cancelled) return;
        setDismissLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const prefs = useQuery<PrefsResponse>({
    queryKey: ["notification-prefs"],
    queryFn: () => apiFetch<PrefsResponse>("/api/me/notification-prefs"),
  });

  const mutation = useMutation({
    mutationFn: (patch: Partial<PrefsBody> & QuietHoursPatch & SmsPhonePatch) =>
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
    sms_live_trade:
      (prefs.data?.prefs?.sms_live_trade as boolean | undefined) ?? false,
  };
  const sms: SmsPhone = {
    sms_phone_number:
      (prefs.data?.prefs?.sms_phone_number as string | null | undefined) ?? null,
    sms_phone_verified:
      (prefs.data?.prefs?.sms_phone_verified as boolean | undefined) ?? false,
    sms_phone_verification_expires_at:
      (prefs.data?.prefs?.sms_phone_verification_expires_at as
        | string
        | null
        | undefined) ?? null,
  };
  const eliteUnlocked = !!prefs.data?.elite_unlocked;

  // Keep the local phone input synced with the saved value when the
  // user hasn't started editing — but don't clobber an in-progress edit
  // when a background refetch arrives.
  React.useEffect(() => {
    if (!phoneDirty) {
      setPhone(sms.sms_phone_number ?? "");
    }
    // Only depend on the saved phone string; phoneDirty changes are
    // intentional state and shouldn't retrigger this sync.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sms.sms_phone_number]);

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

  const switchToDeviceTz = () => {
    // Opting in clears the dismissed pair — once the saved zone matches
    // the device, the banner has nothing to suggest anyway.
    setDismissedPair(null);
    AsyncStorage.removeItem(TZ_BANNER_DISMISS_KEY).catch(() => {
      /* best-effort; in-memory state still hides the banner */
    });
    mutation.mutate({
      quiet_hours_tz: deviceTz,
      quiet_hours_tz_present: true,
    });
  };

  const dismissTzBanner = () => {
    if (!quiet.quiet_hours_tz) return;
    const pair: DismissedTzPair = {
      device: deviceTz,
      quiet: quiet.quiet_hours_tz,
    };
    setDismissedPair(pair);
    AsyncStorage.setItem(TZ_BANNER_DISMISS_KEY, JSON.stringify(pair)).catch(
      () => {
        /* best-effort; banner stays hidden for this session regardless */
      },
    );
  };

  // Only suggest a switch when:
  //   - quiet hours are configured (otherwise there is nothing pinned),
  //   - the device's IANA zone is well-formed and differs from the saved one,
  //   - the dismissal store has been read (so we don't flash the banner
  //     for a beat before remembering the user already dismissed it),
  //   - and the user hasn't already dismissed this exact pair.
  const showTzBanner =
    dismissLoaded &&
    !!quiet.quiet_hours_tz &&
    !!deviceTz &&
    deviceTz !== quiet.quiet_hours_tz &&
    !(
      dismissedPair &&
      dismissedPair.device === deviceTz &&
      dismissedPair.quiet === quiet.quiet_hours_tz
    );
  const deviceTzOpt = findTimezone(deviceTz);
  const deviceTzCity = deviceTzOpt ? deviceTzOpt.city : deviceTz;
  const savedTzOpt = findTimezone(quiet.quiet_hours_tz);
  const savedTzCity = savedTzOpt
    ? savedTzOpt.city
    : (quiet.quiet_hours_tz ?? "");

  const savePhone = () => {
    const next = phone.trim();
    setVerifyMsg(null);
    setVerifyError(null);
    setVerifyCode("");
    mutation.mutate(
      {
        sms_phone_number_present: true,
        sms_phone_number: next || null,
      },
      {
        onSuccess: () => {
          setPhoneDirty(false);
          if (next) {
            setVerifyMsg(
              "We just texted a 6-digit verification code. Enter it below to confirm this number.",
            );
          }
        },
        onError: (e) => {
          setVerifyError(errMsg(e));
        },
      },
    );
  };

  const resendCode = async () => {
    setVerifyBusy(true);
    setVerifyMsg(null);
    setVerifyError(null);
    try {
      await apiFetch("/api/me/notification-prefs/sms/start-verification", {
        method: "POST",
        body: {},
      });
      setVerifyMsg("New code sent. Check your phone.");
    } catch (e) {
      setVerifyError(errMsg(e));
    } finally {
      setVerifyBusy(false);
    }
  };

  const submitCode = async () => {
    const code = verifyCode.trim();
    if (!/^\d{6}$/.test(code)) {
      setVerifyError("Enter the 6-digit code we texted you.");
      return;
    }
    setVerifyBusy(true);
    setVerifyMsg(null);
    setVerifyError(null);
    try {
      const next = await apiFetch<PrefsResponse>(
        "/api/me/notification-prefs/sms/verify",
        { method: "POST", body: { code } },
      );
      qc.setQueryData(["notification-prefs"], next);
      setVerifyCode("");
      setVerifyMsg("Phone number verified. SMS alerts are unlocked.");
    } catch (e) {
      setVerifyError(errMsg(e));
    } finally {
      setVerifyBusy(false);
    }
  };

  const phoneSavedNonEmpty = !!sms.sms_phone_number;
  const needsVerification = phoneSavedNonEmpty && !sms.sms_phone_verified;
  const smsToggleDisabled =
    busy || !eliteUnlocked || !sms.sms_phone_verified;

  return (
    <View>
      <SectionHeader>NOTIFICATIONS</SectionHeader>
      <Card>
        {showTzBanner ? (
          <View
            testID="banner-device-tz-mismatch"
            style={[
              styles.tzBanner,
              {
                borderColor: colors.primary,
                backgroundColor: colors.background,
              },
            ]}
          >
            <Text style={[styles.label, { color: colors.foreground }]}>
              Switch quiet hours to {deviceTzCity}?
            </Text>
            <Text
              testID="text-tz-banner-detail"
              style={[styles.hint, { color: colors.mutedForeground }]}
            >
              Your device is on {deviceTz}, but quiet hours are pinned to{" "}
              {savedTzCity} ({quiet.quiet_hours_tz}).
            </Text>
            <View style={styles.tzBannerActions}>
              <Pressable
                testID="button-tz-banner-switch"
                onPress={switchToDeviceTz}
                disabled={busy}
                accessibilityRole="button"
                accessibilityLabel={`Switch quiet hours to ${deviceTz}`}
                style={({ pressed }) => [
                  styles.primaryButton,
                  {
                    backgroundColor: colors.primary,
                    opacity: busy ? 0.5 : pressed ? 0.7 : 1,
                  },
                ]}
              >
                <Text
                  style={[
                    styles.primaryButtonText,
                    { color: colors.primaryForeground },
                  ]}
                >
                  Switch
                </Text>
              </Pressable>
              <Pressable
                testID="button-tz-banner-dismiss"
                onPress={dismissTzBanner}
                disabled={busy}
                accessibilityRole="button"
                accessibilityLabel="Dismiss timezone suggestion"
                style={({ pressed }) => [
                  styles.secondaryButton,
                  {
                    borderColor: colors.border,
                    opacity: busy ? 0.5 : pressed ? 0.7 : 1,
                  },
                ]}
              >
                <Text
                  style={[
                    styles.secondaryButtonText,
                    { color: colors.foreground },
                  ]}
                >
                  Dismiss
                </Text>
              </Pressable>
            </View>
          </View>
        ) : null}
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
              ? `Mute pushes ${formatHour(quiet.quiet_hours_start_hour!)} – ${formatHour(quiet.quiet_hours_end_hour!)} ${quietHintZoneSuffix(quiet.quiet_hours_tz!)}. Email alerts are unaffected.`
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

      <SectionHeader>SMS ALERTS</SectionHeader>
      <Card>
        <Row
          label="Live trades — SMS"
          hint={
            !eliteUnlocked
              ? "Elite tier required to enable SMS alerts."
              : !sms.sms_phone_verified
                ? "Verify your phone number below to enable SMS alerts."
                : "Text-message alert for every live (non-paper) fill."
          }
          value={data.sms_live_trade}
          disabled={smsToggleDisabled}
          onChange={(v) => mutation.mutate({ sms_live_trade: v })}
          colors={colors}
          testID="row-sms-live-trade"
        />
        <View style={[styles.divider, { backgroundColor: colors.border }]} />
        <View style={styles.smsBlock}>
          <View style={styles.smsHeaderRow}>
            <Text style={[styles.label, { color: colors.foreground }]}>
              Phone number
            </Text>
            {phoneSavedNonEmpty ? (
              <View
                testID="badge-phone-verified-status"
                style={[
                  styles.badge,
                  {
                    borderColor: sms.sms_phone_verified
                      ? colors.primary
                      : colors.destructive,
                  },
                ]}
              >
                <Text
                  style={[
                    styles.badgeText,
                    {
                      color: sms.sms_phone_verified
                        ? colors.primary
                        : colors.destructive,
                    },
                  ]}
                >
                  {sms.sms_phone_verified ? "Verified" : "Unverified"}
                </Text>
              </View>
            ) : null}
          </View>
          <Text style={[styles.hint, { color: colors.mutedForeground }]}>
            E.164 format, e.g. +15551234567
          </Text>
          <View style={styles.phoneRow}>
            <TextInput
              testID="input-sms-phone"
              value={phone}
              onChangeText={(v) => {
                setPhone(v);
                setPhoneDirty(true);
              }}
              placeholder="+15551234567"
              placeholderTextColor={colors.mutedForeground}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="phone-pad"
              editable={!busy && eliteUnlocked}
              style={[
                styles.input,
                {
                  borderColor: colors.border,
                  color: colors.foreground,
                  backgroundColor: colors.background,
                  opacity: !eliteUnlocked ? 0.5 : 1,
                },
              ]}
            />
            <Pressable
              testID="button-save-phone"
              onPress={savePhone}
              disabled={busy || !eliteUnlocked}
              accessibilityRole="button"
              accessibilityLabel="Save phone number"
              style={({ pressed }) => [
                styles.primaryButton,
                {
                  backgroundColor: colors.primary,
                  opacity: busy || !eliteUnlocked ? 0.5 : pressed ? 0.7 : 1,
                },
              ]}
            >
              <Text
                style={[
                  styles.primaryButtonText,
                  { color: colors.primaryForeground },
                ]}
              >
                Save
              </Text>
            </Pressable>
          </View>

          {needsVerification ? (
            <View
              testID="section-phone-verify"
              style={[
                styles.verifyBox,
                { borderColor: colors.border },
              ]}
            >
              <Text style={[styles.label, { color: colors.foreground }]}>
                Confirm ownership
              </Text>
              <Text style={[styles.hint, { color: colors.mutedForeground }]}>
                We sent a 6-digit code to {sms.sms_phone_number}. Enter it
                below to enable paid SMS alerts.
              </Text>
              <View style={styles.phoneRow}>
                <TextInput
                  testID="input-sms-code"
                  value={verifyCode}
                  onChangeText={(v) =>
                    setVerifyCode(v.replace(/\D/g, "").slice(0, 6))
                  }
                  placeholder="123456"
                  placeholderTextColor={colors.mutedForeground}
                  keyboardType="number-pad"
                  maxLength={6}
                  editable={!verifyBusy && !busy}
                  textContentType="oneTimeCode"
                  autoComplete="sms-otp"
                  style={[
                    styles.input,
                    styles.codeInput,
                    {
                      borderColor: colors.border,
                      color: colors.foreground,
                      backgroundColor: colors.background,
                    },
                  ]}
                />
                <Pressable
                  testID="button-verify-code"
                  onPress={submitCode}
                  disabled={verifyBusy || busy || verifyCode.length !== 6}
                  accessibilityRole="button"
                  accessibilityLabel="Verify code"
                  style={({ pressed }) => [
                    styles.primaryButton,
                    {
                      backgroundColor: colors.primary,
                      opacity:
                        verifyBusy || busy || verifyCode.length !== 6
                          ? 0.5
                          : pressed
                            ? 0.7
                            : 1,
                    },
                  ]}
                >
                  <Text
                    style={[
                      styles.primaryButtonText,
                      { color: colors.primaryForeground },
                    ]}
                  >
                    Verify
                  </Text>
                </Pressable>
                <Pressable
                  testID="button-resend-code"
                  onPress={resendCode}
                  disabled={verifyBusy || busy}
                  accessibilityRole="button"
                  accessibilityLabel="Resend verification code"
                  style={({ pressed }) => [
                    styles.secondaryButton,
                    {
                      borderColor: colors.border,
                      opacity:
                        verifyBusy || busy ? 0.5 : pressed ? 0.7 : 1,
                    },
                  ]}
                >
                  <Text
                    style={[
                      styles.secondaryButtonText,
                      { color: colors.foreground },
                    ]}
                  >
                    Resend
                  </Text>
                </Pressable>
              </View>
            </View>
          ) : null}

          {verifyMsg ? (
            <Text
              testID="text-verify-msg"
              style={[styles.hint, { color: colors.primary }]}
            >
              {verifyMsg}
            </Text>
          ) : null}
          {verifyError ? (
            <Text
              testID="text-verify-error"
              style={[styles.hint, { color: colors.destructive }]}
            >
              {verifyError}
            </Text>
          ) : null}
        </View>
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
  testID,
}: {
  label: string;
  hint: string;
  value: boolean;
  disabled: boolean;
  onChange: (v: boolean) => void;
  colors: ReturnType<typeof useColors>;
  testID?: string;
}): React.ReactElement {
  return (
    <View style={styles.row} testID={testID}>
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
  smsBlock: { paddingTop: 8, paddingBottom: 4, gap: 8 },
  smsHeaderRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  badge: {
    borderWidth: 1,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
  },
  badgeText: {
    fontSize: 10,
    letterSpacing: 1.2,
    fontFamily: "Inter_600SemiBold",
    textTransform: "uppercase",
  },
  phoneRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  input: {
    flex: 1,
    borderWidth: 1,
    borderRadius: 6,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 13,
    fontFamily: "Inter_500Medium",
  },
  codeInput: {
    textAlign: "center",
    letterSpacing: 4,
  },
  primaryButton: {
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 6,
    alignItems: "center",
    justifyContent: "center",
  },
  primaryButtonText: {
    fontSize: 12,
    fontFamily: "Inter_700Bold",
    textTransform: "uppercase",
    letterSpacing: 1,
  },
  secondaryButton: {
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 6,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  secondaryButtonText: {
    fontSize: 12,
    fontFamily: "Inter_600SemiBold",
    textTransform: "uppercase",
    letterSpacing: 1,
  },
  verifyBox: {
    borderWidth: 1,
    borderRadius: 6,
    padding: 10,
    gap: 8,
    marginTop: 4,
  },
  tzBanner: {
    borderWidth: 1,
    borderRadius: 6,
    padding: 10,
    gap: 8,
    marginBottom: 8,
  },
  tzBannerActions: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
});
