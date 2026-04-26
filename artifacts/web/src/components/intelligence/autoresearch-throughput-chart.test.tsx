import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state, capturedChart } = vi.hoisted(() => ({
  state: {
    promote: {
      data: undefined as unknown,
      isLoading: false,
      isError: false,
    },
    reject: {
      data: undefined as unknown,
      isLoading: false,
      isError: false,
    },
  },
  capturedChart: { data: null as unknown },
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => ({
    getToken: async () => "test-token",
    isSignedIn: true,
  }),
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const action = queryKey[3];
    if (action === "autoresearch_promote") return state.promote;
    if (action === "autoresearch_reject") return state.reject;
    return { data: undefined, isLoading: false, isError: false };
  },
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

// Stub out the heavy SVG chart so the test can read the bucketed data
// straight off the props instead of trying to introspect rendered SVG.
vi.mock("recharts", async () => {
  const React = await import("react");
  return {
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) =>
      React.createElement("div", { "data-testid": "responsive" }, children),
    BarChart: ({
      data,
      children,
    }: {
      data: unknown;
      children: React.ReactNode;
    }) => {
      capturedChart.data = data;
      return React.createElement(
        "div",
        {
          "data-testid": "bar-chart",
          "data-chart-payload": JSON.stringify(data),
        },
        children,
      );
    },
    Bar: () => null,
    CartesianGrid: () => null,
    XAxis: () => null,
    YAxis: () => null,
    Tooltip: () => null,
  };
});

import AutoResearchThroughputChart from "./autoresearch-throughput-chart";

beforeEach(() => {
  // Anchor "today" so the rolling 14-day window is deterministic.
  vi.useFakeTimers();
  vi.setSystemTime(new Date(2026, 3, 26, 12, 0, 0));

  state.promote = { data: undefined, isLoading: false, isError: false };
  state.reject = { data: undefined, isLoading: false, isError: false };
  capturedChart.data = null;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function localIso(year: number, monthIndex: number, day: number, hour = 12) {
  // Build an ISO timestamp anchored in the local timezone so it falls in
  // the same day bucket the component computes from the local clock.
  return new Date(year, monthIndex, day, hour, 0, 0).toISOString();
}

describe("AutoResearchThroughputChart", () => {
  it("buckets entries by day across both audit-log queries and renders totals", () => {
    state.promote = {
      data: {
        items: [
          // Two promotions on 2026-04-26 (today).
          { id: 1, action: "autoresearch_promote", created_at: localIso(2026, 3, 26, 9) },
          { id: 2, action: "autoresearch_promote", created_at: localIso(2026, 3, 26, 11) },
          // One promotion on 2026-04-25.
          { id: 3, action: "autoresearch_promote", created_at: localIso(2026, 3, 25, 8) },
          // Outside the 14-day window (2026-04-10), must be ignored.
          { id: 4, action: "autoresearch_promote", created_at: localIso(2026, 3, 10) },
          // Missing timestamp, must be ignored.
          { id: 5, action: "autoresearch_promote", created_at: null },
        ],
      },
      isLoading: false,
      isError: false,
    };
    state.reject = {
      data: {
        items: [
          // Three rejects on 2026-04-25.
          { id: 11, action: "autoresearch_reject", created_at: localIso(2026, 3, 25, 7) },
          { id: 12, action: "autoresearch_reject", created_at: localIso(2026, 3, 25, 14) },
          { id: 13, action: "autoresearch_reject", created_at: localIso(2026, 3, 25, 22) },
          // One reject on 2026-04-20.
          { id: 14, action: "autoresearch_reject", created_at: localIso(2026, 3, 20) },
          // Garbage timestamp, must be skipped without throwing.
          { id: 15, action: "autoresearch_reject", created_at: "not-a-date" },
          // Wrong action type bleeding through the same payload, must not be counted.
          { id: 16, action: "some_other_action", created_at: localIso(2026, 3, 26) },
        ],
      },
      isLoading: false,
      isError: false,
    };

    render(<AutoResearchThroughputChart />);

    // Totals: 3 promoted (the 4th is out of window, the 5th has no ts), 4 rejected.
    expect(
      screen.getByTestId("text-autoresearch-throughput-promoted").textContent,
    ).toBe("3 PROMOTED");
    expect(
      screen.getByTestId("text-autoresearch-throughput-rejected").textContent,
    ).toBe("4 REJECTED");

    // Chart container rendered (i.e. non-empty, non-error branch).
    expect(screen.getByTestId("chart-autoresearch-throughput")).toBeTruthy();

    // The chart receives one row per day for the full window.
    const series = capturedChart.data as Array<{
      key: string;
      label: string;
      promoted: number;
      rejected: number;
    }>;
    expect(series).toHaveLength(14);

    // The window walks from oldest -> newest, ending on today.
    expect(series[0].key).toBe("2026-04-13");
    expect(series[13].key).toBe("2026-04-26");

    // Per-day buckets line up with the seeded entries.
    const byKey = new Map(series.map((row) => [row.key, row]));
    expect(byKey.get("2026-04-26")).toMatchObject({ promoted: 2, rejected: 0 });
    expect(byKey.get("2026-04-25")).toMatchObject({ promoted: 1, rejected: 3 });
    expect(byKey.get("2026-04-20")).toMatchObject({ promoted: 0, rejected: 1 });

    // Days with no activity inside the window are still present with zeros.
    expect(byKey.get("2026-04-22")).toMatchObject({ promoted: 0, rejected: 0 });
    expect(byKey.get("2026-04-13")).toMatchObject({ promoted: 0, rejected: 0 });

    // The series totals match the headline counters.
    const totalPromoted = series.reduce((acc, row) => acc + row.promoted, 0);
    const totalRejected = series.reduce((acc, row) => acc + row.rejected, 0);
    expect(totalPromoted).toBe(3);
    expect(totalRejected).toBe(4);
  });

  it("renders the empty state when both queries return no in-window items", () => {
    state.promote = {
      data: {
        items: [
          // Only entries outside the window — totals will be zero.
          { id: 1, action: "autoresearch_promote", created_at: localIso(2026, 2, 1) },
        ],
      },
      isLoading: false,
      isError: false,
    };
    state.reject = {
      data: { items: [] },
      isLoading: false,
      isError: false,
    };

    render(<AutoResearchThroughputChart />);

    expect(screen.getByTestId("text-autoresearch-throughput-empty")).toBeTruthy();
    expect(screen.getByText("NO_DECISIONS_IN_WINDOW")).toBeTruthy();

    // Headline totals are zeros and the chart container is not rendered.
    expect(
      screen.getByTestId("text-autoresearch-throughput-promoted").textContent,
    ).toBe("0 PROMOTED");
    expect(
      screen.getByTestId("text-autoresearch-throughput-rejected").textContent,
    ).toBe("0 REJECTED");
    expect(screen.queryByTestId("chart-autoresearch-throughput")).toBeNull();
  });

  it("renders the error state if either audit-log query fails", () => {
    state.promote = {
      data: { items: [] },
      isLoading: false,
      isError: true,
    };
    state.reject = {
      data: { items: [] },
      isLoading: false,
      isError: false,
    };

    render(<AutoResearchThroughputChart />);

    expect(
      screen.getByTestId("text-autoresearch-throughput-error"),
    ).toBeTruthy();
    expect(screen.getByText("ERR_AUTORESEARCH_THROUGHPUT_LOAD")).toBeTruthy();

    // Error branch wins over both empty and chart branches.
    expect(screen.queryByTestId("chart-autoresearch-throughput")).toBeNull();
    expect(screen.queryByTestId("text-autoresearch-throughput-empty")).toBeNull();
  });
});
