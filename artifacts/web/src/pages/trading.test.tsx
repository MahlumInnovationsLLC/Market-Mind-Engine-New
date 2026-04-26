import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";
import React from "react";

function stub(testId: string) {
  return () => React.createElement("div", { "data-testid": testId });
}

const { accessState } = vi.hoisted(() => ({
  accessState: { data: { is_admin: false } as { is_admin: boolean } | null },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetMyAccessQueryKey: () => ["get-my-access"],
  useGetMyAccess: () => ({ data: accessState.data }),
}));

vi.mock("@/hooks/use-focus-from-query", () => ({
  useFocusFromQuery: () => undefined,
}));

vi.mock("@/components/telemetry/live-execute-toggle", () => ({
  default: stub("stub-live-execute"),
}));
vi.mock("@/components/telemetry/emergency-stop", () => ({
  default: stub("stub-emergency-stop"),
}));
vi.mock("@/components/telemetry/recent-trades", () => ({
  default: stub("stub-recent-trades"),
}));
vi.mock("@/components/telemetry/trading-cycles", () => ({
  default: stub("stub-trading-cycles"),
}));
vi.mock("@/components/dashboard/active-positions", () => ({
  default: stub("stub-active-positions"),
}));
vi.mock("@/components/dashboard/trade-stats-panel", () => ({
  default: stub("stub-trade-stats"),
}));
vi.mock("@/components/dashboard/pnl-sparkline", () => ({
  default: stub("stub-pnl-sparkline"),
}));
vi.mock("@/components/dashboard/period-context", () => ({
  PeriodProvider: ({ children }: { children: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children),
  PeriodStrip: stub("stub-period-strip"),
}));

import TradingPage from "./trading";

afterEach(() => {
  cleanup();
  accessState.data = { is_admin: false };
});

function renderAt(path: string) {
  const hook = memoryLocation({ path, record: true });
  return {
    ...render(
      <Router hook={hook.hook}>
        <TradingPage />
      </Router>,
    ),
    history: hook.history,
  };
}

describe("TradingPage", () => {
  it("defaults to the live tab and renders the command center", () => {
    renderAt("/trading");
    expect(screen.getByTestId("page-trading")).toBeTruthy();
    expect(
      screen.getByRole("heading", { level: 1, name: /trading/i }),
    ).toBeTruthy();
    expect(screen.getByTestId("panel-trading-live")).toBeTruthy();
    expect(screen.queryByTestId("panel-trading-feed")).toBeNull();
    expect(screen.getByTestId("stub-live-execute")).toBeTruthy();
    // Default subtitle reflects the live tab copy.
    expect(
      screen.getByText(/Command center.*positions.*cycles/i),
    ).toBeTruthy();
  });

  it("hides the emergency-stop control from non-admin operators", () => {
    accessState.data = { is_admin: false };
    renderAt("/trading");
    expect(screen.getByTestId("stub-live-execute")).toBeTruthy();
    expect(screen.queryByTestId("stub-emergency-stop")).toBeNull();
  });

  it("renders the emergency-stop control for admin operators", () => {
    accessState.data = { is_admin: true };
    renderAt("/trading");
    expect(screen.getByTestId("stub-emergency-stop")).toBeTruthy();
  });

  it("switches to the feed tab when the feed tab is clicked", () => {
    renderAt("/trading");
    fireEvent.click(screen.getByTestId("tab-trading-feed"));
    expect(screen.getByTestId("panel-trading-feed")).toBeTruthy();
    expect(screen.queryByTestId("panel-trading-live")).toBeNull();
    expect(
      screen.getByText(/Chronological execution stream/i),
    ).toBeTruthy();
  });

  it("respects ?tab=feed in the URL on first render", () => {
    renderAt("/trading?tab=feed");
    expect(screen.getByTestId("panel-trading-feed")).toBeTruthy();
    expect(screen.queryByTestId("panel-trading-live")).toBeNull();
  });
});
