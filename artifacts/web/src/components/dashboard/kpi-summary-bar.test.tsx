import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    overview: {
      data: undefined as unknown,
      isLoading: false,
    },
    trades: {
      data: undefined as unknown,
      isLoading: false,
    },
    verticals: {
      data: undefined as unknown,
      isLoading: false,
    },
    equity: {
      data: undefined as unknown,
      isLoading: false,
    },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetDashboardOverviewQueryKey: () => ["dashboard-overview"],
  getGetRecentTradesQueryKey: () => ["recent-trades"],
  getGetVerticalsOverviewQueryKey: () => ["verticals-overview"],
  // `useVerticalFilter` (consumed by KpiSummaryBar via the placed_by /
  // vertical wiring) reads access + visibility + the same
  // verticals-overview query that powers the per-vertical breakdown.
  // We share the `state.verticals` source so the registry list visible
  // to the hook is whatever the test populates for the breakdown.
  getGetMyAccessQueryKey: () => ["my-access"],
  getGetVerticalVisibilityQueryKey: () => ["vertical-visibility"],
  useGetDashboardOverview: () => state.overview,
  useGetRecentTrades: () => state.trades,
  useGetVerticalsOverview: () => state.verticals,
  useGetMyAccess: () => ({ data: { is_admin: false } }),
  useGetVerticalVisibility: () => ({ data: { hidden: [], pinned: [] } }),
  useGetEdgeTerminalStatus: () => ({ data: undefined }),
  getGetEdgeTerminalStatusQueryKey: () => ["edge-terminal-status"],
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => state.equity,
}));

vi.mock("./period-context", () => ({
  usePeriod: () => ({ period: "30d", days: 30 }),
}));

import KpiSummaryBar from "./kpi-summary-bar";

afterEach(() => {
  cleanup();
  state.overview.data = undefined;
  state.overview.isLoading = false;
  state.trades.data = undefined;
  state.trades.isLoading = false;
  state.verticals.data = undefined;
  state.verticals.isLoading = false;
  state.equity.data = undefined;
  state.equity.isLoading = false;
});

describe("KpiSummaryBar", () => {
  it("renders skeleton tiles while the overview is loading", () => {
    state.overview.isLoading = true;
    state.equity.isLoading = true;
    const { container } = render(<KpiSummaryBar />);
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(
      0,
    );
  });

  it("renders the six KPI tiles when all four queries land", () => {
    state.overview.data = {
      win_rate: 0.55,
      open_positions: 3,
      realized_pnl: 1234,
      latest_executed_at: "2026-04-24T10:00:00Z",
    };
    state.trades.data = {
      items: [
        {
          quantity: 10,
          price: 0.5,
          executed_at: "2026-04-24T09:00:00Z",
          vertical: "nba",
        },
      ],
    };
    state.verticals.data = {
      verticals: [
        { vertical: "nba", trades: 100, sharpe: 1.4 },
        { vertical: "nfl", trades: 50, sharpe: 0.9 },
      ],
    };
    state.equity.data = {
      days: 30,
      points: [
        { date: "2026-04-23", daily_pnl: 100, cumulative_pnl: 1000, trades: 1 },
        { date: "2026-04-24", daily_pnl: 200, cumulative_pnl: 1200, trades: 2 },
      ],
    };
    render(<KpiSummaryBar />);
    expect(screen.getByText(/P&L Today/i)).toBeTruthy();
    expect(screen.getByText(/Win Rate/i)).toBeTruthy();
    expect(screen.getByTestId("kpi-roi")).toBeTruthy();
    expect(screen.getByText(/Volume/i)).toBeTruthy();
    expect(screen.getByText(/Open Positions/i)).toBeTruthy();
    expect(screen.getByText(/Sharpe/i)).toBeTruthy();
  });
});
