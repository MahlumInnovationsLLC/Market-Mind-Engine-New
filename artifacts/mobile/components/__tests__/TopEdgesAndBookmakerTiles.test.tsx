import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import React from "react";

const mockApiFetch = jest.fn();

jest.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
  ApiError: class ApiError extends Error {},
}));

import {
  TopEdgesTile,
  BookmakerMetricsTile,
} from "../TopEdgesAndBookmakerTiles";

function renderTile(node: React.ReactElement): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

beforeEach(() => jest.clearAllMocks());

describe("Mobile TopEdgesTile", () => {
  it("renders empty state when items=[]", async () => {
    mockApiFetch.mockResolvedValueOnce({
      limit: 25,
      generated_at: "2026-05-22T12:00:00Z",
      items: [],
      halted_verticals: [],
      error: null,
    });
    renderTile(<TopEdgesTile />);
    expect(await screen.findByTestId("top-edges-empty")).toBeTruthy();
  });

  it("renders rows + greys halted vertical", async () => {
    mockApiFetch.mockResolvedValueOnce({
      limit: 25,
      generated_at: "2026-05-22T12:00:00Z",
      items: [
        {
          prediction_id: 1, matchup: "LAL @ BOS", league: "NBA",
          vertical: "sports", vertical_halted: false,
          model_prob: 0.6, market_price: 0.5, edge_pct: 8.5,
          confidence: 0.7, planned_stake: null, kalshi_market_id: "KX1",
        },
        {
          prediction_id: 2, matchup: "BTC > 70k", league: "BTC",
          vertical: "crypto", vertical_halted: true,
          model_prob: 0.55, market_price: 0.48, edge_pct: 7.0,
          confidence: 0.6, planned_stake: null, kalshi_market_id: "KX2",
        },
      ],
      halted_verticals: ["crypto"],
      error: null,
    });
    renderTile(<TopEdgesTile />);
    expect(await screen.findByTestId("top-edge-row-1")).toBeTruthy();
    expect(screen.getByTestId("top-edge-row-2")).toBeTruthy();
    expect(screen.getByTestId("top-edge-edge-1").props.children).toMatch(/8\.50%/);
  });
});

describe("Mobile BookmakerMetricsTile", () => {
  it("renders six cells when data lands", async () => {
    mockApiFetch.mockResolvedValueOnce({
      window: "7d",
      generated_at: "2026-05-22T12:00:00Z",
      profit_factor: 1.85,
      equivalent_unit_pnl: 12.34,
      unit_dollars: 100,
      win_rate: 0.62,
      n_settled: 42,
      top_confidence_today: 0.78,
      highest_edge_today: 9.12,
      picks_today: { total: 17, by_vertical: [
        { vertical: "sports", n: 10 }, { vertical: "crypto", n: 7 },
      ] },
      error: null,
    });
    renderTile(<BookmakerMetricsTile />);
    expect(await screen.findByTestId("bm-profit-factor")).toBeTruthy();
    expect(screen.getByTestId("bm-unit-pnl")).toBeTruthy();
    expect(screen.getByTestId("bm-hit-rate")).toBeTruthy();
    expect(screen.getByTestId("bm-top-confidence")).toBeTruthy();
    expect(screen.getByTestId("bm-highest-edge")).toBeTruthy();
    expect(screen.getByTestId("bm-picks-today")).toBeTruthy();
  });

  it("changes window via chip", async () => {
    mockApiFetch.mockResolvedValue({
      window: "7d", generated_at: "2026-05-22T12:00:00Z",
      profit_factor: null, equivalent_unit_pnl: 0, unit_dollars: 100,
      win_rate: null, n_settled: 0,
      top_confidence_today: null, highest_edge_today: null,
      picks_today: { total: 0, by_vertical: [] }, error: null,
    });
    renderTile(<BookmakerMetricsTile />);
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());
    const ytd = screen.getByTestId("bm-window-ytd");
    fireEvent.press(ytd);
    await waitFor(() =>
      expect(
        mockApiFetch.mock.calls.some((c) =>
          String(c[0]).includes("window=ytd"),
        ),
      ).toBe(true),
    );
  });
});
