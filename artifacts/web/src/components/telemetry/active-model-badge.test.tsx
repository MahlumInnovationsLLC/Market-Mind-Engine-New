import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { healthState } = vi.hoisted(() => ({
  healthState: {
    data: undefined as unknown,
    isLoading: false,
    isError: false,
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  useHealthFull: () => healthState,
  getHealthFullQueryKey: () => ["health-full"],
}));

import ActiveModelBadge from "./active-model-badge";

afterEach(() => {
  cleanup();
  healthState.data = undefined;
  healthState.isLoading = false;
  healthState.isError = false;
});

describe("ActiveModelBadge", () => {
  it("renders the loading shell while the health query is in flight", () => {
    healthState.isLoading = true;
    render(<ActiveModelBadge />);
    expect(screen.getByTestId("badge-active-model-loading")).toBeTruthy();
  });

  it("renders a destructive 'NONE' badge when /api/health errors", () => {
    healthState.isError = true;
    render(<ActiveModelBadge />);
    const el = screen.getByTestId("badge-active-model-degraded");
    expect(el.textContent).toMatch(/NONE/);
    expect(el.className).toMatch(/destructive/);
  });

  it("renders a destructive 'NONE' badge when no predictor is bound", () => {
    healthState.data = { active_predictor: null };
    render(<ActiveModelBadge />);
    expect(screen.getByTestId("badge-active-model-degraded")).toBeTruthy();
  });

  it("renders the active model name and tone when a predictor is bound", () => {
    healthState.data = {
      active_predictor: {
        name: "vendor/path/model-v2",
        bound: true,
        loaded_at: 1714000000,
      },
    };
    render(<ActiveModelBadge />);
    // The short name (last path segment) is what shows on screen so
    // long org-prefixed model paths don't blow out the layout.
    expect(screen.getByText(/model-v2/i)).toBeTruthy();
  });
});
