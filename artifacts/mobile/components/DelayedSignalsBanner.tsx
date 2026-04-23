import { Linking, Pressable, StyleSheet, Text, View } from "react-native";

import { useColors } from "@/hooks/useColors";

/**
 * Free-tier upsell banner shown above any list whose backend response
 * carries `delayed: true` — i.e. the caller is on the free tier and the
 * 15-minute signal delay has been applied.
 *
 * Tapping "UPGRADE" opens the marketing pricing page in the system
 * browser; the mobile app doesn't host the pricing checkout itself.
 */
export interface DelayedSignalsBannerProps {
  delayMinutes?: number | null;
  testID?: string;
}

const PRICING_URL = "https://axiompredict.com/pricing";

export default function DelayedSignalsBanner({
  delayMinutes,
  testID = "banner-delayed-signals",
}: DelayedSignalsBannerProps) {
  const colors = useColors();
  const minutes = delayMinutes && delayMinutes > 0 ? delayMinutes : 15;
  return (
    <View
      style={[
        styles.row,
        { borderColor: colors.primary, backgroundColor: colors.primary + "11" },
      ]}
      testID={testID}
    >
      <View style={styles.text}>
        <Text style={[styles.eyebrow, { color: colors.primary }]}>
          FREE TIER · {minutes}-MIN DELAY
        </Text>
        <Text
          style={[styles.body, { color: colors.foreground }]}
          numberOfLines={2}
        >
          You're seeing signals delayed by {minutes} minutes. Upgrade to
          Pro for live data.
        </Text>
      </View>
      <Pressable
        onPress={() => Linking.openURL(PRICING_URL)}
        style={[styles.cta, { borderColor: colors.primary }]}
        testID="button-delayed-upgrade"
      >
        <Text style={[styles.ctaText, { color: colors.primary }]}>
          UPGRADE
        </Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    borderWidth: 1,
    paddingHorizontal: 12,
    paddingVertical: 8,
    marginBottom: 8,
    gap: 12,
  },
  text: { flex: 1 },
  eyebrow: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
    marginBottom: 2,
  },
  body: { fontSize: 11, fontFamily: "Inter_400Regular" },
  cta: {
    borderWidth: 1,
    paddingHorizontal: 10,
    paddingVertical: 6,
  },
  ctaText: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
});
