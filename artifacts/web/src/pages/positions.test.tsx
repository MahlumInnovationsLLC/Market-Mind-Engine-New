import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { state } = vi.hoisted(() => ({
  state: {
    auth: { isSignedIn: false, getToken: vi.fn(async () => null) },
    positions: {
      data: undefined as unknown,
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    },
    trades: { data: undefined as unknown, isLoading: false, refetch: vi.fn() },
  },
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => state.auth,
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => state.positions,
}));

vi.mock("@workspace/api-client-react", () => ({
  useGetRecentTrades: () => state.trades,
  getGetRecentTradesQueryKey: () => ["recent-trades"],
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(async () => ({ items: [], count: 0 })),
}));

import PositionsPage from "./positions";

afterEach(() => {
  cleanup();
  state.auth.isSignedIn = false;
  state.positions.data = undefined;
  state.positions.isLoading = false;
  state.positions.isError = false;
  state.trades.data = undefined;
  state.trades.isLoading = false;
});

function renderPage() {
  const { hook } = memoryLocation({ path: "/positions" });
  return render(
    <Router hook={hook}>
      <PositionsPage />
    </Router>,
  );
}

describe("PositionsPage", () => {
  it("renders the page-positions shell with the filters card for guest visitors", () => {
    renderPage();
    expect(screen.getByTestId("page-positions")).toBeTruthy();
    expect(screen.getByTestId("card-positions-filters")).toBeTruthy();
  });

  it("renders the positions table card when authenticated with no items", () => {
    state.auth.isSignedIn = true;
    state.positions.data = { items: [], count: 0 };
    state.trades.data = { items: [], next_cursor: null };
    renderPage();
    expect(screen.getByTestId("card-positions-table")).toBeTruthy();
  });

  it("renders one row-position-{ticker} testid per item when positions are returned", () => {
    state.auth.isSignedIn = true;
    state.positions.data = {
      items: [
        {
          market_ticker: "KX-NBA-LAL",
          market_title: "Lakers win tonight",
          bet_type: "YES",
          position: 5,
          market_exposure_cents: 250,
          realized_pnl_cents: 100,
          fees_paid_cents: 5,
          current_value_cents: 280,
          entry_price_cents: 50,
          last_price_cents: 56,
        },
      ],
      count: 1,
    };
    state.trades.data = { items: [], next_cursor: null };
    renderPage();
    expect(screen.getByTestId("row-position-KX-NBA-LAL")).toBeTruthy();
  });
});
