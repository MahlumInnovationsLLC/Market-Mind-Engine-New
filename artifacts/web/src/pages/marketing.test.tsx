import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { state, apiFetchMock } = vi.hoisted(() => ({
  state: { hasSession: false },
  apiFetchMock: vi.fn(async (path: string) => {
    if (path.includes("/api/public/equity-curve")) {
      return { days: 7, points: [] };
    }
    if (path.includes("/api/public/stats")) {
      return {
        realized_pnl: 1234.56,
        total_trades: 42,
        win_rate: 0.61,
        last_trade_at: "2026-04-24T10:00:00Z",
        trading_cycles: 100,
        swarm_debates: 250,
      };
    }
    if (path.includes("/api/public/pricing")) {
      return { tiers: [] };
    }
    if (path.includes("/api/public/live-signals")) {
      return { items: [] };
    }
    return null;
  }),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: apiFetchMock,
  formatErrorMessage: (e: unknown) =>
    e instanceof Error ? e.message : String(e),
}));

vi.mock("@/lib/clerk-session", () => ({
  hasClerkSession: () => state.hasSession,
}));

vi.mock("@/components/public/equity-chart", () => ({
  default: () => <div data-testid="equity-chart-stub" />,
}));

vi.mock("@/components/public/public-nav", () => ({
  PublicNav: () => <nav data-testid="public-nav" />,
  PublicFooter: () => <footer data-testid="public-footer" />,
}));

import MarketingPage from "./marketing";

afterEach(() => {
  cleanup();
  state.hasSession = false;
});

describe("MarketingPage", () => {
  it("renders the public marketing chrome (nav + footer) for guests", () => {
    const { hook } = memoryLocation({ path: "/" });
    render(
      <Router hook={hook}>
        <MarketingPage />
      </Router>,
    );
    expect(screen.getByTestId("public-nav")).toBeTruthy();
    expect(screen.getByTestId("public-footer")).toBeTruthy();
  });
});
