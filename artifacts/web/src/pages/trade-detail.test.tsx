import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import React from "react";

const { state, ApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    status: number;
    data: unknown;
    constructor(status: number, data: unknown) {
      super(`HTTP ${status}`);
      this.status = status;
      this.data = data;
    }
  }
  return {
    ApiError: FakeApiError,
    state: {
      detail: {
        data: undefined as unknown,
        isLoading: false,
        isError: false,
        isSuccess: false,
        error: undefined as unknown,
      },
    },
  };
});

vi.mock("@workspace/api-client-react", () => ({
  ApiError,
  getGetTradeDetailQueryKey: (ref: string) => ["trade-detail", ref],
  useGetTradeDetail: () => state.detail,
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock("@/components/upgrade-required-modal", () => ({
  default: ({ open }: { open: boolean }) =>
    open
      ? React.createElement(
          "div",
          { "data-testid": "stub-upgrade-modal" },
          "upgrade modal",
        )
      : null,
}));

import TradeDetailPage from "./trade-detail";

afterEach(() => {
  cleanup();
  state.detail.data = undefined;
  state.detail.isLoading = false;
  state.detail.isError = false;
  state.detail.isSuccess = false;
  state.detail.error = undefined;
});

function renderAt(path: string) {
  const hook = memoryLocation({ path, record: true });
  return render(
    <Router hook={hook.hook}>
      <TradeDetailPage />
    </Router>,
  );
}

describe("TradeDetailPage", () => {
  it("renders 'Missing trade reference' when the URL has no :ref", () => {
    renderAt("/");
    expect(screen.getByText(/Missing trade reference/i)).toBeTruthy();
  });

  it("renders the loading frame while the detail query is in flight", () => {
    state.detail.isLoading = true;
    renderAt("/trade/abc");
    expect(screen.getByText(/loading trade detail/i)).toBeTruthy();
  });

  it("renders the destructive error block on a 404", () => {
    state.detail.isError = true;
    state.detail.error = new ApiError(404, null);
    renderAt("/trade/abc");
    expect(screen.getByText(/Trade not found/i)).toBeTruthy();
  });

  it("renders the upgrade-required panel and modal when the API returns 402", () => {
    state.detail.isError = true;
    state.detail.error = new ApiError(402, {
      detail: {
        error: "upgrade_required",
        feature: "trade_detail",
        plan_required: "pro",
      },
    });
    renderAt("/trade/abc");
    expect(screen.getByTestId("text-upgrade-required")).toBeTruthy();
    expect(screen.getByTestId("stub-upgrade-modal")).toBeTruthy();
  });

  it("renders the trade title and the empty-series sentinel when no samples exist", () => {
    state.detail.isSuccess = true;
    state.detail.data = {
      trade: {
        id: 7,
        trade_id: "T-7",
        market_id: "KX",
        market_title: "NBA Finals",
        side: "yes",
        action: "buy",
        quantity: 10,
        price: 0.55,
        model_prob: 0.6,
        model_edge: 0.05,
        pnl: 12.5,
        kelly_fraction: 0.04,
        executed_at: "2026-04-22T10:00:00Z",
        exchange: "kalshi",
        trading_mode: "live",
        vertical: "nba",
        league: "NBA",
      },
      series: [],
    };
    renderAt("/trade/abc");
    expect(screen.getByTestId("text-trade-title").textContent).toMatch(
      /NBA Finals/,
    );
    // No series → the chart placeholder must surface so the operator
    // knows there's no time-series rather than thinking the chart broke.
    expect(screen.getByTestId("text-no-series")).toBeTruthy();
    // No soccer summary attached → the match banner must be omitted.
    expect(screen.queryByTestId("card-trade-soccer-match")).toBeNull();
  });

  it("renders the soccer match banner with team names + scoreline when present", () => {
    state.detail.isSuccess = true;
    state.detail.data = {
      trade: {
        id: 9,
        trade_id: "T-9",
        market_id: "KX-EPL-DRAW",
        market_title: "EPL: Arsenal vs Chelsea — Draw",
        side: "yes",
        action: "buy",
        quantity: 5,
        price: 0.32,
        model_prob: 0.4,
        model_edge: 0.08,
        pnl: 3.4,
        kelly_fraction: 0.02,
        executed_at: "2026-04-22T10:00:00Z",
        exchange: "kalshi",
        trading_mode: "live",
        vertical: "soccer",
        league: "EPL",
      },
      series: [],
      soccer_match: {
        result: "draw",
        home_team: "Arsenal",
        away_team: "Chelsea",
        home_score: 1,
        away_score: 1,
        status: "closed",
      },
    };
    renderAt("/trade/abc");
    const banner = screen.getByTestId("card-trade-soccer-match");
    expect(banner).toBeTruthy();
    expect(screen.getByTestId("text-trade-soccer-teams").textContent).toMatch(
      /Arsenal/,
    );
    expect(screen.getByTestId("text-trade-soccer-teams").textContent).toMatch(
      /Chelsea/,
    );
    expect(screen.getByTestId("text-trade-soccer-score").textContent).toMatch(
      /1\s*–\s*1/,
    );
    expect(screen.getByTestId("badge-trade-soccer-result").textContent).toMatch(
      /DRAW/,
    );
  });
});
