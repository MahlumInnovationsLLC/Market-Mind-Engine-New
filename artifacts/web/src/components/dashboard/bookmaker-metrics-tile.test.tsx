import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: { data: undefined as unknown, isLoading: false, error: null as unknown },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetDashboardBookmakerMetricsQueryKey: () => ["bm"],
  useGetDashboardBookmakerMetrics: () => state,
}));

import BookmakerMetricsTile from "./bookmaker-metrics-tile";

afterEach(() => {
  cleanup();
  state.data = undefined;
  state.isLoading = false;
  state.error = null;
});

const baseData = {
  window: "7d",
  generated_at: "2026-05-22T12:00:00Z",
  profit_factor: 1.85,
  equivalent_unit_pnl: 12.34,
  unit_dollars: 100,
  win_rate: 0.62,
  n_settled: 42,
  top_confidence_today: 0.78,
  highest_edge_today: 9.12,
  picks_today: {
    total: 17,
    by_vertical: [
      { vertical: "sports", n: 10 },
      { vertical: "crypto", n: 7 },
    ],
  },
  error: null,
};

describe("BookmakerMetricsTile", () => {
  it("renders all six tiles when data lands", () => {
    state.data = baseData;
    render(<BookmakerMetricsTile />);
    expect(screen.getByTestId("bm-profit-factor").textContent).toMatch(/1\.85/);
    expect(screen.getByTestId("bm-unit-pnl").textContent).toMatch(/\+12\.34u/);
    expect(screen.getByTestId("bm-hit-rate").textContent).toMatch(/62\.0%/);
    expect(screen.getByTestId("bm-top-confidence").textContent).toMatch(/78\.0%/);
    expect(screen.getByTestId("bm-highest-edge").textContent).toMatch(/9\.12%/);
    expect(screen.getByTestId("bm-picks-today").textContent).toMatch(/17/);
  });

  it("switches window via tab", () => {
    state.data = baseData;
    render(<BookmakerMetricsTile />);
    const ytd = screen.getByTestId("bm-window-ytd");
    fireEvent.click(ytd);
    expect(ytd.getAttribute("aria-selected")).toBe("true");
  });

  it("renders dashes when numbers are null", () => {
    state.data = {
      ...baseData,
      profit_factor: null,
      equivalent_unit_pnl: null,
      win_rate: null,
      top_confidence_today: null,
      highest_edge_today: null,
      n_settled: 0,
      picks_today: { total: 0, by_vertical: [] },
    };
    render(<BookmakerMetricsTile />);
    expect(screen.getByTestId("bm-profit-factor").textContent).toMatch(/—/);
    expect(screen.getByTestId("bm-hit-rate").textContent).toMatch(/—/);
  });
});
