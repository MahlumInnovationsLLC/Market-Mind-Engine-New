import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import React, { useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Linking,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { Card, SectionHeader } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { ApiError, apiFetch } from "@/lib/api";

type TelegramStatus = {
  linked: boolean;
  telegram_user_id?: number | null;
  telegram_username?: string | null;
  bound_at?: string | null;
  last_seen_at?: string | null;
  pending_link_code?: string | null;
  pending_link_expires_at?: string | null;
  bot_username?: string | null;
  bot_running?: boolean;
};

type TelegramLinkResponse = TelegramStatus & {
  code?: string | null;
  expires_at?: string | null;
};

const TELEGRAM_QUERY_KEY = ["me", "telegram"] as const;

function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function fmtRemaining(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const exp = new Date(iso).getTime();
  if (Number.isNaN(exp)) return null;
  const ms = exp - Date.now();
  if (ms <= 0) return "expired";
  const mins = Math.floor(ms / 60_000);
  const secs = Math.floor((ms % 60_000) / 1000);
  if (mins <= 0) return `${secs}s left`;
  return `${mins}m ${secs}s left`;
}

function describeError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    return `${fallback} (HTTP ${err.status})\nrequest_id=${err.requestId}`;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

/**
 * Mobile counterpart to the web Telegram concierge card
 * (`artifacts/web/src/components/dashboard/telegram-concierge.tsx`).
 *
 * Lets a mobile-first operator bind/unbind their Telegram account
 * against the existing `/api/me/telegram` and `/api/me/telegram/link`
 * endpoints — the same ones the web dashboard hits — so the same
 * concierge alerts and `/estop` commands work without a desktop.
 */
export function TelegramConcierge(): React.ReactElement {
  const colors = useColors();
  const queryClient = useQueryClient();
  const [issuedCode, setIssuedCode] = useState<string | null>(null);
  const [issuedExpiresAt, setIssuedExpiresAt] = useState<string | null>(null);

  const statusQuery = useQuery<TelegramStatus>({
    queryKey: TELEGRAM_QUERY_KEY,
    queryFn: () => apiFetch<TelegramStatus>("/api/me/telegram"),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });

  const linkMutation = useMutation<TelegramLinkResponse, unknown, void>({
    mutationFn: () =>
      apiFetch<TelegramLinkResponse>("/api/me/telegram/link", {
        method: "POST",
        body: {},
      }),
    onSuccess: (data) => {
      const code =
        (typeof data.code === "string" && data.code) ||
        data.pending_link_code ||
        null;
      const expires =
        (typeof data.expires_at === "string" && data.expires_at) ||
        data.pending_link_expires_at ||
        null;
      setIssuedCode(code);
      setIssuedExpiresAt(expires);
      void queryClient.invalidateQueries({ queryKey: TELEGRAM_QUERY_KEY });
    },
  });

  const unlinkMutation = useMutation<{ ok: boolean; removed: boolean }, unknown, void>({
    mutationFn: () =>
      apiFetch<{ ok: boolean; removed: boolean }>("/api/me/telegram", {
        method: "DELETE",
      }),
    onSuccess: () => {
      setIssuedCode(null);
      setIssuedExpiresAt(null);
      void queryClient.invalidateQueries({ queryKey: TELEGRAM_QUERY_KEY });
    },
  });

  const data = statusQuery.data;
  const botUsername = data?.bot_username ?? null;
  const botRunning = data?.bot_running !== false;
  const linked = !!data?.linked;
  const pendingCode =
    issuedCode ?? (!linked ? data?.pending_link_code ?? null : null);
  const pendingExpiresAt =
    issuedExpiresAt ??
    (!linked ? data?.pending_link_expires_at ?? null : null);
  const remaining = fmtRemaining(pendingExpiresAt);
  const deepLink =
    pendingCode && botUsername
      ? `https://t.me/${botUsername}?start=${encodeURIComponent(pendingCode)}`
      : null;

  const linkErr = linkMutation.error;
  const unlinkErr = unlinkMutation.error;
  const actionError = linkErr
    ? describeError(linkErr, "Generate code failed")
    : unlinkErr
      ? describeError(unlinkErr, "Unlink failed")
      : null;

  const onUnlinkPress = () => {
    if (unlinkMutation.isPending) return;
    Alert.alert(
      "Unlink Telegram?",
      "You'll stop receiving concierge alerts and lose the ability to /estop from chat until you re-link.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Unlink",
          style: "destructive",
          onPress: () => unlinkMutation.mutate(),
        },
      ],
      { cancelable: true },
    );
  };

  const onOpenDeepLink = () => {
    if (!deepLink) return;
    void Linking.openURL(deepLink).catch(() => {
      Alert.alert(
        "Telegram not installed",
        `Send /start ${pendingCode} to @${botUsername ?? "the concierge bot"} to finish linking.`,
      );
    });
  };

  return (
    <View>
      <SectionHeader>TELEGRAM CONCIERGE</SectionHeader>
      <Card style={styles.card} testID="card-telegram-concierge">
        <View style={styles.header}>
          <Text style={[styles.title, { color: colors.foreground }]}>
            Concierge bot
          </Text>
          {botUsername ? (
            <Text
              style={[
                styles.badge,
                {
                  color: botRunning ? colors.success : colors.destructive,
                  borderColor: botRunning
                    ? colors.success
                    : colors.destructive,
                },
              ]}
              testID="badge-telegram-bot-status"
            >
              @{botUsername} {botRunning ? "ONLINE" : "OFFLINE"}
            </Text>
          ) : null}
        </View>
        <Text style={[styles.muted, { color: colors.mutedForeground }]}>
          Bind your Telegram account to receive trade alerts and issue
          /estop from chat.
        </Text>

        {statusQuery.isLoading && !data ? (
          <ActivityIndicator
            color={colors.primary}
            testID="text-telegram-loading"
          />
        ) : statusQuery.isError ? (
          <Text
            style={[styles.muted, { color: colors.destructive }]}
            testID="text-telegram-error"
          >
            {describeError(statusQuery.error, "Failed to load Telegram status")}
          </Text>
        ) : linked ? (
          <View style={styles.section}>
            <View style={styles.statusRow}>
              <Text
                style={[
                  styles.badge,
                  { color: colors.success, borderColor: colors.success },
                ]}
                testID="badge-telegram-status"
              >
                LINKED
              </Text>
              {data?.telegram_username ? (
                <Text
                  style={[styles.username, { color: colors.foreground }]}
                  testID="text-telegram-username"
                >
                  @{data.telegram_username}
                </Text>
              ) : data?.telegram_user_id != null ? (
                <Text
                  style={[styles.username, { color: colors.foreground }]}
                  testID="text-telegram-user-id"
                >
                  tg id {data.telegram_user_id}
                </Text>
              ) : null}
            </View>
            <View style={styles.metaGrid}>
              <View style={styles.metaCell}>
                <Text style={[styles.label, { color: colors.mutedForeground }]}>
                  BOUND AT
                </Text>
                <Text
                  style={[styles.metaValue, { color: colors.foreground }]}
                  testID="text-telegram-bound-at"
                >
                  {fmtDateTime(data?.bound_at)}
                </Text>
              </View>
              <View style={styles.metaCell}>
                <Text style={[styles.label, { color: colors.mutedForeground }]}>
                  LAST SEEN
                </Text>
                <Text
                  style={[styles.metaValue, { color: colors.foreground }]}
                  testID="text-telegram-last-seen"
                >
                  {fmtDateTime(data?.last_seen_at)}
                </Text>
              </View>
            </View>
            <Pressable
              onPress={onUnlinkPress}
              disabled={unlinkMutation.isPending}
              testID="button-telegram-unlink"
              style={({ pressed }) => [
                styles.btn,
                {
                  borderColor: colors.destructive,
                  backgroundColor: pressed
                    ? colors.destructive
                    : "transparent",
                  opacity: unlinkMutation.isPending ? 0.5 : 1,
                },
              ]}
            >
              <Text style={[styles.btnText, { color: colors.destructive }]}>
                {unlinkMutation.isPending ? "UNLINKING…" : "UNLINK"}
              </Text>
            </Pressable>
          </View>
        ) : (
          <View style={styles.section}>
            <View style={styles.statusRow}>
              <Text
                style={[
                  styles.badge,
                  {
                    color: colors.mutedForeground,
                    borderColor: colors.border,
                  },
                ]}
                testID="badge-telegram-status"
              >
                NOT LINKED
              </Text>
              <Text style={[styles.muted, { color: colors.mutedForeground }]}>
                {botUsername
                  ? `Bot: @${botUsername}`
                  : "Bot username not configured."}
              </Text>
            </View>

            {pendingCode ? (
              <View
                style={[
                  styles.codeBox,
                  { borderColor: colors.primary },
                ]}
                testID="block-telegram-pending-code"
              >
                <Text style={[styles.label, { color: colors.primary }]}>
                  LINK CODE
                </Text>
                <Text
                  selectable
                  style={[styles.code, { color: colors.foreground }]}
                  testID="text-telegram-link-code"
                >
                  {pendingCode}
                </Text>
                {remaining ? (
                  <Text
                    style={[
                      styles.muted,
                      { color: colors.mutedForeground },
                    ]}
                    testID="text-telegram-code-expires"
                  >
                    {remaining}
                  </Text>
                ) : null}
                {deepLink ? (
                  <Pressable
                    onPress={onOpenDeepLink}
                    testID="link-telegram-deep-link"
                    style={({ pressed }) => [
                      styles.btn,
                      {
                        borderColor: colors.primary,
                        backgroundColor: colors.primary,
                        opacity: pressed ? 0.85 : 1,
                      },
                    ]}
                  >
                    <Text
                      style={[
                        styles.btnText,
                        { color: colors.primaryForeground },
                      ]}
                    >
                      OPEN IN TELEGRAM
                    </Text>
                  </Pressable>
                ) : (
                  <Text
                    style={[styles.muted, { color: colors.mutedForeground }]}
                  >
                    {`Send /start ${pendingCode} to the concierge bot to finish linking.`}
                  </Text>
                )}
              </View>
            ) : null}

            <Pressable
              onPress={() => linkMutation.mutate()}
              disabled={linkMutation.isPending}
              testID="button-telegram-generate-code"
              style={({ pressed }) => [
                styles.btn,
                {
                  borderColor: colors.primary,
                  backgroundColor: pressed ? colors.primary : "transparent",
                  opacity: linkMutation.isPending ? 0.5 : 1,
                },
              ]}
            >
              <Text style={[styles.btnText, { color: colors.primary }]}>
                {linkMutation.isPending
                  ? "GENERATING…"
                  : pendingCode
                    ? "REGENERATE CODE"
                    : "GENERATE LINK CODE"}
              </Text>
            </Pressable>
          </View>
        )}

        {actionError ? (
          <Text
            style={[styles.muted, { color: colors.destructive }]}
            testID="text-telegram-action-error"
          >
            {actionError}
          </Text>
        ) : null}
      </Card>
    </View>
  );
}

const styles = StyleSheet.create({
  card: { gap: 12 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
  },
  title: {
    fontSize: 14,
    fontFamily: "Inter_700Bold",
    letterSpacing: 1.5,
    flex: 1,
  },
  muted: { fontSize: 12, fontFamily: "Inter_400Regular", lineHeight: 16 },
  section: { gap: 12 },
  statusRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    flexWrap: "wrap",
  },
  username: { fontSize: 13, fontFamily: "Inter_600SemiBold" },
  metaGrid: {
    flexDirection: "row",
    gap: 12,
    flexWrap: "wrap",
  },
  metaCell: { flexBasis: 140, flexGrow: 1, gap: 2 },
  label: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_500Medium",
  },
  metaValue: { fontSize: 13, fontFamily: "Inter_400Regular" },
  badge: {
    fontSize: 10,
    letterSpacing: 1,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderWidth: 1,
    fontFamily: "Inter_700Bold",
  },
  codeBox: {
    borderWidth: 1,
    padding: 12,
    gap: 8,
  },
  code: {
    fontSize: 22,
    fontFamily: "Inter_700Bold",
    letterSpacing: 4,
  },
  btn: {
    borderWidth: 1,
    paddingVertical: 12,
    paddingHorizontal: 14,
    alignItems: "center",
  },
  btnText: {
    fontSize: 12,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
    textTransform: "uppercase",
  },
});
