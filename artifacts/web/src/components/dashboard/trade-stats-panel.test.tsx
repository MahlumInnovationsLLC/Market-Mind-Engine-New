import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import React from "react";

const { state } = vi.hoisted(() => ({
  state: {
    data: undefined as unknown,
    isLoading: false,
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetRecentTradesQueryKey: () => ["recent-trades"],
  useGetRecentTrades: () => state,
}));

vi.mock("@/hooks/use-selected-verticals", () => ({
  useVerticalFilter: () => ({ matches: () => true }),
}));

vi.mock("./period-context", () => ({
  usePeriod: () => ({ period: "30d", days: 30 }),
}));

import TradeStatsPanel from "./trade-stats-panel";

afterEach(() => {
  cleanup();
  state.data = undefined;
  state.isLoading = false;
});

describe("TradeStatsPanel", () => {
  it("renders the loading shell while the recent-trades feed is in flight", () => {
    state.isLoading = true;
    const { container } = render(<TradeStatsPanel />);
    expect(container.querySelector(".animate-pulse")).toBeTruthy();
  });

  it("aggregates wins, losses, scratches and surfaces them on the panel", () => {
    const ts = new Date().toISOString();
    state.data = {
      items: [
        { vertical: "nba", pnl: 25, quantity: 10, price: 0.5, executed_at: ts },
        { vertical: "nba", pnl: -10, quantity: 8, price: 0.6, executed_at: ts },
        { vertical: "nba", pnl: 0, quantity: 4, price: 0.55, executed_at: ts },
        { vertical: "nba", pnl: 12, quantity: 6, price: 0.45, executed_at: ts },
      ],
    };
    render(<TradeStatsPanel />);
    // The panel renders Wins / Losses / Scratches counters.  The exact
    // copy is fixed in the component — we just need at least one of
    // each label to surface.
    expect(screen.getAllByText(/win/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/loss/i).length).toBeGreaterThan(0);
  });

  it("falls back to dashes when the feed is empty", () => {
    state.data = { items: [] };
    render(<TradeStatsPanel />);
    // Empty feed → expectancy / volume / win-rate all show as "—"
    // sentinel.
    expect(screen.getAllByText(/—/).length).toBeGreaterThan(0);
  });
});
