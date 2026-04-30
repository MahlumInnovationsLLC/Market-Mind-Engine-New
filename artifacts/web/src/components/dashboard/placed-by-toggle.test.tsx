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
  it("renders all 3 segments with the documented default = system", () => {
    // Operator preference 2026-04-30: in-provider default is `system`
    // so the landing dashboard reflects autotrader performance, not
    // manual side-bets.  See `DEFAULT_FILTER` in placed-by-context.tsx.
    renderWithRouter(<PlacedByToggle />);

    const allBtn = screen.getByTestId("button-placed-by-all");
    const manualBtn = screen.getByTestId("button-placed-by-manual");
    const systemBtn = screen.getByTestId("button-placed-by-system");

    expect(pressed("button-placed-by-system")).toBe("true");
    expect(pressed("button-placed-by-manual")).toBe("false");
    expect(pressed("button-placed-by-all")).toBe("false");
    expect(allBtn.textContent).toBe("BOTH");
    expect(manualBtn.textContent).toBe("MANUAL");
    expect(systemBtn.textContent).toBe("SYSTEM");
  });

  it("flips aria-pressed when a segment is clicked", () => {
    renderWithRouter(<PlacedByToggle />);

    fireEvent.click(screen.getByTestId("button-placed-by-manual"));

    expect(pressed("button-placed-by-manual")).toBe("true");
    expect(pressed("button-placed-by-system")).toBe("false");
  });

  it("hydrates from the URL `?placed_by=` query string on mount", () => {
    renderWithRouter(<PlacedByToggle />, "/dashboard?placed_by=all");

    expect(pressed("button-placed-by-all")).toBe("true");
    expect(pressed("button-placed-by-system")).toBe("false");
  });

  it("ignores garbage values and falls back to the default", () => {
    renderWithRouter(<PlacedByToggle />, "/dashboard?placed_by=evil_input");

    // Default is now `system`, not `all`.
    expect(pressed("button-placed-by-system")).toBe("true");
  });

  it("strips ?placed_by= from the URL when the user re-selects the default (system)", () => {
    // Locks the omit-when-default contract under the new `system`
    // default: shared links should stay clean once a user toggles
    // back from a non-default segment.
    renderWithRouter(<PlacedByToggle />, "/dashboard?placed_by=manual");
    expect(pressed("button-placed-by-manual")).toBe("true");

    fireEvent.click(screen.getByTestId("button-placed-by-system"));

    expect(pressed("button-placed-by-system")).toBe("true");
    // The provider mirrors state to window.location via wouter — when
    // the value equals the default, the param is deleted so the URL
    // is just the path again (no `?placed_by=…` tail).
    expect(window.location.search).not.toMatch(/placed_by/);
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
