import { useQuery } from "@tanstack/react-query";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import React from "react";
import {
  ActivityIndicator,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Card, SectionHeader } from "@/components/Card";
import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";

const PRICING_URL = "https://axiompredict.com/pricing";

type AnalystVerdict = {
  persona?: string | null;
  provider?: string | null;
  model?: string | null;
  primary_provider?: string | null;
  primary_model?: string | null;
  fallback_used?: boolean | null;
  fallback_reason?: string | null;
  probability?: number | null;
  confidence?: number | null;
  lean?: string | null;
  weight?: number | null;
  reasoning?: string | null;
  latency_ms?: number | null;
  error?: string | null;
};

type ModeratorSynthesis = {
  model?: string | null;
  consensus_probability?: number | null;
  confidence_band_low?: number | null;
  confidence_band_high?: number | null;
  agreement_rate?: number | null;
  recommended_action?: string | null;
  synthesis?: string | null;
  latency_ms?: number | null;
  cost?: number | null;
  gatekeeper?: {
    model?: string | null;
    approved?: boolean | null;
    recommended_action?: string | null;
    veto_reason?: string | null;
  } | null;
};

type DebateDetail = {
  id: number;
  matchup?: string | null;
  league?: string | null;
  market_type?: string | null;
  consensus_probability?: number | null;
  confidence_band_low?: number | null;
  confidence_band_high?: number | null;
  agreement_rate?: number | null;
  approved?: boolean | null;
  recommended_action?: string | null;
  created_at?: string | null;
  cost_estimate?: number | null;
  latency_ms?: number | null;
  redacted?: boolean | null;
  analyst_verdicts?: AnalystVerdict[] | null;
  moderator_synthesis?: ModeratorSynthesis | null;
};

type DebatesResponse = {
  items: DebateDetail[];
  tier?: string;
  delayed?: boolean;
  delay_minutes?: number;
};

type DebateByIdResponse = {
  item: DebateDetail;
  tier?: string;
  delayed?: boolean;
  delay_minutes?: number;
};

function fmtPct(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function leanColor(lean: string | null | undefined, colors: ReturnType<typeof useColors>) {
  if (lean === "YES") return colors.success;
  if (lean === "NO") return colors.destructive;
  return colors.mutedForeground;
}

export default function DebateDetailScreen() {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const { id } = useLocalSearchParams<{ id: string }>();
  const debateId = Number(id);

  // Reuse the same queryKey as the research tab so the cached list
  // satisfies us instantly when the user came in from there. If the
  // detail screen is opened cold (deep link / refresh) the query just
  // re-fetches with limit=25.
  const debates = useQuery({
    queryKey: ["dashboard", "recent-debates", "research"],
    queryFn: () =>
      apiFetch<DebatesResponse>("/api/dashboard/recent-debates?limit=25"),
  });

  const itemFromList =
    (debates.data?.items ?? []).find((d) => d.id === debateId) ?? null;

  // Per-id fallback: if the debate isn't in the cached recent feed
  // (older than the top 25, evicted, or this screen was opened cold
  // from a deep link / push notification), fetch it directly. The
  // backend applies the exact same Pro-vs-free redaction the list
  // endpoint does, so the rendering below works identically either
  // way.
  const debateById = useQuery({
    queryKey: ["dashboard", "debate-by-id", debateId],
    queryFn: () =>
      apiFetch<DebateByIdResponse>(`/api/dashboard/debates/${debateId}`),
    enabled:
      Number.isFinite(debateId) &&
      !debates.isLoading &&
      itemFromList === null &&
      // Don't fall back when the list call itself failed — the user
      // already sees the list error and we'd just pile on a second
      // (likely identical) failure.
      !debates.isError,
  });

  const item = itemFromList ?? debateById.data?.item ?? null;
  const isLoading =
    debates.isLoading || (itemFromList === null && debateById.isLoading);
  const isError = debates.isError || debateById.isError;
  const status =
    (debates.error as { status?: number } | null)?.status ??
    (debateById.error as { status?: number } | null)?.status;

  const headerTitle = item?.matchup ?? `DEBATE #${debateId}`;

  return (
    <View style={{ flex: 1, backgroundColor: colors.background }}>
      <Stack.Screen
        options={{
          title: headerTitle,
          headerStyle: { backgroundColor: colors.background },
          headerTitleStyle: {
            color: colors.foreground,
            fontFamily: "Inter_700Bold",
            fontSize: 14,
          },
          headerTintColor: colors.primary,
          headerBackTitle: "Back",
        }}
      />
      {isLoading ? (
        <View style={styles.center}>
          <ActivityIndicator color={colors.primary} />
        </View>
      ) : isError && !item ? (
        <View style={[styles.center, { padding: 24 }]}>
          <Text style={{ color: colors.destructive }}>
            {status === 403
              ? "Awaiting operator approval"
              : status === 404
                ? "Debate not found."
                : `Failed to load (HTTP ${status ?? "?"})`}
          </Text>
          <Pressable
            onPress={() => router.back()}
            style={[styles.backBtn, { borderColor: colors.border }]}
            testID="button-debate-detail-back"
          >
            <Text style={{ color: colors.foreground }}>Back</Text>
          </Pressable>
        </View>
      ) : !item ? (
        <View style={[styles.center, { padding: 24 }]}>
          <Text style={{ color: colors.mutedForeground }}>
            Debate not found.
          </Text>
          <Pressable
            onPress={() => router.back()}
            style={[styles.backBtn, { borderColor: colors.border }]}
            testID="button-debate-detail-back"
          >
            <Text style={{ color: colors.foreground }}>Back</Text>
          </Pressable>
        </View>
      ) : (
        <ScrollView
          contentContainerStyle={[
            styles.container,
            { paddingBottom: insets.bottom + 32 },
          ]}
          testID="debate-detail-scroll"
        >
          <Card style={styles.card} testID="debate-detail-summary">
            <View style={styles.rowHead}>
              <Text
                style={[styles.title, { color: colors.foreground }]}
                numberOfLines={2}
              >
                {item.matchup ?? `#${item.id}`}
              </Text>
              <Text
                style={[
                  styles.badge,
                  {
                    color: item.approved ? colors.success : colors.mutedForeground,
                    borderColor: item.approved ? colors.success : colors.border,
                  },
                ]}
              >
                {item.approved ? "APPROVED" : "REJECTED"}
              </Text>
            </View>
            <View style={styles.statsRow}>
              <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                {(item.league ?? "—").toUpperCase()} ·{" "}
                {(item.market_type ?? "—").toUpperCase()}
              </Text>
              <Text style={[styles.stat, { color: colors.primary }]}>
                ACTION {(item.recommended_action ?? "—").toUpperCase()}
              </Text>
            </View>
            <View style={styles.statsRow}>
              <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                consensus {fmtPct(item.consensus_probability, 1)}
              </Text>
              <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                band {fmtPct(item.confidence_band_low)}–{fmtPct(item.confidence_band_high)}
              </Text>
              <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                agree {fmtPct(item.agreement_rate)}
              </Text>
            </View>
          </Card>

          {item.redacted ? (
            <Card
              style={[
                styles.card,
                { borderColor: colors.primary, backgroundColor: colors.primary + "11" },
              ]}
              testID="debate-detail-upsell"
            >
              <Text style={[styles.eyebrow, { color: colors.primary }]}>
                PRO ONLY
              </Text>
              <Text style={[styles.body, { color: colors.foreground }]}>
                Per-persona reasoning and the moderator's synthesis are part of
                the Pro plan. Upgrade to unlock the full debate transcript.
              </Text>
              <Pressable
                onPress={() => Linking.openURL(PRICING_URL)}
                style={[styles.cta, { borderColor: colors.primary }]}
                testID="button-debate-detail-upgrade"
              >
                <Text style={[styles.ctaText, { color: colors.primary }]}>
                  UPGRADE
                </Text>
              </Pressable>
            </Card>
          ) : (
            <>
              <SectionHeader>MODERATOR SYNTHESIS</SectionHeader>
              <Card style={styles.card} testID="debate-detail-moderator">
                {item.moderator_synthesis?.synthesis ? (
                  <Text style={[styles.body, { color: colors.foreground }]}>
                    {item.moderator_synthesis.synthesis}
                  </Text>
                ) : (
                  <Text style={[styles.body, { color: colors.mutedForeground }]}>
                    No synthesis recorded.
                  </Text>
                )}
                <View style={styles.statsRow}>
                  <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                    moderator {item.moderator_synthesis?.model ?? "—"}
                  </Text>
                  <Text style={[styles.stat, { color: colors.primary }]}>
                    REC {(item.moderator_synthesis?.recommended_action ?? "—").toUpperCase()}
                  </Text>
                </View>
                {item.moderator_synthesis?.gatekeeper ? (
                  <View
                    style={[
                      styles.gateBox,
                      {
                        borderColor:
                          item.moderator_synthesis.gatekeeper.approved
                            ? colors.success
                            : colors.destructive,
                      },
                    ]}
                  >
                    <Text
                      style={[
                        styles.eyebrow,
                        {
                          color: item.moderator_synthesis.gatekeeper.approved
                            ? colors.success
                            : colors.destructive,
                        },
                      ]}
                    >
                      GATEKEEPER ·{" "}
                      {item.moderator_synthesis.gatekeeper.approved
                        ? "APPROVED"
                        : "VETOED"}
                    </Text>
                    {item.moderator_synthesis.gatekeeper.veto_reason ? (
                      <Text
                        style={[styles.body, { color: colors.foreground }]}
                      >
                        {item.moderator_synthesis.gatekeeper.veto_reason}
                      </Text>
                    ) : null}
                    <Text
                      style={[styles.stat, { color: colors.mutedForeground }]}
                    >
                      {item.moderator_synthesis.gatekeeper.model ?? "—"} ·{" "}
                      FINAL{" "}
                      {(item.moderator_synthesis.gatekeeper.recommended_action ??
                        "—").toUpperCase()}
                    </Text>
                  </View>
                ) : null}
              </Card>

              <SectionHeader>ANALYST VERDICTS</SectionHeader>
              {(item.analyst_verdicts ?? []).length === 0 ? (
                <Card>
                  <Text style={{ color: colors.mutedForeground, fontSize: 12 }}>
                    No persona verdicts recorded.
                  </Text>
                </Card>
              ) : (
                (item.analyst_verdicts ?? []).map((v, idx) => (
                  <Card
                    key={`${v.persona ?? "p"}-${idx}`}
                    style={styles.card}
                    testID={`debate-detail-persona-${v.persona ?? idx}`}
                  >
                    <View style={styles.rowHead}>
                      <Text
                        style={[styles.title, { color: colors.foreground }]}
                        numberOfLines={1}
                      >
                        {(v.persona ?? "?").toUpperCase()}
                      </Text>
                      <Text
                        style={[styles.stat, { color: leanColor(v.lean, colors) }]}
                      >
                        {(v.lean ?? "—").toUpperCase()} ·{" "}
                        {fmtPct(v.probability)}
                      </Text>
                    </View>
                    <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                      {(v.provider ?? "—")}/{v.model ?? "—"}
                      {v.fallback_used
                        ? ` · FALLBACK${v.fallback_reason ? ` (${v.fallback_reason})` : ""}`
                        : ""}
                    </Text>
                    {v.error ? (
                      <Text
                        style={[styles.body, { color: colors.destructive }]}
                      >
                        ERR · {v.error}
                      </Text>
                    ) : v.reasoning ? (
                      <Text style={[styles.body, { color: colors.foreground }]}>
                        {v.reasoning}
                      </Text>
                    ) : (
                      <Text
                        style={[styles.body, { color: colors.mutedForeground }]}
                      >
                        No reasoning recorded.
                      </Text>
                    )}
                    <Text style={[styles.stat, { color: colors.mutedForeground }]}>
                      conf {fmtPct(v.confidence)}
                      {typeof v.weight === "number"
                        ? ` · weight ${v.weight.toFixed(2)}`
                        : ""}
                      {typeof v.latency_ms === "number"
                        ? ` · ${v.latency_ms.toFixed(0)}ms`
                        : ""}
                    </Text>
                  </Card>
                ))
              )}
            </>
          )}
        </ScrollView>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { padding: 16, gap: 8 },
  center: { flex: 1, alignItems: "center", justifyContent: "center", gap: 12 },
  card: { gap: 8 },
  rowHead: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    gap: 8,
  },
  title: {
    fontSize: 14,
    fontFamily: "Inter_600SemiBold",
    flex: 1,
    marginRight: 8,
  },
  body: { fontSize: 12, fontFamily: "Inter_400Regular", lineHeight: 18 },
  statsRow: { flexDirection: "row", gap: 12, flexWrap: "wrap" },
  stat: { fontSize: 12, fontFamily: "Inter_500Medium" },
  badge: {
    fontSize: 10,
    letterSpacing: 1,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderWidth: 1,
    fontFamily: "Inter_700Bold",
  },
  eyebrow: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
  cta: {
    borderWidth: 1,
    paddingHorizontal: 10,
    paddingVertical: 6,
    alignSelf: "flex-start",
  },
  ctaText: {
    fontSize: 11,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
  gateBox: {
    borderWidth: 1,
    padding: 8,
    gap: 4,
  },
  backBtn: {
    borderWidth: 1,
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
});
