import {
  describe,
  it,
  expect,
  afterEach,
  beforeEach,
  vi,
} from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import React from "react";

const { apiState } = vi.hoisted(() => ({
  apiState: {
    stats: {
      realized_pnl: 12500,
      total_trades: 280,
      win_rate: 0.58,
      last_trade_at: "2026-04-24T11:00:00Z",
      trading_cycles: 41,
      swarm_debates: 95,
    } as unknown,
    signals: [
      {
        vertical: "nba",
        league: "NBA",
        side: "yes",
        model_prob: 0.62,
        entry_price: 0.5,
        actual_outcome: 1,
        settle_price: 1.0,
        settled_at: "2026-04-24T10:00:00Z",
      },
      {
        vertical: "nfl",
        league: "NFL",
        side: "no",
        model_prob: 0.4,
        entry_price: 0.6,
        actual_outcome: 0,
        settle_price: 0.0,
        settled_at: "2026-04-24T09:00:00Z",
      },
    ] as unknown[],
    statsRejects: false,
    signalsRejects: false,
  },
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(async (path: string) => {
    if (path.includes("/api/public/stats")) {
      if (apiState.statsRejects) throw new Error("stats down");
      return apiState.stats;
    }
    if (path.includes("/api/public/recent-signals")) {
      if (apiState.signalsRejects) throw new Error("signals down");
      return { signals: apiState.signals };
    }
    throw new Error(`unexpected path ${path}`);
  }),
  formatErrorMessage: (e: unknown) =>
    e instanceof Error ? e.message : String(e),
}));

vi.mock("@/components/public/public-nav", () => ({
  PublicNav: () =>
    React.createElement("div", { "data-testid": "stub-public-nav" }),
  PublicFooter: () =>
    React.createElement("div", { "data-testid": "stub-public-footer" }),
}));

import LeaderboardPage from "./leaderboard";

beforeEach(() => {
  apiState.statsRejects = false;
  apiState.signalsRejects = false;
});

afterEach(() => cleanup());

function renderPage() {
  const hook = memoryLocation({ path: "/leaderboard", record: true });
  return render(
    <Router hook={hook.hook}>
      <LeaderboardPage />
    </Router>,
  );
}

describe("LeaderboardPage", () => {
  it("renders the page title and the four stat cards once stats land", async () => {
    renderPage();
    expect(screen.getByTestId("text-leaderboard-title")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByTestId("stat-leaderboard-pnl")).toBeTruthy();
      expect(screen.getByTestId("stat-leaderboard-winrate")).toBeTruthy();
      expect(screen.getByTestId("stat-leaderboard-trades")).toBeTruthy();
      expect(screen.getByTestId("stat-leaderboard-debates")).toBeTruthy();
    });
  });

  it("renders one row per recent signal once the feed lands", async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("row-leaderboard-0")).toBeTruthy();
      expect(screen.getByTestId("row-leaderboard-1")).toBeTruthy();
    });
  });

  it("falls back to the empty-state copy when the recent-signals feed is empty", async () => {
    apiState.signals = [];
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("empty-leaderboard")).toBeTruthy();
    });
    // Restore for subsequent tests.
    apiState.signals = [
      {
        vertical: "nba",
        league: "NBA",
        side: "yes",
        model_prob: 0.62,
        entry_price: 0.5,
        actual_outcome: 1,
        settle_price: 1.0,
        settled_at: "2026-04-24T10:00:00Z",
      },
    ];
  });

  it("surfaces the error banner if the recent-signals feed rejects", async () => {
    apiState.signalsRejects = true;
    renderPage();
    await waitFor(() => {
      const err = screen.getByTestId("text-leaderboard-error");
      expect(err.textContent).toMatch(/signals down/);
    });
  });

  it("renders the early-access CTA links into the marketing landing", async () => {
    renderPage();
    const link = screen.getByTestId("link-leaderboard-early-access");
    expect(link.getAttribute("href")).toBe("/#early-access");
  });
});
