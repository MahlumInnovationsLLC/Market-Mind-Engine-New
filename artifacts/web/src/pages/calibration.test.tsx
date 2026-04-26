import { describe, it, expect, afterEach, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

const { qstate } = vi.hoisted(() => ({
  qstate: {
    data: undefined as unknown,
    isLoading: false,
    isError: false,
    error: undefined as unknown,
  },
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => qstate,
}));

vi.mock("@workspace/api-client-react", () => ({
  customFetch: vi.fn(),
}));

import CalibrationPage from "./calibration";

afterEach(() => {
  cleanup();
  qstate.data = undefined;
  qstate.isLoading = false;
  qstate.isError = false;
  qstate.error = undefined;
});

const SNAPSHOT_FIXTURE = {
  items: [
    {
      id: 1,
      vertical: "weather",
      sub_category: null,
      ece: 0.05,
      mce: 0.12,
      brier_score: 0.18,
      sample_count: 1234,
      drift_status: { status: "promoted" },
      // Knot points exercise the isotonic-curve renderer in the
      // ReliabilityDiagram. Two well-formed parallel arrays are the
      calibrator_state: { x: [0.0, 0.5, 1.0], y: [0.0, 0.55, 1.0] },
      config: null,
      created_at: "2026-04-22T12:00:00Z",
    },
    {
      id: 2,
      vertical: "weather",
      sub_category: "rain",
      ece: 0.07,
      mce: 0.2,
      brier_score: 0.27,
      sample_count: 800,
      // 'rolled_back' triggers the destructive badge + bigger marker
      // on the brier-history sparkline.
      drift_status: { status: "rolled_back" },
      calibrator_state: null,
      config: null,
      created_at: "2026-04-23T12:00:00Z",
    },
  ],
  count: 2,
};

describe("CalibrationPage", () => {
  it("renders the loading row inside the snapshots table", () => {
    qstate.isLoading = true;
    render(<CalibrationPage />);
    expect(screen.getByTestId("page-calibration")).toBeTruthy();
    expect(screen.getByText(/Loading…/)).toBeTruthy();
  });

  it("renders the destructive error row when the snapshots fetch fails", () => {
    qstate.isError = true;
    qstate.error = new Error("snapshot service down");
    render(<CalibrationPage />);
    expect(screen.getByText(/snapshot service down/)).toBeTruthy();
  });

  it("renders the empty-state row when zero snapshots are returned", () => {
    qstate.data = { items: [], count: 0 };
    render(<CalibrationPage />);
    expect(screen.getByText(/No snapshots yet/i)).toBeTruthy();
  });

  it("renders one row per snapshot and a 'rolled back' badge for rollbacks", () => {
    qstate.data = SNAPSHOT_FIXTURE;
    render(<CalibrationPage />);
    expect(screen.getByTestId("row-snapshot-1")).toBeTruthy();
    expect(screen.getByTestId("row-snapshot-2")).toBeTruthy();
    expect(screen.getAllByText(/rolled back/i).length).toBeGreaterThan(0);
    expect(screen.getByTestId("table-calibration")).toBeTruthy();
  });

  it("re-selects the clicked snapshot — driving the reliability diagram", () => {
    qstate.data = SNAPSHOT_FIXTURE;
    render(<CalibrationPage />);
    const row2 = screen.getByTestId("row-snapshot-2");
    fireEvent.click(row2);
    // After clicking row 2 (no knots), the diagram should fall back
    // to the explanatory copy for snapshots without isotonic state.
    expect(
      screen.getByText(/no isotonic knot points/i),
    ).toBeTruthy();
  });

  it("changes the vertical filter via the select control", () => {
    qstate.data = SNAPSHOT_FIXTURE;
    render(<CalibrationPage />);
    const select = screen.getByTestId(
      "select-calibration-vertical",
    ) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "nfl" } });
    expect(select.value).toBe("nfl");
  });
});
