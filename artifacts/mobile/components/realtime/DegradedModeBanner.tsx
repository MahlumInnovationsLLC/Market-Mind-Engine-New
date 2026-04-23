import { useQuery } from "@tanstack/react-query";
import { Feather } from "@expo/vector-icons";
import { StyleSheet, Text, View } from "react-native";

import { useColors } from "@/hooks/useColors";
import { apiFetch } from "@/lib/api";

type HealthSnapshot = {
  status?: string | null;
  degraded?: boolean | null;
  reason?: string | null;
  message?: string | null;
  components?: Record<string, { status?: string | null; reason?: string | null }>;
};

function describe(snap: HealthSnapshot | undefined): string | null {
  if (!snap) return null;
  const status = (snap.status ?? "").toLowerCase();
  const degraded =
    snap.degraded === true ||
    status === "degraded" ||
    status === "down" ||
    status === "unhealthy";
  if (!degraded) return null;
  if (snap.reason) return snap.reason;
  if (snap.message) return snap.message;
  // Surface the first failing component if the snapshot doesn't include
  // a top-level reason.
  const comps = snap.components ?? {};
  for (const [name, info] of Object.entries(comps)) {
    const s = (info?.status ?? "").toLowerCase();
    if (s && s !== "ok" && s !== "healthy") {
      return `${name}: ${info?.reason ?? s}`;
    }
  }
  return "System running in degraded mode";
}

/**
 * Top-of-screen banner shown whenever /api/health/snapshot reports the
 * system as degraded. Mounted at the tab layout root so it appears on
 * every authenticated screen, mirroring the web shell behavior.
 */
export function DegradedModeBanner() {
  const c = useColors();
  const { data } = useQuery<HealthSnapshot>({
    queryKey: ["mobile", "health-snapshot"],
    queryFn: () => apiFetch<HealthSnapshot>("/api/health/snapshot"),
    refetchInterval: 30_000,
    retry: 0,
    staleTime: 15_000,
  });

  const reason = describe(data);
  if (!reason) return null;

  return (
    <View
      style={[
        styles.banner,
        { backgroundColor: c.warning + "22", borderColor: c.warning },
      ]}
      testID="mobile-degraded-banner"
    >
      <Feather name="alert-triangle" size={12} color={c.warning} />
      <Text
        style={[styles.label, { color: c.warning }]}
        numberOfLines={1}
      >
        DEGRADED
      </Text>
      <Text
        style={[styles.reason, { color: c.foreground }]}
        numberOfLines={1}
      >
        · {reason}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderBottomWidth: 1,
    paddingHorizontal: 12,
    paddingVertical: 6,
  },
  label: {
    fontFamily: "Inter_700Bold",
    fontSize: 10,
    letterSpacing: 1.2,
  },
  reason: {
    fontFamily: "Inter_400Regular",
    fontSize: 11,
    flexShrink: 1,
  },
});
