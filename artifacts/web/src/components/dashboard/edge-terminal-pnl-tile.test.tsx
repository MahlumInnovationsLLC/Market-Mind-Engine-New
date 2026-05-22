import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { pnlState, statusState } = vi.hoisted(() => ({
  pnlState: { data: undefined as unknown, isLoading: false, error: null as unknown },
  statusState: { data: undefined as unknown },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetDashboardEdgeTerminalPnlQueryKey: () => ["edge-terminal-pnl"],
  getGetEdgeTerminalStatusQueryKey: () => ["edge-terminal-status"],
  useGetDashboardEdgeTerminalPnl: () => pnlState,
  useGetEdgeTerminalStatus: () => statusState,
}));

import EdgeTerminalPnlTile from "./edge-terminal-pnl-tile";

afterEach(() => {
  cleanup();
  pnlState.data = undefined;
  pnlState.isLoading = false;
  pnlState.error = null;
  statusState.data = undefined;
});

describe("EdgeTerminalPnlTile", () => {
  it("renders cumulative P&L and hit rate in green when positive", () => {
    pnlState.data = {
      generated_at: "2026-05-22T12:00:00Z",
      since_date: "2026-05-21",
      n_trades: 8,
      n_settled: 5,
      cumulative_pnl: 42.5,
      win_rate: 0.6,
    };
    render(<EdgeTerminalPnlTile />);
    const cum = screen.getByTestId("edge-terminal-pnl-cumulative");
    expect(cum.textContent).toContain("+$42.50");
    expect(cum.className).toMatch(/emerald/);
    expect(screen.getByText(/60\.0%/)).toBeTruthy();
    expect(screen.getByText(/5 settled · 8 placed/)).toBeTruthy();
  });

  it("uses rose color for negative P&L", () => {
    pnlState.data = {
      generated_at: "2026-05-22T12:00:00Z",
      since_date: "2026-05-21",
      n_trades: 2, n_settled: 2, cumulative_pnl: -12.34, win_rate: 0,
    };
    render(<EdgeTerminalPnlTile />);
    const cum = screen.getByTestId("edge-terminal-pnl-cumulative");
    expect(cum.textContent).toContain("-$12.34");
    expect(cum.className).toMatch(/rose/);
  });

  it("renders stale badge when poller is stale", () => {
    pnlState.data = {
      generated_at: "2026-05-22T12:00:00Z",
      since_date: "2026-05-21",
      n_trades: 0, n_settled: 0, cumulative_pnl: null, win_rate: null,
    };
    statusState.data = { stale: true, running: true, age_seconds: 240 };
    render(<EdgeTerminalPnlTile />);
    const badge = screen.getByTestId("edge-terminal-stale-badge");
    expect(badge.textContent).toContain("Stale poller");
    expect(badge.textContent).toContain("240s");
  });

  it("renders error state when the response carries an error string", () => {
    pnlState.data = {
      generated_at: "x", since_date: "2026-05-21",
      n_trades: 0, n_settled: 0, cumulative_pnl: null, win_rate: null,
      error: "db down",
    };
    render(<EdgeTerminalPnlTile />);
    expect(screen.getByTestId("edge-terminal-pnl-error").textContent)
      .toContain("db down");
  });

  it("renders em-dash for null P&L", () => {
    pnlState.data = {
      generated_at: "x", since_date: "2026-05-21",
      n_trades: 0, n_settled: 0, cumulative_pnl: null, win_rate: null,
    };
    render(<EdgeTerminalPnlTile />);
    expect(screen.getByTestId("edge-terminal-pnl-cumulative").textContent)
      .toBe("—");
  });
});
