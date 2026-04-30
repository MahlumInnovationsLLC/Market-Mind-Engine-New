import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import React from "react";

import { PlacedByProvider, usePlacedBy } from "./placed-by-context";
import { PlacedByToggle } from "./placed-by-toggle";

afterEach(() => {
  cleanup();
});

/**
 * Render helper: wires a wouter `memoryLocation` so the
 * provider's `useLocation`/`useSearch` plumbing works without
 * touching window.history.
 */
function renderWithRouter(
  ui: React.ReactElement,
  initialPath = "/dashboard",
) {
  const { hook } = memoryLocation({ path: initialPath, record: true });
  return render(
    <Router hook={hook}>
      <PlacedByProvider>{ui}</PlacedByProvider>
    </Router>,
  );
}

function pressed(testId: string): string | null {
  return screen.getByTestId(testId).getAttribute("aria-pressed");
}

describe("PlacedByToggle (T004)", () => {
  it("renders all 3 segments with the documented default = all (Both)", () => {
    renderWithRouter(<PlacedByToggle />);

    const allBtn = screen.getByTestId("button-placed-by-all");
    const manualBtn = screen.getByTestId("button-placed-by-manual");
    const systemBtn = screen.getByTestId("button-placed-by-system");

    expect(pressed("button-placed-by-all")).toBe("true");
    expect(pressed("button-placed-by-manual")).toBe("false");
    expect(pressed("button-placed-by-system")).toBe("false");
    expect(allBtn.textContent).toBe("BOTH");
    expect(manualBtn.textContent).toBe("MANUAL");
    expect(systemBtn.textContent).toBe("SYSTEM");
  });

  it("flips aria-pressed when a segment is clicked", () => {
    renderWithRouter(<PlacedByToggle />);

    fireEvent.click(screen.getByTestId("button-placed-by-manual"));

    expect(pressed("button-placed-by-manual")).toBe("true");
    expect(pressed("button-placed-by-all")).toBe("false");
  });

  it("hydrates from the URL `?placed_by=` query string on mount", () => {
    renderWithRouter(<PlacedByToggle />, "/dashboard?placed_by=system");

    expect(pressed("button-placed-by-system")).toBe("true");
  });

  it("ignores garbage values and falls back to the default", () => {
    renderWithRouter(<PlacedByToggle />, "/dashboard?placed_by=evil_input");

    expect(pressed("button-placed-by-all")).toBe("true");
  });
});

describe("usePlacedBy outside a provider", () => {
  it("returns the default + a no-op setter so unwrapped components don't crash", () => {
    // We can't render a hook directly without a wrapper; use a tiny
    // probe component to assert the fallback contract from
    // placed-by-context.tsx.
    function Probe() {
      const ctx = usePlacedBy();
      return (
        <span data-testid="probe">
          {ctx.placedBy}/{typeof ctx.setPlacedBy}
        </span>
      );
    }
    render(<Probe />);
    expect(screen.getByTestId("probe").textContent).toBe("all/function");
  });
});
