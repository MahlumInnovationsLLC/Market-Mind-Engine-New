import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state, mutates } = vi.hoisted(() => ({
  state: { data: undefined as unknown, isLoading: false, error: null as unknown },
  mutates: { snooze: vi.fn(async () => ({})), fire: vi.fn(async () => ({})) },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetDashboardTopEdgesQueryKey: () => ["top-edges"],
  useGetDashboardTopEdges: () => state,
  useSnoozeDashboardCandidate: () => ({ mutateAsync: mutates.snooze }),
  useFireDashboardCandidate: () => ({ mutateAsync: mutates.fire }),
}));
vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: () => undefined }),
}));

import TopEdgesTile from "./top-edges-tile";

afterEach(() => {
  cleanup();
  state.data = undefined;
  state.isLoading = false;
  state.error = null;
  mutates.snooze.mockClear();
  mutates.fire.mockClear();
});

describe("TopEdgesTile", () => {
  it("renders empty state when items=[]", () => {
    state.data = {
      limit: 25,
      generated_at: "2026-05-22T12:00:00Z",
      items: [],
      halted_verticals: [],
      error: null,
    };
    render(<TopEdgesTile />);
    expect(screen.getByTestId("top-edges-empty")).toBeTruthy();
  });

  it("renders rows ranked by edge with halt greyout", () => {
    state.data = {
      limit: 25,
      generated_at: "2026-05-22T12:00:00Z",
      items: [
        {
          prediction_id: 1,
          matchup: "LAL @ BOS",
          league: "NBA",
          vertical: "sports",
          vertical_halted: false,
          model_prob: 0.62,
          market_price: 0.53,
          edge_pct: 9.0,
          confidence: 0.7,
          planned_stake: null,
          kalshi_market_id: "KX-1",
        },
        {
          prediction_id: 2,
          matchup: "BTC > 70k",
          league: "BTC",
          vertical: "crypto",
          vertical_halted: true,
          model_prob: 0.55,
          market_price: 0.48,
          edge_pct: 7.0,
          confidence: 0.6,
          planned_stake: null,
          kalshi_market_id: "KX-2",
        },
      ],
      halted_verticals: ["crypto"],
      error: null,
    };
    render(<TopEdgesTile />);
    const halted = screen.getByTestId("top-edge-row-2");
    expect(halted.getAttribute("data-halted")).toBe("true");
    const fireBtn = screen.getByTestId("fire-2") as HTMLButtonElement;
    const snoozeBtn = screen.getByTestId("snooze-2") as HTMLButtonElement;
    expect(fireBtn.disabled).toBe(true);
    expect(snoozeBtn.disabled).toBe(true);
    expect(screen.getByText("LAL @ BOS")).toBeTruthy();
    expect(screen.getByText("BTC > 70k")).toBeTruthy();
  });

  it("surfaces backend error string", () => {
    state.data = {
      limit: 25,
      generated_at: "2026-05-22T12:00:00Z",
      items: [],
      halted_verticals: [],
      error: "ProgrammingError: relation predictions does not exist",
    };
    render(<TopEdgesTile />);
    expect(screen.getByRole("alert").textContent).toMatch(/relation predictions/);
  });
});
