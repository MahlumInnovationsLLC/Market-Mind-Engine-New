import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    live: { data: undefined as unknown },
    stop: { mutate: vi.fn(), isPending: false },
    rearm: { mutate: vi.fn(), isPending: false },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetLiveExecuteStateQueryKey: () => ["live-execute-state"],
  useAdminEmergencyStop: ({ mutation }: { mutation: { onSuccess?: (d: unknown) => void; onError?: (e: unknown) => void } }) => {
    state.stop.mutate = vi.fn(() => mutation?.onSuccess?.({ canceled_orders: 3 }));
    return state.stop;
  },
  useAdminRearmTrading: ({ mutation }: { mutation: { onSuccess?: (d: unknown) => void; onError?: (e: unknown) => void } }) => {
    state.rearm.mutate = vi.fn(() => mutation?.onSuccess?.({ trading_mode: "live" }));
    return state.rearm;
  },
  useGetLiveExecuteState: () => state.live,
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  formatErrorMessage: (e: unknown) =>
    e instanceof Error ? e.message : String(e),
}));

import EmergencyStop from "./emergency-stop";

afterEach(() => {
  cleanup();
  state.live.data = undefined;
});

describe("EmergencyStop", () => {
  it("renders the arm button with KILL ALL state when live-execute is normal", () => {
    state.live.data = { execute_live: true, effective_mode: "live" };
    render(<EmergencyStop />);
    expect(screen.getByTestId("button-emergency-stop-arm")).toBeTruthy();
    expect(screen.getByTestId("emergency-stop-state").textContent).toBe("KILL ALL");
  });

  it("renders the re-arm panel with HALTED state when the system is halted", () => {
    state.live.data = { execute_live: false, effective_mode: "halt" };
    render(<EmergencyStop />);
    expect(screen.getByTestId("emergency-stop-rearm")).toBeTruthy();
    expect(screen.getByTestId("button-rearm-paper")).toBeTruthy();
    expect(screen.getByTestId("button-rearm-live")).toBeTruthy();
    expect(screen.getByTestId("emergency-stop-state").textContent).toBe("HALTED");
  });
});
