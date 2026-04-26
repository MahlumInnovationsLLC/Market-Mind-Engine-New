import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: { rejects: { data: undefined as unknown, isLoading: false, isError: false } },
}));

vi.mock("@workspace/api-client-react", () => ({
  useGetSlippageGuardRejects: () => state.rejects,
  getGetSlippageGuardRejectsQueryKey: () => ["slippage-guard-rejects"],
}));

import SlippageGuardRejectsPanel from "./slippage-guard-rejects";

afterEach(() => {
  cleanup();
  state.rejects.data = undefined;
  state.rejects.isLoading = false;
  state.rejects.isError = false;
});

describe("SlippageGuardRejectsPanel", () => {
  it("renders the loading state with slippage-guard-panel-loading testid", () => {
    state.rejects.isLoading = true;
    render(<SlippageGuardRejectsPanel cycleId={1} />);
    expect(screen.getByTestId("slippage-guard-panel-loading")).toBeTruthy();
  });

  it("renders the failure copy when the query errors out", () => {
    state.rejects.isError = true;
    render(<SlippageGuardRejectsPanel cycleId={42} />);
    expect(
      screen.getByText("Failed to load slippage-guard rejects for cycle 42."),
    ).toBeTruthy();
  });

  it("renders the empty-state copy when no rejects are returned", () => {
    state.rejects.data = { items: [] };
    render(<SlippageGuardRejectsPanel cycleId={1} />);
    expect(
      screen.getByText("No slippage-guard rejects recorded inside this cycle."),
    ).toBeTruthy();
  });

  it("renders one row per item with the slippage-guard-row-{id} testid", () => {
    state.rejects.data = {
      items: [
        {
          id: 7,
          created_at: "2026-04-24T12:00:00Z",
          market_ticker: "KX-NBA-LAL",
          side: "yes",
          slippage_cents: 5,
          edge_erased_pct: 9.1,
        },
      ],
    };
    render(<SlippageGuardRejectsPanel cycleId={1} />);
    expect(screen.getByTestId("slippage-guard-row-7")).toBeTruthy();
    expect(screen.getByText("KX-NBA-LAL")).toBeTruthy();
  });
});
