import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    list: { data: undefined as unknown, isLoading: false, isError: false, refetch: vi.fn() },
    decide: { mutate: vi.fn(), isPending: false },
    run: { mutate: vi.fn(), isPending: false },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  AutoResearchDecisionRequestDecision: { promote: "promote", reject: "reject" },
  getAdminAutoResearchListFeaturesQueryKey: () => ["autoresearch-list"],
  useAdminAutoResearchListFeatures: () => state.list,
  useAdminAutoResearchDecide: () => state.decide,
  useAdminAutoResearchRun: () => state.run,
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  formatErrorMessage: (e: unknown) =>
    e instanceof Error ? e.message : String(e),
}));

vi.mock("./autoresearch-throughput-chart", () => ({
  default: () => <div data-testid="throughput-chart-stub" />,
}));

import AutoResearchPanel from "./autoresearch-panel";

afterEach(() => {
  cleanup();
  state.list.data = undefined;
  state.list.isLoading = false;
  state.list.isError = false;
});

describe("AutoResearchPanel", () => {
  it("renders the admin-only forbidden card when isAdmin=false", () => {
    render(<AutoResearchPanel isAdmin={false} />);
    expect(screen.getByTestId("card-autoresearch-forbidden")).toBeTruthy();
    expect(screen.getByText("ADMIN_ONLY")).toBeTruthy();
  });

  it("renders the loading skeleton while features are loading for admin", () => {
    state.list.isLoading = true;
    render(<AutoResearchPanel isAdmin={true} />);
    expect(screen.getByTestId("skeleton-autoresearch")).toBeTruthy();
  });

  it("renders the ERR_AUTORESEARCH_LOAD block when the features query fails", () => {
    state.list.isError = true;
    render(<AutoResearchPanel isAdmin={true} />);
    expect(screen.getByText("ERR_AUTORESEARCH_LOAD")).toBeTruthy();
  });

  it("renders the NO_CANDIDATES_DISCOVERED empty-state when items is empty", () => {
    state.list.data = { items: [] };
    render(<AutoResearchPanel isAdmin={true} />);
    expect(screen.getByText("NO_CANDIDATES_DISCOVERED")).toBeTruthy();
  });

  it("renders the features table with one row per item when data is present", () => {
    state.list.data = {
      items: [
        {
          id: 1,
          name: "alpha-feature",
          vertical: "nba",
          status: "candidate",
          ic: 0.12,
          ic_p_value: 0.04,
          sharpe: 1.5,
          monotonicity: 0.9,
          n_obs: 1000,
          last_evaluated_at: "2026-04-24T10:00:00Z",
          shadow_pnl: 123,
        },
      ],
    };
    render(<AutoResearchPanel isAdmin={true} />);
    expect(screen.getByTestId("table-autoresearch")).toBeTruthy();
    expect(screen.getByText("alpha-feature")).toBeTruthy();
  });
});
