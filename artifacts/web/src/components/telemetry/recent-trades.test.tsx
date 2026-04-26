import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { state } = vi.hoisted(() => ({
  state: {
    trades: { data: undefined as unknown, isLoading: false },
    pipeline: { status: "open" as "open" | "closed" },
    filter: {
      selected: [] as string[],
      matches: () => true,
      filterActive: false,
      hiddenActive: false,
    },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  useGetRecentTrades: () => state.trades,
  getGetRecentTradesQueryKey: () => ["recent-trades"],
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock("@/hooks/use-websocket", () => ({
  useWebSocket: () => ({ status: "open" }),
}));

vi.mock("@/components/realtime/websocket-provider", () => ({
  usePipelineSubscription: vi.fn(),
  usePipelineSocket: () => state.pipeline,
}));

vi.mock("@/hooks/use-selected-verticals", () => ({
  useVerticalFilter: () => state.filter,
}));

vi.mock("@/components/delayed-signals-banner", () => ({
  default: () => <div data-testid="delayed-signals-banner-stub" />,
}));

import RecentTrades from "./recent-trades";

afterEach(() => {
  cleanup();
  state.trades.data = undefined;
  state.trades.isLoading = false;
});

function renderTrades() {
  const { hook } = memoryLocation({ path: "/" });
  return render(
    <Router hook={hook}>
      <RecentTrades />
    </Router>,
  );
}

describe("RecentTrades", () => {
  it("renders the loading skeleton while trades are loading", () => {
    state.trades.isLoading = true;
    const { container } = renderTrades();
    expect(container.querySelector(".animate-pulse")).toBeTruthy();
  });

  it("renders one trade-venue-badge per item with the normalized venue exposed via data-venue", () => {
    state.trades.data = {
      items: [
        {
          id: "trade-1",
          ticker: "KX-NBA-LAL",
          market_title: "Lakers win tonight",
          side: "yes",
          qty: 5,
          fill_price: 0.55,
          executed_at: "2026-04-24T10:00:00Z",
          exchange: "kalshi",
          vertical: "nba",
          pnl: 23,
        },
      ],
    };
    renderTrades();
    const badges = screen.getAllByTestId("trade-venue-badge");
    expect(badges.length).toBe(1);
    expect(badges[0]?.getAttribute("data-venue")).toBe("kalshi");
  });
});
