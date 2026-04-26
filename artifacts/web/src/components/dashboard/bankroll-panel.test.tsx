import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    data: undefined as unknown,
    isLoading: false,
    isError: false,
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetRecentBankrollEventsQueryKey: () => ["bankroll-recent"],
  useGetRecentBankrollEvents: () => state,
}));

import BankrollPanel from "./bankroll-panel";

afterEach(() => {
  cleanup();
  state.data = undefined;
  state.isLoading = false;
  state.isError = false;
});

describe("BankrollPanel", () => {
  it("renders the skeleton loader while the bankroll feed is in flight", () => {
    state.isLoading = true;
    render(<BankrollPanel />);
    expect(screen.getByTestId("skeleton-bankroll-panel")).toBeTruthy();
  });

  it("renders the destructive error block when the bankroll feed errors", () => {
    state.isError = true;
    render(<BankrollPanel />);
    expect(screen.getByTestId("bankroll-panel-error").textContent).toMatch(
      /BANKROLL_UNAVAILABLE/,
    );
  });

  it("renders current balance + sync badge when data lands but no events fall in 24h", () => {
    state.data = {
      items: [],
      sync: {
        running: true,
        last_tick_at: Math.floor(Date.now() / 1000),
        interval_seconds: 60,
      },
    };
    render(<BankrollPanel />);
    // No events → no reason breakdown is rendered, but the headline
    // balance scaffolding stays. Verify the current-balance node.
    expect(screen.getByTestId("bankroll-current-balance")).toBeTruthy();
  });

  it("renders one row per reason in the 24h breakdown when events are present", () => {
    const nowIso = new Date().toISOString();
    state.data = {
      items: [
        {
          id: 1,
          reason: "fill",
          delta_cents: 1500,
          balance_cents: 250_000,
          occurred_at: nowIso,
        },
        {
          id: 2,
          reason: "deposit",
          delta_cents: 5000,
          balance_cents: 255_000,
          occurred_at: nowIso,
        },
        {
          id: 3,
          reason: "withdrawal",
          delta_cents: -2000,
          balance_cents: 253_000,
          occurred_at: nowIso,
        },
      ],
      sync: {
        running: true,
        last_tick_at: Math.floor(Date.now() / 1000),
        interval_seconds: 60,
      },
    };
    render(<BankrollPanel />);
    // Each reason gets its own data-testid so we can assert presence
    // without coupling to layout/copy.
    expect(screen.getByTestId("bankroll-reason-fill")).toBeTruthy();
    expect(screen.getByTestId("bankroll-reason-deposit")).toBeTruthy();
    expect(screen.getByTestId("bankroll-reason-withdrawal")).toBeTruthy();
    expect(screen.getByTestId("bankroll-reason-breakdown")).toBeTruthy();
  });
});
