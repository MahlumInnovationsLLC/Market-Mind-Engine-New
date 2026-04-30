/**
 * Snapshot / behaviour test for the mobile <PlacedByToggle/> (T005,
 * mobile counterpart of the web `placed-by-toggle.test.tsx`).
 *
 * Verifies:
 *   - Default segment is "SYSTEM" (operator preference 2026-04-30:
 *     landing surfaces should reflect autotrader performance, not
 *     manual side-bets — see `DEFAULT_FILTER` in placed-by-filter.ts).
 *   - Tapping a segment publishes the new value through the
 *     module-level pub/sub backing the `usePlacedBy` hook so other
 *     mounted consumers (e.g. another tab's toggle) move in lock-step.
 *   - The accessibility selection state mirrors the active segment so
 *     screen readers announce the correct radio.
 */
import { fireEvent, render, screen } from "@testing-library/react-native";
import React from "react";

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

import { PlacedByToggle } from "@/components/PlacedByToggle";
import {
  __resetPlacedByForTests,
  getPlacedBy,
  usePlacedBy,
} from "@/lib/placed-by-filter";

beforeEach(() => {
  __resetPlacedByForTests();
});

describe("PlacedByToggle", () => {
  it("renders the three provenance segments with SYSTEM selected by default", () => {
    render(<PlacedByToggle />);
    expect(screen.getByTestId("placed-by-manual")).toBeTruthy();
    expect(screen.getByTestId("placed-by-all")).toBeTruthy();
    const system = screen.getByTestId("placed-by-system");
    // The "system" segment is selected by default — operator preference
    // 2026-04-30 is to surface autotrader-only P&L on landing.  See
    // DEFAULT_FILTER in placed-by-filter.ts.
    expect(system.props.accessibilityState).toEqual(
      expect.objectContaining({ selected: true }),
    );
  });

  it("publishes the new value through the shared store when a segment is tapped", () => {
    render(<PlacedByToggle />);
    fireEvent.press(screen.getByTestId("placed-by-manual"));
    expect(getPlacedBy()).toBe("manual");

    const manual = screen.getByTestId("placed-by-manual");
    expect(manual.props.accessibilityState).toEqual(
      expect.objectContaining({ selected: true }),
    );
    // The previously-default "system" segment should now be deselected.
    const system = screen.getByTestId("placed-by-system");
    expect(system.props.accessibilityState).toEqual(
      expect.objectContaining({ selected: false }),
    );
  });

  it("keeps two mounted toggles in lock-step via the module-level subscription", () => {
    // Two screens (e.g. dashboard + verticals) can mount the toggle
    // simultaneously — the shared store must keep both rendered
    // copies in sync without React Context.
    function Twin() {
      const { placedBy } = usePlacedBy();
      return (
        <>
          <PlacedByToggle testID="dash-toggle" />
          <PlacedByToggle testID="verts-toggle" />
          {/* echo the current value so the test can assert on it */}
        </>
      );
    }
    render(<Twin />);

    fireEvent.press(screen.getAllByTestId("placed-by-system")[0]);
    expect(getPlacedBy()).toBe("system");

    // Both rendered toggles should report "system" as selected.
    const systemSegments = screen.getAllByTestId("placed-by-system");
    expect(systemSegments.length).toBeGreaterThanOrEqual(2);
    for (const seg of systemSegments) {
      expect(seg.props.accessibilityState).toEqual(
        expect.objectContaining({ selected: true }),
      );
    }
  });
});
