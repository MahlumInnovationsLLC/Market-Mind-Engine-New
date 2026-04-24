import { Feather } from "@expo/vector-icons";
import React, { useMemo, useState } from "react";
import {
  FlatList,
  Modal,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useColors } from "@/hooks/useColors";
import {
  TIMEZONES,
  TimeZoneOption,
  offsetForZone,
  searchTimezones,
} from "@/lib/timezones";

export type TimezonePickerProps = {
  visible: boolean;
  selected: string | null;
  onClose: () => void;
  onSelect: (id: string) => void;
  onUseDevice?: () => void;
};

export function TimezonePicker({
  visible,
  selected,
  onClose,
  onSelect,
  onUseDevice,
}: TimezonePickerProps): React.ReactElement {
  const colors = useColors();
  const insets = useSafeAreaInsets();
  const [query, setQuery] = useState("");

  const items = useMemo<TimeZoneOption[]>(
    () => searchTimezones(query),
    [query],
  );

  const handleSelect = (id: string) => {
    setQuery("");
    onSelect(id);
  };

  const handleClose = () => {
    setQuery("");
    onClose();
  };

  const handleUseDevice = () => {
    setQuery("");
    onUseDevice?.();
  };

  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent
      onRequestClose={handleClose}
    >
      <View style={styles.overlay}>
        <View
          style={[
            styles.container,
            { backgroundColor: colors.background, paddingBottom: insets.bottom },
          ]}
        >
          <View style={[styles.header, { borderBottomColor: colors.border }]}>
            <Text style={[styles.title, { color: colors.foreground }]}>
              Time zone
            </Text>
            <Pressable
              onPress={handleClose}
              accessibilityLabel="Close time zone picker"
              accessibilityRole="button"
              testID="tz-picker-close"
              style={({ pressed }) => [
                styles.closeButton,
                { opacity: pressed ? 0.6 : 1 },
              ]}
            >
              <Feather name="x" size={22} color={colors.foreground} />
            </Pressable>
          </View>

          <View style={styles.searchWrap}>
            <TextInput
              value={query}
              onChangeText={setQuery}
              placeholder="Search cities or regions"
              placeholderTextColor={colors.mutedForeground}
              autoCapitalize="none"
              autoCorrect={false}
              testID="tz-picker-search"
              style={[
                styles.search,
                {
                  color: colors.foreground,
                  borderColor: colors.border,
                  backgroundColor: colors.card,
                },
              ]}
            />
            {onUseDevice ? (
              <Pressable
                onPress={handleUseDevice}
                testID="tz-picker-use-device"
                style={({ pressed }) => [
                  styles.deviceButton,
                  {
                    borderColor: colors.border,
                    opacity: pressed ? 0.7 : 1,
                  },
                ]}
              >
                <Text
                  style={[styles.deviceButtonText, { color: colors.primary }]}
                >
                  Use device
                </Text>
              </Pressable>
            ) : null}
          </View>

          <FlatList
            data={items}
            keyExtractor={(item) => item.id}
            keyboardShouldPersistTaps="handled"
            initialNumToRender={20}
            ListEmptyComponent={
              <View style={styles.emptyWrap}>
                <Text style={[styles.empty, { color: colors.mutedForeground }]}>
                  No matching time zones.
                </Text>
              </View>
            }
            renderItem={({ item, index }) => {
              const isSelected = item.id === selected;
              const showRegion =
                index === 0 || items[index - 1].region !== item.region;
              return (
                <View>
                  {showRegion ? (
                    <Text
                      style={[
                        styles.regionHeader,
                        {
                          color: colors.mutedForeground,
                          backgroundColor: colors.background,
                          borderBottomColor: colors.border,
                        },
                      ]}
                    >
                      {item.region.toUpperCase()}
                    </Text>
                  ) : null}
                  <Pressable
                    onPress={() => handleSelect(item.id)}
                    testID={`tz-option-${item.id}`}
                    style={({ pressed }) => [
                      styles.row,
                      {
                        borderBottomColor: colors.border,
                        backgroundColor: isSelected
                          ? colors.card
                          : pressed
                            ? colors.card
                            : "transparent",
                      },
                    ]}
                  >
                    <View style={{ flex: 1 }}>
                      <Text
                        style={[styles.rowCity, { color: colors.foreground }]}
                      >
                        {item.city}
                      </Text>
                      <Text
                        style={[
                          styles.rowId,
                          { color: colors.mutedForeground },
                        ]}
                      >
                        {item.id}
                      </Text>
                    </View>
                    <Text
                      style={[styles.offset, { color: colors.mutedForeground }]}
                    >
                      {offsetForZone(item.id)}
                    </Text>
                    {isSelected ? (
                      <Feather
                        name="check"
                        size={18}
                        color={colors.primary}
                        style={{ marginLeft: 8 }}
                      />
                    ) : null}
                  </Pressable>
                </View>
              );
            }}
          />
        </View>
      </View>
    </Modal>
  );
}

export const TIMEZONE_PICKER_OPTIONS = TIMEZONES;

const styles = StyleSheet.create({
  overlay: {
    flex: 1,
    backgroundColor: "rgba(0, 0, 0, 0.5)",
    justifyContent: "flex-end",
  },
  container: {
    width: "100%",
    height: "85%",
    borderTopLeftRadius: 16,
    borderTopRightRadius: 16,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 16,
    paddingTop: 16,
    paddingBottom: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  title: { fontSize: 16, fontFamily: "Inter_600SemiBold" },
  closeButton: {
    width: 36,
    height: 36,
    alignItems: "center",
    justifyContent: "center",
  },
  searchWrap: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 16,
    paddingTop: 12,
    paddingBottom: 8,
    gap: 8,
  },
  search: {
    flex: 1,
    height: 40,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 8,
    paddingHorizontal: 12,
    fontSize: 14,
    fontFamily: "Inter_400Regular",
  },
  deviceButton: {
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 6,
    borderWidth: StyleSheet.hairlineWidth,
  },
  deviceButtonText: { fontSize: 13, fontFamily: "Inter_600SemiBold" },
  regionHeader: {
    fontSize: 10,
    letterSpacing: 1.5,
    fontFamily: "Inter_600SemiBold",
    paddingHorizontal: 16,
    paddingTop: 12,
    paddingBottom: 4,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  rowCity: { fontSize: 15, fontFamily: "Inter_600SemiBold" },
  rowId: { fontSize: 11, fontFamily: "Inter_400Regular", marginTop: 2 },
  offset: { fontSize: 12, fontFamily: "Inter_500Medium", minWidth: 60, textAlign: "right" },
  emptyWrap: { paddingVertical: 32, alignItems: "center" },
  empty: { fontSize: 13, fontFamily: "Inter_400Regular" },
});
