import React from "react";
import { StyleSheet, Text, View, ViewProps } from "react-native";

import { useColors } from "@/hooks/useColors";

export function Card({ style, children, ...rest }: ViewProps) {
  const colors = useColors();
  return (
    <View
      {...rest}
      style={[
        styles.card,
        { backgroundColor: colors.card, borderColor: colors.border },
        style,
      ]}
    >
      {children}
    </View>
  );
}

export function StatTile({
  label,
  value,
  hint,
  testID,
}: {
  label: string;
  value: string;
  hint?: string;
  testID?: string;
}) {
  const colors = useColors();
  return (
    <Card style={styles.tile} testID={testID}>
      <Text style={[styles.tileLabel, { color: colors.mutedForeground }]}>
        {label}
      </Text>
      <Text style={[styles.tileValue, { color: colors.foreground }]}>{value}</Text>
      {hint ? (
        <Text style={[styles.tileHint, { color: colors.mutedForeground }]}>
          {hint}
        </Text>
      ) : null}
    </Card>
  );
}

export function SectionHeader({ children }: { children: React.ReactNode }) {
  const colors = useColors();
  return (
    <Text
      style={[
        styles.sectionHeader,
        { color: colors.mutedForeground, borderBottomColor: colors.border },
      ]}
    >
      {children}
    </Text>
  );
}

const styles = StyleSheet.create({
  card: {
    borderWidth: 1,
    padding: 16,
  },
  tile: {
    flex: 1,
    minWidth: 140,
    gap: 4,
  },
  tileLabel: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_500Medium",
  },
  tileValue: {
    fontSize: 22,
    fontFamily: "Inter_700Bold",
  },
  tileHint: {
    fontSize: 11,
    fontFamily: "Inter_400Regular",
  },
  sectionHeader: {
    fontSize: 11,
    letterSpacing: 2,
    fontFamily: "Inter_600SemiBold",
    paddingBottom: 8,
    marginBottom: 12,
    borderBottomWidth: 1,
  },
});
