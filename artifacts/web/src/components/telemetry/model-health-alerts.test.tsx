import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    list: {
      data: undefined as unknown,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    },
    ack: { mutate: vi.fn() },
    ws: { status: "open" as "open" | "closed" },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  useListModelHealthAlerts: () => state.list,
  getListModelHealthAlertsQueryKey: () => ["model-health-alerts"],
  useAcknowledgeModelHealthAlert: () => state.ack,
}));

vi.mock("@/hooks/use-websocket", () => ({
  useWebSocket: () => state.ws,
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  formatErrorMessage: (e: unknown) =>
    e instanceof Error ? e.message : String(e),
}));

import ModelHealthAlerts from "./model-health-alerts";

afterEach(() => {
  cleanup();
  state.list.data = undefined;
  state.list.isLoading = false;
  state.list.isError = false;
  state.list.refetch.mockReset();
  state.ack.mutate.mockReset();
  state.ws.status = "open";
});

describe("ModelHealthAlerts", () => {
  it("renders the loading skeleton while the alerts query is in flight", () => {
    state.list.isLoading = true;
    const { container } = render(<ModelHealthAlerts />);
    expect(container.querySelector(".animate-pulse")).toBeTruthy();
  });

  it("renders the destructive error block when the alerts query fails", () => {
    state.list.isError = true;
    render(<ModelHealthAlerts />);
    expect(screen.getByText(/ERR_MODEL_HEALTH_FAIL/)).toBeTruthy();
  });

  it("renders the OK headline + empty-state when there are no open alerts", () => {
    state.list.data = { items: [] };
    render(<ModelHealthAlerts />);
    expect(screen.getByTestId("text-model-alert-count").textContent).toMatch(
      /OK/,
    );
    expect(screen.getByText(/NO_OPEN_ALERTS/)).toBeTruthy();
  });

  it("renders one row per non-acknowledged alert and the open count", () => {
    state.list.data = {
      items: [
        {
          id: 1,
          severity: "critical",
          alert_type: "drift",
          vertical: "nba",
          message: "Calibration regression",
          age_seconds: 60,
          acknowledged: false,
          snooze_until: null,
        },
        {
          id: 2,
          severity: "info",
          alert_type: "info",
          vertical: "nfl",
          message: "Backfill complete",
          age_seconds: 30,
          acknowledged: true,
          snooze_until: null,
        },
      ],
    };
    render(<ModelHealthAlerts />);
    const rows = screen.getAllByTestId("row-model-health-alert");
    expect(rows.length).toBe(1);
    expect(screen.getByTestId("text-model-alert-count").textContent).toMatch(
      /1 OPEN/,
    );
  });
});
