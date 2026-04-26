import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import React from "react";

const { state } = vi.hoisted(() => ({
  state: {
    access: { data: { is_admin: false } as { is_admin: boolean } | null },
    overview: {
      data: undefined as unknown,
      isLoading: false,
      error: undefined as unknown,
    },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetMyAccessQueryKey: () => ["my-access"],
  getGetVerticalsOverviewQueryKey: () => ["vertical-overview"],
  useGetMyAccess: () => state.access,
  useGetVerticalsOverview: () => state.overview,
}));

vi.mock("@/components/intelligence/vertical-admin-controls", () => ({
  default: () =>
    React.createElement("div", { "data-testid": "stub-vertical-admin" }),
}));

import VerticalsPage from "./verticals";

afterEach(() => {
  cleanup();
  state.access.data = { is_admin: false };
  state.overview.data = undefined;
  state.overview.isLoading = false;
  state.overview.error = undefined;
});

function renderPage() {
  const hook = memoryLocation({ path: "/verticals", record: true });
  return render(
    <Router hook={hook.hook}>
      <VerticalsPage />
    </Router>,
  );
}

describe("VerticalsPage", () => {
  it("renders the skeleton placeholders while the overview is loading", () => {
    state.overview.isLoading = true;
    renderPage();
    expect(screen.getByTestId("page-verticals")).toBeTruthy();
    expect(screen.getByTestId("skeleton-verticals")).toBeTruthy();
  });

  it("surfaces a hard error sentinel when the overview query fails", () => {
    state.overview.error = new Error("boom");
    renderPage();
    expect(screen.getByTestId("text-verticals-error").textContent).toMatch(
      /ERR_OVERVIEW_LOAD/,
    );
  });

  it("renders one card per visible vertical and hides admin-only entries from non-admins", () => {
    state.overview.data = {
      verticals: [
        {
          vertical: "nba",
          pnl: 123,
          trades: 5,
          win_rate: 0.6,
          brier: 0.18,
          edge_bps: 25,
          open_positions: 2,
          hidden: false,
          pinned: true,
          description: "Basketball",
        },
        {
          vertical: "secret",
          pnl: -50,
          trades: 1,
          win_rate: null,
          brier: null,
          edge_bps: null,
          open_positions: 0,
          hidden: true,
          pinned: false,
          description: null,
        },
      ],
    };
    renderPage();
    expect(screen.getByTestId("card-vertical-overview-nba")).toBeTruthy();
    // Hidden vertical is filtered out for non-admins.
    expect(
      screen.queryByTestId("card-vertical-overview-secret"),
    ).toBeNull();
  });

  it("shows hidden cards (and admin controls) when the operator is admin", () => {
    state.access.data = { is_admin: true };
    state.overview.data = {
      verticals: [
        {
          vertical: "secret",
          pnl: 0,
          trades: 0,
          win_rate: null,
          brier: null,
          edge_bps: null,
          open_positions: 0,
          hidden: true,
          pinned: false,
          description: null,
        },
      ],
    };
    renderPage();
    expect(screen.getByTestId("card-vertical-overview-secret")).toBeTruthy();
    // VerticalAdminControls is mounted only for admins.
    expect(screen.getByTestId("stub-vertical-admin")).toBeTruthy();
  });

  it("renders the empty-state when no verticals are registered", () => {
    state.overview.data = { verticals: [] };
    renderPage();
    expect(screen.getByText(/NO_VERTICALS_REGISTERED/i)).toBeTruthy();
  });
});
