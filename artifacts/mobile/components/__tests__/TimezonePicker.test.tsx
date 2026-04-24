/**
 * Component test for the searchable IANA time-zone picker that powers
 * the quiet-hours setting (task #350). Exercises the search filter,
 * the "use device" shortcut, and selection callbacks so a regression
 * that, e.g., stops emitting the IANA id or hides the device shortcut
 * surfaces here instead of slipping out to the device.
 */
import { fireEvent, render, screen } from "@testing-library/react-native";
import React from "react";

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
  SafeAreaProvider: ({ children }: { children: React.ReactNode }) => children,
}));

jest.mock("@/hooks/useColors", () => ({
  useColors: () => ({
    background: "#000",
    foreground: "#fff",
    card: "#111",
    border: "#222",
    primary: "#0af",
    primaryForeground: "#fff",
    mutedForeground: "#888",
    destructive: "#f00",
  }),
}));

import { TimezonePicker } from "@/components/TimezonePicker";

function renderPicker(overrides: Partial<React.ComponentProps<typeof TimezonePicker>> = {}) {
  const onClose = jest.fn();
  const onSelect = jest.fn();
  const onUseDevice = jest.fn();
  const utils = render(
    <TimezonePicker
      visible
      selected="America/New_York"
      onClose={onClose}
      onSelect={onSelect}
      onUseDevice={onUseDevice}
      {...overrides}
    />,
  );
  return { ...utils, onClose, onSelect, onUseDevice };
}

describe("TimezonePicker", () => {
  it("filters the list as the operator types in the search box", () => {
    renderPicker();
    const search = screen.getByTestId("tz-picker-search");

    fireEvent.changeText(search, "tokyo");
    expect(screen.getByTestId("tz-option-Asia/Tokyo")).toBeTruthy();
    expect(screen.queryByTestId("tz-option-America/New_York")).toBeNull();
  });

  it("shows the empty state when nothing matches", () => {
    renderPicker();
    fireEvent.changeText(
      screen.getByTestId("tz-picker-search"),
      "zzz_nothing_matches",
    );
    expect(screen.getByText("No matching time zones.")).toBeTruthy();
  });

  it("emits the IANA id when a zone is tapped", () => {
    const { onSelect } = renderPicker();
    fireEvent.changeText(screen.getByTestId("tz-picker-search"), "london");
    fireEvent.press(screen.getByTestId("tz-option-Europe/London"));
    expect(onSelect).toHaveBeenCalledWith("Europe/London");
  });

  it("calls onUseDevice when the shortcut is tapped", () => {
    const { onUseDevice } = renderPicker();
    fireEvent.press(screen.getByTestId("tz-picker-use-device"));
    expect(onUseDevice).toHaveBeenCalledTimes(1);
  });

  it("hides the device shortcut when no callback is supplied", () => {
    renderPicker({ onUseDevice: undefined });
    expect(screen.queryByTestId("tz-picker-use-device")).toBeNull();
  });

  it("dismisses via the close button", () => {
    const { onClose } = renderPicker();
    fireEvent.press(screen.getByTestId("tz-picker-close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
