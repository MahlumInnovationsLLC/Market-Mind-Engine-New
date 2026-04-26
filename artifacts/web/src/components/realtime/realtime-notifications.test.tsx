import { describe, it, expect, afterEach, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import React from "react";

const { state, toastSpy } = vi.hoisted(() => ({
  state: {
    alerts: { data: undefined as unknown },
    pipelineHandler: null as ((ev: Record<string, unknown>) => void) | null,
  },
  toastSpy: vi.fn(),
}));

vi.mock("@workspace/api-client-react", () => ({
  useListModelHealthAlerts: () => state.alerts,
  getListModelHealthAlertsQueryKey: () => ["model-health-alerts"],
}));

vi.mock("@/hooks/use-toast", () => ({
  toast: toastSpy,
}));

vi.mock("@/components/ui/toast", () => ({
  ToastAction: ({ children }: { children: React.ReactNode }) =>
    React.createElement("span", null, children),
}));

vi.mock("./websocket-provider", () => ({
  usePipelineSubscription: (
    cb: (ev: Record<string, unknown>) => void,
  ) => {
    state.pipelineHandler = cb;
  },
}));

import { RealtimeNotifications } from "./realtime-notifications";

afterEach(() => {
  cleanup();
  state.alerts.data = undefined;
  state.pipelineHandler = null;
  toastSpy.mockReset();
});

describe("RealtimeNotifications", () => {
  it("renders nothing into the DOM (it's a side-effect bridge)", () => {
    const { container } = render(<RealtimeNotifications />);
    expect(container.innerHTML).toBe("");
  });

  it("does not blast toasts for the alerts present on first render (seed-only)", () => {
    state.alerts.data = {
      items: [
        { id: 1, severity: "critical", alert_type: "drift", vertical: "nba" },
      ],
    };
    render(<RealtimeNotifications />);
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it("toasts a fill notification for trade_executed pipeline frames", () => {
    state.alerts.data = { items: [] };
    render(<RealtimeNotifications />);
    act(() => {
      state.pipelineHandler?.({
        type: "trade_executed",
        trade_id: "T-1",
        ticker: "KX-NBA-XYZ",
        side: "yes",
        qty: 3,
        price: 0.55,
        ts: "2026-04-24T10:00:00Z",
      });
    });
    expect(toastSpy).toHaveBeenCalledTimes(1);
    expect(toastSpy.mock.calls[0]?.[0].title).toMatch(/FILL/);
  });

  it("dedupes a duplicate trade_executed frame with the same trade_id", () => {
    state.alerts.data = { items: [] };
    render(<RealtimeNotifications />);
    const frame = {
      type: "trade_executed",
      trade_id: "T-2",
      ticker: "KX",
      side: "no",
      qty: 1,
      price: 0.45,
      ts: "2026-04-24T11:00:00Z",
    };
    act(() => {
      state.pipelineHandler?.(frame);
      state.pipelineHandler?.(frame);
    });
    expect(toastSpy).toHaveBeenCalledTimes(1);
  });

  it("toasts a placement notification for candidate_complete with placement.placed=true", () => {
    state.alerts.data = { items: [] };
    render(<RealtimeNotifications />);
    act(() => {
      state.pipelineHandler?.({
        type: "candidate_complete",
        placement: {
          placed: true,
          trade_id: "P-9",
          ticker: "KX-PLACED",
          side: "yes",
          qty: 2,
          limit_price: 0.6,
        },
      });
    });
    expect(toastSpy).toHaveBeenCalledTimes(1);
    expect(toastSpy.mock.calls[0]?.[0].title).toMatch(/ORDER PLACED/);
  });

  it("ignores candidate_complete frames without a placement payload", () => {
    state.alerts.data = { items: [] };
    render(<RealtimeNotifications />);
    act(() => {
      state.pipelineHandler?.({ type: "candidate_complete" });
    });
    expect(toastSpy).not.toHaveBeenCalled();
  });
});
