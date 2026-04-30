import React from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { useColors } from "@/hooks/useColors";
import {
  type PlacedByFilter,
  usePlacedBy,
} from "@/lib/placed-by-filter";

/**
 * Mobile counterpart of the web `<PlacedByToggle>` (T005).
 *
 * 3-segment Manual / System / Both filter that drives the
 * `placed_by` query param on the dashboard / verticals / recent-trades
 * endpoints.  Visually mirrors the existing period-chip row on the
 * dashboard tab so the two controls read as a consistent control bar.
 *
 * Uses the same module-level pub/sub backing store as `vertical-filter`
 * so the choice survives navigation between tabs.
 */
type Segment = { key: PlacedByFilter; label: string };

const SEGMENTS: Segment[] = [
  { key: "manual", label: "MANUAL" },
  { key: "system", label: "SYSTEM" },
  { key: "all", label: "BOTH" },
];

export function PlacedByToggle({
  testID = "placed-by-toggle",
}: {
  testID?: string;
}) {
  const colors = useColors();
  const { placedBy, setPlacedBy } = usePlacedBy();

  return (
    <View
      style={[
        styles.row,
        { borderColor: colors.border, backgroundColor: colors.card },
      ]}
      testID={testID}
      accessibilityRole="radiogroup"
      accessibilityLabel="Trade provenance filter"
    >
      {SEGMENTS.map((seg) => {
        const active = placedBy === seg.key;
        return (
          <Pressable
            key={seg.key}
            onPress={() => setPlacedBy(seg.key)}
            style={[
              styles.chip,
              { backgroundColor: active ? colors.primary : "transparent" },
            ]}
            accessibilityRole="radio"
            accessibilityState={{ selected: active }}
            testID={`placed-by-${seg.key}`}
          >
            <Text
              style={[
                styles.label,
                {
                  color: active
                    ? colors.primaryForeground
                    : colors.mutedForeground,
                },
              ]}
            >
              {seg.label}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    borderWidth: 1,
    padding: 4,
    marginBottom: 12,
    gap: 4,
  },
  chip: {
    flex: 1,
    paddingVertical: 8,
    alignItems: "center",
  },
  label: {
    fontSize: 11,
    letterSpacing: 1.5,
    fontFamily: "Inter_700Bold",
  },
});
