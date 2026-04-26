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

vi.mock("@/components/telemetry/swarm-analysts", () => ({
  default: stub("stub-swarm-analysts"),
}));
vi.mock("@/components/telemetry/swarm-debates", () => ({
  default: stub("stub-swarm-debates"),
}));
vi.mock("@/components/telemetry/model-inventory", () => ({
  default: stub("stub-model-inventory"),
}));
vi.mock("@/components/telemetry/model-health-alerts", () => ({
  default: stub("stub-model-health-alerts"),
}));
vi.mock("@/components/telemetry/recent-retrains", () => ({
  default: stub("stub-recent-retrains"),
}));
vi.mock("@/components/telemetry/active-model-badge", () => ({
  default: stub("stub-active-model-badge"),
}));
vi.mock("@/components/intelligence/autoresearch-panel", () => ({
  default: ({ isAdmin }: { isAdmin: boolean }) =>
    React.createElement("div", {
      "data-testid": "stub-autoresearch",
      "data-is-admin": String(isAdmin),
    }),
}));
vi.mock("@/components/intelligence/calibration-panel", () => ({
  default: stub("stub-calibration"),
}));
vi.mock("@/components/intelligence/edge-panel", () => ({
  default: stub("stub-edge"),
}));
vi.mock("@/components/intelligence/active-predictor-selector", () => ({
  default: ({ isAdmin }: { isAdmin: boolean }) =>
    React.createElement("div", {
      "data-testid": "stub-active-predictor",
      "data-is-admin": String(isAdmin),
    }),
}));

import IntelligencePage from "./intelligence";

afterEach(() => {
  cleanup();
  accessState.data = { is_admin: false };
});

function renderAt(path: string) {
  const hook = memoryLocation({ path, record: true });
  return {
    ...render(
      <Router hook={hook.hook}>
        <IntelligencePage />
      </Router>,
    ),
    history: hook.history,
  };
}

describe("IntelligencePage", () => {
  it("renders the page header with the default 'swarm' subtitle", () => {
    renderAt("/intelligence");
    expect(screen.getByTestId("page-intelligence")).toBeTruthy();
    expect(
      screen.getByRole("heading", { level: 1, name: /swarm.+models.+research/i }),
    ).toBeTruthy();
    // Default tab is "swarm" → subtitle reflects the swarm copy.
    expect(
      screen.getByText(/6-persona analysts \+ consensus debates/i),
    ).toBeTruthy();
  });

  it("defaults to the swarm tab and renders swarm panels", () => {
    renderAt("/intelligence");
    expect(screen.getByTestId("stub-swarm-debates")).toBeTruthy();
    expect(screen.getByTestId("stub-swarm-analysts")).toBeTruthy();
    // Models / autoresearch / calibration / edge panels stay hidden.
    expect(screen.queryByTestId("stub-model-inventory")).toBeNull();
    expect(screen.queryByTestId("panel-intelligence-autoresearch")).toBeNull();
  });

  it("switches to the models tab when the tab button is clicked", () => {
    renderAt("/intelligence");
    fireEvent.click(screen.getByTestId("tab-intelligence-models"));
    expect(screen.getByTestId("stub-model-inventory")).toBeTruthy();
    expect(screen.getByTestId("stub-model-health-alerts")).toBeTruthy();
    expect(screen.getByTestId("stub-recent-retrains")).toBeTruthy();
    // Subtitle updates with the active tab.
    expect(
      screen.getByText(/Model inventory.*active predictor.*health alerts/i),
    ).toBeTruthy();
  });

  it("renders autoresearch / calibration / edge panels when their tabs are picked up from the URL", () => {
    renderAt("/intelligence?tab=autoresearch");
    expect(screen.getByTestId("panel-intelligence-autoresearch")).toBeTruthy();

    cleanup();
    renderAt("/intelligence?tab=calibration");
    expect(screen.getByTestId("panel-intelligence-calibration")).toBeTruthy();

    cleanup();
    renderAt("/intelligence?tab=edge");
    expect(screen.getByTestId("panel-intelligence-edge")).toBeTruthy();
  });

  it("forwards admin status to the autoresearch panel when the user is admin", () => {
    accessState.data = { is_admin: true };
    renderAt("/intelligence?tab=autoresearch");
    expect(
      screen.getByTestId("stub-autoresearch").getAttribute("data-is-admin"),
    ).toBe("true");
  });
});
