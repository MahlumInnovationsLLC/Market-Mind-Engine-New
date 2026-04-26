import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { state } = vi.hoisted(() => ({
  state: {
    overview: {
      data: {
        verticals: [
          { vertical: "nba", hidden: false },
          { vertical: "nfl", hidden: false },
          { vertical: "secret", hidden: true },
        ],
      },
      isLoading: false,
    } as { data: unknown; isLoading: boolean },
    visibility: { data: { hidden: ["secret"] } } as { data: unknown },
    selected: new Set<string>(),
    toggle: vi.fn(),
    clear: vi.fn(),
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  useGetVerticalsOverview: () => state.overview,
  useGetVerticalVisibility: () => state.visibility,
  getGetVerticalsOverviewQueryKey: () => ["vertical-overview"],
  getGetVerticalVisibilityQueryKey: () => ["vertical-visibility"],
}));

vi.mock("@/hooks/use-selected-verticals", () => ({
  useSelectedVerticals: () => ({
    selected: state.selected,
    isSelected: (v: string) => state.selected.has(v),
    toggle: state.toggle,
    clear: state.clear,
  }),
}));

import VerticalPicker from "./vertical-picker";

afterEach(() => {
  cleanup();
  state.selected = new Set();
  state.toggle.mockReset();
  state.clear.mockReset();
});

function renderPicker(isAdmin = false) {
  const hook = memoryLocation({ path: "/", record: true });
  return render(
    <Router hook={hook.hook}>
      <VerticalPicker isAdmin={isAdmin} />
    </Router>,
  );
}

describe("VerticalPicker", () => {
  it("hides admin-flagged 'secret' vertical from non-admin operators", () => {
    renderPicker(false);
    expect(screen.getByTestId("vertical-picker")).toBeTruthy();
    // nba + nfl visible, secret hidden because the visibility list
    // shadows it for non-admins.
    expect(screen.getByText(/nba/i)).toBeTruthy();
    expect(screen.getByText(/nfl/i)).toBeTruthy();
    expect(screen.queryByText(/secret/i)).toBeNull();
  });

  it("shows the hidden 'secret' chip for admin operators", () => {
    renderPicker(true);
    expect(screen.getByText(/secret/i)).toBeTruthy();
  });

  it("invokes the clear callback when the 'all' chip is clicked", () => {
    renderPicker(false);
    // The first button in the picker is the 'clear' affordance.
    const buttons = screen
      .getByTestId("vertical-picker")
      .querySelectorAll("button");
    fireEvent.click(buttons[0]!);
    expect(state.clear).toHaveBeenCalled();
  });
});
