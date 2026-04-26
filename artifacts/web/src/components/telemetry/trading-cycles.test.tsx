import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    cycles: { data: undefined as unknown, isLoading: false },
    filter: {
      selected: [] as string[],
      matches: () => true,
    },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  useGetRecentCycles: () => state.cycles,
  getGetRecentCyclesQueryKey: () => ["recent-cycles"],
}));

vi.mock("@/hooks/use-selected-verticals", () => ({
  useVerticalFilter: () => state.filter,
}));

vi.mock("./slippage-guard-rejects", () => ({
  default: () => <div data-testid="slippage-guard-rejects-stub" />,
}));

import TradingCycles from "./trading-cycles";

afterEach(() => {
  cleanup();
  state.cycles.data = undefined;
  state.cycles.isLoading = false;
});

describe("TradingCycles", () => {
  it("renders the loading skeleton when cycles are still loading", () => {
    state.cycles.isLoading = true;
    const { container } = render(<TradingCycles />);
    expect(container.querySelector(".animate-pulse")).toBeTruthy();
  });

  it("renders NO_CYCLES_FOUND when items is empty", () => {
    state.cycles.data = { items: [] };
    render(<TradingCycles />);
    expect(screen.getByText("NO_CYCLES_FOUND")).toBeTruthy();
  });

  it("renders a row per cycle with cycle-row-{id} testid when items are returned", () => {
    state.cycles.data = {
      items: [
        {
          id: 101,
          started_at: "2026-04-24T12:00:00Z",
          completed_at: "2026-04-24T12:00:30Z",
          duration_seconds: 30.4,
          status: "completed",
          vertical: "nba",
          picks_evaluated: 5,
          trades_placed: 1,
          slippage_guard_rejects: 0,
          errors: null,
        },
      ],
    };
    render(<TradingCycles />);
    expect(screen.getByTestId("cycle-row-101")).toBeTruthy();
  });
});
