import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    overview: { data: undefined as unknown, isLoading: false },
    verticals: { data: undefined as unknown },
    ws: { status: "open" as "open" | "closed" },
    filter: {
      selected: [] as string[],
      matches: () => true,
      filterActive: false,
      hiddenActive: false,
    },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  useGetDashboardOverview: () => state.overview,
  getGetDashboardOverviewQueryKey: () => ["dashboard-overview"],
  useGetVerticalsOverview: () => state.verticals,
  getGetVerticalsOverviewQueryKey: () => ["verticals-overview"],
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock("@/hooks/use-websocket", () => ({
  useWebSocket: () => state.ws,
}));

vi.mock("@/components/realtime/websocket-provider", () => ({
  usePipelineSubscription: vi.fn(),
}));

vi.mock("@/hooks/use-selected-verticals", () => ({
  useVerticalFilter: () => state.filter,
}));

import TopCounters from "./top-counters";

afterEach(() => {
  cleanup();
  state.overview.data = undefined;
  state.overview.isLoading = false;
  state.verticals.data = undefined;
});

describe("TopCounters", () => {
  it("renders the loading skeleton while overview is loading", () => {
    state.overview.isLoading = true;
    const { container } = render(<TopCounters />);
    expect(container.querySelector(".animate-pulse")).toBeTruthy();
  });

  it("renders the trading_cycles, swarm_debates and ml_training_logs counts from data.counts", () => {
    state.overview.data = {
      total_pnl: 12345,
      win_rate: 0.62,
      open_positions: 5,
      daily_pnl: 320,
      monthly_pnl: 4500,
      latest_cycle_at: "2026-04-24T12:00:00Z",
      latest_trade_at: "2026-04-24T11:00:00Z",
      counts: {
        trades: 87,
        trading_cycles: 100,
        swarm_debates: 250,
        ml_training_logs: 30,
      },
    };
    state.verticals.data = { verticals: [] };
    render(<TopCounters />);
    expect(screen.getByText("100")).toBeTruthy();
    expect(screen.getByText("250")).toBeTruthy();
    expect(screen.getByText("30")).toBeTruthy();
  });
});
