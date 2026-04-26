import { describe, it, expect, afterEach, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

const { wsState } = vi.hoisted(() => ({
  wsState: {
    onMessage: null as ((ev: Record<string, unknown>) => void) | null,
    status: "open" as "open" | "closed",
  },
}));

vi.mock("@/hooks/use-websocket", () => ({
  useWebSocket: ({
    onMessage,
  }: {
    onMessage: (ev: Record<string, unknown>) => void;
  }) => {
    wsState.onMessage = onMessage;
    return { status: wsState.status };
  },
}));

import RecentRetrains from "./recent-retrains";

afterEach(() => {
  cleanup();
  wsState.onMessage = null;
  wsState.status = "open";
});

describe("RecentRetrains", () => {
  it("renders the NO_RETRAINS_OBSERVED empty-state with no frames received", () => {
    render(<RecentRetrains />);
    expect(screen.getByTestId("card-recent-retrains")).toBeTruthy();
    expect(screen.getByText("NO_RETRAINS_OBSERVED")).toBeTruthy();
  });

  it("appends a row with PROMOTED status for a successful retrain_complete frame", () => {
    render(<RecentRetrains />);
    act(() => {
      wsState.onMessage?.({
        type: "retrain_complete",
        vertical: "nba",
        triggered_by: "alert",
        rows_consumed: 1234,
        brier_before: 0.2,
        brier_after: 0.18,
        rolled_back: false,
        ok: true,
        snapshot_id: 42,
      });
    });
    const rows = screen.getAllByTestId("row-retrain");
    expect(rows.length).toBe(1);
    expect(screen.getByTestId("badge-retrain-trigger").textContent).toBe(
      "DRIFT ALERT",
    );
    expect(screen.getByText("PROMOTED")).toBeTruthy();
  });

  it("renders ROLLED BACK status when retrain_complete carries rolled_back=true", () => {
    render(<RecentRetrains />);
    act(() => {
      wsState.onMessage?.({
        type: "retrain_complete",
        vertical: "nfl",
        triggered_by: "batch",
        rows_consumed: 500,
        brier_before: 0.22,
        brier_after: 0.26,
        rolled_back: true,
        ok: false,
        snapshot_id: 43,
      });
    });
    expect(screen.getByText("ROLLED BACK")).toBeTruthy();
    expect(screen.getByTestId("badge-retrain-trigger").textContent).toBe("BATCH");
  });
});
