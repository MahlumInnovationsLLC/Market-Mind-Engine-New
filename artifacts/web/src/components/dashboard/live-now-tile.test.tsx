import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: { data: undefined as unknown, isLoading: false, error: null as unknown },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetDashboardLiveNowQueryKey: () => ["live-now"],
  useGetDashboardLiveNow: () => state,
}));

import LiveNowTile from "./live-now-tile";

afterEach(() => {
  cleanup();
  state.data = undefined;
  state.isLoading = false;
  state.error = null;
});

const baseData = {
  fetched_at: "2026-05-22T12:00:00Z",
  totals: { live_games: 4, is_live_predictions: 9 },
  per_sport: {
    nba: {
      live_games: 2,
      is_live_predictions: 5,
      latest_fetched_at: "2026-05-22T12:00:00Z",
      oldest_staleness_seconds: 30,
      staleness_budget_seconds: 60,
      error: null,
    },
    nhl: {
      live_games: 1,
      is_live_predictions: 2,
      latest_fetched_at: "2026-05-22T11:59:00Z",
      oldest_staleness_seconds: 90,
      staleness_budget_seconds: 60,
      error: null,
    },
    mlb: {
      live_games: 1,
      is_live_predictions: 2,
      latest_fetched_at: "2026-05-22T11:55:00Z",
      oldest_staleness_seconds: 600,
      staleness_budget_seconds: 90,
      error: null,
    },
    soccer: {
      live_games: 0,
      is_live_predictions: 0,
      latest_fetched_at: null,
      oldest_staleness_seconds: null,
      staleness_budget_seconds: 30,
      error: null,
    },
  },
};

describe("LiveNowTile", () => {
  it("renders per-sport rows with appropriate tone", () => {
    state.data = baseData;
    render(<LiveNowTile />);
    expect(screen.getByTestId("live-now-row-nba").getAttribute("data-tone")).toBe(
      "ok",
    );
    expect(screen.getByTestId("live-now-row-nhl").getAttribute("data-tone")).toBe(
      "warn",
    );
    expect(screen.getByTestId("live-now-row-mlb").getAttribute("data-tone")).toBe(
      "bad",
    );
    expect(
      screen.getByTestId("live-now-row-soccer").getAttribute("data-tone"),
    ).toBe("neutral");
    expect(screen.getByTestId("live-now-staleness-nba").textContent).toMatch(
      /30s/,
    );
  });

  it("renders error state", () => {
    state.error = new Error("boom");
    render(<LiveNowTile />);
    expect(screen.getByTestId("live-now-error")).toBeTruthy();
  });
});
