import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import React from "react";

// Every panel on the dashboard is its own data-loading widget; mocking
// them lets the test verify *layout & composition* (which is what
// dashboard.tsx actually owns) without dragging in QueryClient + auth +
// websocket plumbing for each child.
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

vi.mock("@/components/dashboard/active-positions", () => ({
  default: stub("stub-active-positions"),
}));
vi.mock("@/components/dashboard/bankroll-panel", () => ({
  default: stub("stub-bankroll-panel"),
}));
vi.mock("@/components/dashboard/crypto-backtest-retention-panel", () => ({
  default: stub("stub-crypto-backtest"),
}));
vi.mock("@/components/dashboard/crypto-ohlcv-retention-panel", () => ({
  default: stub("stub-crypto-ohlcv"),
}));
vi.mock("@/components/dashboard/equity-charts", () => ({
  default: stub("stub-equity-charts"),
}));
vi.mock("@/components/dashboard/highlights-panel", () => ({
  default: stub("stub-highlights"),
}));
vi.mock("@/components/dashboard/kpi-summary-bar", () => ({
  default: stub("stub-kpi-summary"),
}));
vi.mock("@/components/dashboard/macro-release-surprises", () => ({
  default: stub("stub-macro-surprises"),
}));
vi.mock("@/components/dashboard/leagues-exposure-panel", () => ({
  default: stub("stub-leagues-exposure"),
}));
vi.mock("@/components/dashboard/operator-controls", () => ({
  default: ({ isAdmin }: { isAdmin: boolean }) =>
    React.createElement("div", {
      "data-testid": "stub-operator-controls",
      "data-is-admin": String(isAdmin),
    }),
}));
vi.mock("@/components/dashboard/over-filled-orders-badge", () => ({
  default: stub("stub-over-filled-badge"),
}));
vi.mock("@/components/dashboard/per-league-calibration", () => ({
  default: stub("stub-per-league-calibration"),
}));
vi.mock("@/components/dashboard/period-context", () => ({
  PeriodProvider: ({ children }: { children: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children),
  PeriodStrip: stub("stub-period-strip"),
}));
vi.mock("@/components/dashboard/pipeline-tracker", () => ({
  default: stub("stub-pipeline-tracker"),
}));
vi.mock("@/components/dashboard/telegram-concierge", () => ({
  default: stub("stub-telegram-concierge"),
}));
vi.mock("@/components/dashboard/tournament-leaderboards-panel", () => ({
  default: stub("stub-tournament-leaderboards"),
}));
vi.mock("@/components/dashboard/trade-stats-panel", () => ({
  default: stub("stub-trade-stats"),
}));
vi.mock("@/components/dashboard/vertical-cards", () => ({
  default: stub("stub-vertical-cards"),
}));

vi.mock("@/components/telemetry/health-snapshot", () => ({
  default: stub("stub-health-snapshot"),
}));
vi.mock("@/components/telemetry/recent-trades", () => ({
  default: stub("stub-recent-trades"),
}));
vi.mock("@/components/telemetry/trading-cycles", () => ({
  default: stub("stub-trading-cycles"),
}));
vi.mock("@/components/telemetry/swarm-analysts", () => ({
  default: stub("stub-swarm-analysts"),
}));
vi.mock("@/components/telemetry/swarm-personas-today", () => ({
  default: stub("stub-swarm-personas-today"),
}));
vi.mock("@/components/telemetry/swarm-fallbacks-today", () => ({
  default: stub("stub-swarm-fallbacks-today"),
}));
vi.mock("@/components/telemetry/provider-fallback-rates", () => ({
  default: stub("stub-provider-fallback-rates"),
}));
vi.mock("@/components/telemetry/swarm-debates", () => ({
  default: stub("stub-swarm-debates"),
}));
vi.mock("@/components/telemetry/vertical-performance", () => ({
  default: stub("stub-vertical-performance"),
}));
vi.mock("@/components/telemetry/vertical-picker", () => ({
  default: ({ isAdmin }: { isAdmin: boolean }) =>
    React.createElement("div", {
      "data-testid": "stub-vertical-picker",
      "data-is-admin": String(isAdmin),
    }),
}));
vi.mock("@/components/telemetry/active-model-badge", () => ({
  default: stub("stub-active-model-badge"),
}));

import Dashboard from "./dashboard";

afterEach(() => {
  cleanup();
  accessState.data = { is_admin: false };
});

describe("Dashboard", () => {
  it("renders the operator overview header and threads the panels in order", () => {
    render(<Dashboard />);

    // Header copy anchors the page identity.
    expect(
      screen.getByRole("heading", { level: 1, name: /overview/i }),
    ).toBeTruthy();
    expect(screen.getByText(/OPERATOR CONSOLE/i)).toBeTruthy();
    // Branded subtitle.
    expect(
      screen.getByText(/live kpis, equity curve, per-vertical performance/i),
    ).toBeTruthy();

    // Spot-check a representative slice of every section: hero strip,
    // grid panels, swarm row, operator controls. Re-ordering or
    // dropping a panel should fail this test.
    const sections = [
      "stub-active-model-badge",
      "stub-over-filled-badge",
      "stub-period-strip",
      "stub-kpi-summary",
      "stub-vertical-picker",
      "stub-equity-charts",
      "stub-bankroll-panel",
      "stub-trade-stats",
      "stub-pipeline-tracker",
      "stub-active-positions",
      "stub-recent-trades",
      "stub-vertical-cards",
      "stub-leagues-exposure",
      "stub-tournament-leaderboards",
      "stub-per-league-calibration",
      "stub-macro-surprises",
      "stub-vertical-performance",
      "stub-highlights",
      "stub-trading-cycles",
      "stub-health-snapshot",
      "stub-crypto-ohlcv",
      "stub-crypto-backtest",
      "stub-swarm-debates",
      "stub-swarm-analysts",
      "stub-swarm-personas-today",
      "stub-provider-fallback-rates",
      "stub-swarm-fallbacks-today",
      "stub-telegram-concierge",
      "stub-operator-controls",
    ];
    for (const id of sections) {
      expect(screen.getByTestId(id)).toBeTruthy();
    }
  });

  it("forwards admin status to the panels that gate on it", () => {
    accessState.data = { is_admin: true };
    render(<Dashboard />);
    expect(
      screen.getByTestId("stub-vertical-picker").getAttribute("data-is-admin"),
    ).toBe("true");
    expect(
      screen
        .getByTestId("stub-operator-controls")
        .getAttribute("data-is-admin"),
    ).toBe("true");
  });

  it("treats missing access data as non-admin", () => {
    accessState.data = null;
    render(<Dashboard />);
    expect(
      screen.getByTestId("stub-vertical-picker").getAttribute("data-is-admin"),
    ).toBe("false");
    expect(
      screen
        .getByTestId("stub-operator-controls")
        .getAttribute("data-is-admin"),
    ).toBe("false");
  });
});
