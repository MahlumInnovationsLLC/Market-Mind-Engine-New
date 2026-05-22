import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import React from "react";

const { state } = vi.hoisted(() => ({
  state: {
    auth: { isSignedIn: false, getToken: vi.fn(async () => null) },
    overview: { data: undefined as unknown, isLoading: false },
    access: { data: undefined as unknown, isLoading: false },
    persona: { data: undefined as unknown },
    period: { period: "30d", periodLabel: "30D" },
    filter: {
      selected: [] as string[],
      matches: () => true,
      filterActive: false,
      hiddenActive: false,
    },
  },
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => state.auth,
}));

vi.mock("@workspace/api-client-react", () => ({
  useGetVerticalsOverview: () => state.overview,
  getGetVerticalsOverviewQueryKey: () => ["verticals-overview"],
  useGetMyAccess: () => state.access,
  getGetMyAccessQueryKey: () => ["my-access"],
  useGetEdgeTerminalStatus: () => ({ data: undefined }),
  getGetEdgeTerminalStatusQueryKey: () => ["edge-terminal-status"],
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => state.persona,
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(async () => ({ items: [] })),
}));

vi.mock("@/hooks/use-selected-verticals", () => ({
  useVerticalFilter: () => state.filter,
}));

vi.mock("../dashboard/period-context", () => ({
  usePeriod: () => state.period,
}));

vi.mock("./period-context", () => ({
  usePeriod: () => state.period,
}));

import VerticalCards from "./vertical-cards";

afterEach(() => {
  cleanup();
  state.overview.data = undefined;
  state.overview.isLoading = false;
  state.access.data = undefined;
  state.persona.data = undefined;
});

function renderCards() {
  const { hook } = memoryLocation({ path: "/" });
  return render(
    <Router hook={hook}>
      <VerticalCards />
    </Router>,
  );
}

describe("VerticalCards", () => {
  it("renders the loading skeleton while the verticals overview query is in flight", () => {
    state.overview.isLoading = true;
    const { container } = renderCards();
    expect(container.querySelector(".animate-pulse")).toBeTruthy();
  });

  it("renders one tile per vertical with its slug + P&L", () => {
    state.overview.data = {
      verticals: [
        {
          vertical: "nba",
          pnl: 12_300,
          win_rate: 0.6,
          enabled: true,
          pinned: true,
          hidden: false,
          halted: false,
          halt: null,
        },
        {
          vertical: "nfl",
          pnl: -450,
          win_rate: 0.45,
          enabled: false,
          pinned: false,
          hidden: false,
          halted: true,
          halt: { by_email: "ops@axiom.io", at: "2026-04-23T10:00:00Z", reason: "audit" },
        },
      ],
    };
    renderCards();
    expect(screen.getByTestId("card-vertical-overview-nba")).toBeTruthy();
    expect(screen.getByTestId("card-vertical-overview-nfl")).toBeTruthy();
    expect(screen.getByTestId("badge-vertical-halted-nfl")).toBeTruthy();
  });
});
