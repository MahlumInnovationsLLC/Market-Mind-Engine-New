import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { state } = vi.hoisted(() => ({
  state: {
    auth: { isSignedIn: true, getToken: vi.fn(async () => "tok") },
    live: {
      data: undefined as unknown,
      isLoading: false,
      isFetching: false,
      error: null as unknown,
      refetch: vi.fn(),
    },
    upcoming: {
      data: undefined as unknown,
      isLoading: false,
      error: null as unknown,
    },
    models: {
      data: { models: [], default_model_id: null },
      isLoading: false,
      error: null as unknown,
    },
  },
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => state.auth,
}));

let queryCall = 0;
vi.mock("@tanstack/react-query", () => ({
  useQuery: () => {
    queryCall += 1;
    if (queryCall === 1) return state.live;
    if (queryCall === 2) return state.upcoming;
    return state.models;
  },
  useMutation: () => ({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
  }),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(async () => ({})),
  formatErrorMessage: (e: unknown) => String(e),
}));

import LiveGamesPage from "./live-games";

function renderPage(path: string) {
  queryCall = 0;
  const { hook } = memoryLocation({ path });
  return render(
    <Router hook={hook}>
      <LiveGamesPage />
    </Router>,
  );
}

afterEach(() => {
  cleanup();
  state.live.data = undefined;
  state.upcoming.data = undefined;
});

const baseLive = (overrides: Partial<Record<string, unknown>> = {}) => ({
  market_id: "KX-NBA-LAL-1",
  game_id: "GAME-1",
  league: "NBA",
  sport: "nba",
  is_live: true,
  action: "HOLD",
  home_score: 50,
  away_score: 48,
  period: 2,
  time_remaining: "5:00",
  game_progress: 0.5,
  pre_game_prob: 0.5,
  updated_prob: 0.55,
  market_price: 0.52,
  current_edge: 0.03,
  last_update: "2026-05-22T12:00:00Z",
  swarm: null,
  tick: null,
  ...overrides,
});

describe("LiveGamesPage league filter (task #874)", () => {
  it("matches ?league=nba against rows whose raw league is uppercase 'NBA'", () => {
    state.live.data = {
      items: [baseLive({ league: "NBA", sport: "nba" })],
      is_live_now_count: 1,
      window_seconds: 21600,
      fetched_at: "2026-05-22T12:00:00Z",
    };
    state.upcoming.data = { items: [], count: 0, window_hours: 24, fetched_at: "" };
    renderPage("/live-games?league=nba");
    expect(screen.getByTestId("live-games-league-NBA")).toBeTruthy();
    expect(screen.queryByTestId("live-games-empty")).toBeNull();
  });

  it("matches ?league=soccer against rows whose raw league is a soccer league code ('EPL')", () => {
    state.live.data = {
      items: [
        baseLive({
          market_id: "KX-EPL-ARS-1",
          league: "EPL",
          sport: "soccer",
        }),
      ],
      is_live_now_count: 1,
      window_seconds: 21600,
      fetched_at: "2026-05-22T12:00:00Z",
    };
    state.upcoming.data = { items: [], count: 0, window_hours: 24, fetched_at: "" };
    renderPage("/live-games?league=soccer");
    expect(screen.getByTestId("live-games-league-EPL")).toBeTruthy();
    expect(screen.queryByTestId("live-games-empty")).toBeNull();
  });

  it("falls back to raw league code when no canonical sport key matches", () => {
    state.live.data = {
      items: [
        baseLive({
          market_id: "KX-MISC-1",
          league: "MISC",
          sport: null,
        }),
      ],
      is_live_now_count: 1,
      window_seconds: 21600,
      fetched_at: "2026-05-22T12:00:00Z",
    };
    state.upcoming.data = { items: [], count: 0, window_hours: 24, fetched_at: "" };
    renderPage("/live-games?league=misc");
    expect(screen.getByTestId("live-games-league-MISC")).toBeTruthy();
  });
});
