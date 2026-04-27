import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

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

vi.mock("@workspace/api-client-react", () => ({
  // The component uses the query-key helper to scope each fetch by
  // action; the test only needs a stable key per action so the mock
  // hook below can route to the right fixture.
  getAdminGetAuditLogQueryKey: (params?: { action?: string; limit?: number }) => [
    "/api/admin/audit-log",
    params ?? {},
  ],
  useAdminGetAuditLog: (params?: { action?: string }) => {
    if (params?.action === "autoresearch_promote") return state.promote;
    if (params?.action === "autoresearch_reject") return state.reject;
    return { data: undefined, isLoading: false, isError: false };
  },
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

  // Window toggle hydrates from localStorage, so wipe it between tests
  // to avoid one test's selection leaking into the next.
  window.localStorage.clear();

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

    // Per-day averages are totals divided by the active window (14d
    // by default), formatted to one decimal so operators can compare
    // ranges at a glance regardless of which window is selected.
    expect(
      screen.getByTestId("text-autoresearch-throughput-promoted-per-day")
        .textContent,
    ).toBe("(0.2/DAY)");
    expect(
      screen.getByTestId("text-autoresearch-throughput-rejected-per-day")
        .textContent,
    ).toBe("(0.3/DAY)");
  });

  it("renders zero per-day averages without dividing by zero on empty windows", () => {
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

    // 0/14 must render as a clean "0.0", never NaN or Infinity.
    expect(
      screen.getByTestId("text-autoresearch-throughput-promoted-per-day")
        .textContent,
    ).toBe("(0.0/DAY)");
    expect(
      screen.getByTestId("text-autoresearch-throughput-rejected-per-day")
        .textContent,
    ).toBe("(0.0/DAY)");
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

  it("defaults to the 14-day window when localStorage is empty", () => {
    state.promote = { data: { items: [] }, isLoading: false, isError: false };
    state.reject = { data: { items: [] }, isLoading: false, isError: false };

    render(<AutoResearchThroughputChart />);

    // Subtitle reflects the default window.
    expect(screen.getByText("Promote vs reject · last 14 days")).toBeTruthy();

    // The 14D button is the active one in the toggle group.
    expect(
      screen
        .getByTestId("button-autoresearch-throughput-window-14d")
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      screen
        .getByTestId("button-autoresearch-throughput-window-7d")
        .getAttribute("aria-pressed"),
    ).toBe("false");
    expect(
      screen
        .getByTestId("button-autoresearch-throughput-window-30d")
        .getAttribute("aria-pressed"),
    ).toBe("false");
  });

  it("updates the visible range and subtitle when the 7D / 30D buttons are clicked", () => {
    // Seed a single in-window entry so the chart branch (not the empty
    // branch) is the one that re-renders, exposing the bucketed series
    // we want to inspect.
    state.promote = {
      data: {
        items: [
          { id: 1, action: "autoresearch_promote", created_at: localIso(2026, 3, 26, 9) },
        ],
      },
      isLoading: false,
      isError: false,
    };
    state.reject = { data: { items: [] }, isLoading: false, isError: false };

    render(<AutoResearchThroughputChart />);

    // Default state: 14d window with 14 buckets ending today.
    expect(screen.getByText("Promote vs reject · last 14 days")).toBeTruthy();
    expect((capturedChart.data as Array<unknown>).length).toBe(14);

    // Switch to 7D — subtitle, active button, and series length all move.
    fireEvent.click(screen.getByTestId("button-autoresearch-throughput-window-7d"));
    expect(screen.getByText("Promote vs reject · last 7 days")).toBeTruthy();
    expect(
      screen
        .getByTestId("button-autoresearch-throughput-window-7d")
        .getAttribute("aria-pressed"),
    ).toBe("true");
    let series = capturedChart.data as Array<{ key: string }>;
    expect(series).toHaveLength(7);
    expect(series[0].key).toBe("2026-04-20");
    expect(series[6].key).toBe("2026-04-26");

    // Switch to 30D — same three observable changes in the other direction.
    fireEvent.click(screen.getByTestId("button-autoresearch-throughput-window-30d"));
    expect(screen.getByText("Promote vs reject · last 30 days")).toBeTruthy();
    expect(
      screen
        .getByTestId("button-autoresearch-throughput-window-30d")
        .getAttribute("aria-pressed"),
    ).toBe("true");
    series = capturedChart.data as Array<{ key: string }>;
    expect(series).toHaveLength(30);
    expect(series[0].key).toBe("2026-03-28");
    expect(series[29].key).toBe("2026-04-26");
  });

  it("persists the chosen window to localStorage and re-reads it on remount", () => {
    state.promote = { data: { items: [] }, isLoading: false, isError: false };
    state.reject = { data: { items: [] }, isLoading: false, isError: false };

    const { unmount } = render(<AutoResearchThroughputChart />);

    fireEvent.click(screen.getByTestId("button-autoresearch-throughput-window-30d"));

    // The selection landed in localStorage under the documented key.
    expect(
      window.localStorage.getItem("axiom.autoresearch-throughput-window"),
    ).toBe("30");

    // Tear down the component and mount a fresh instance — it should
    // hydrate from localStorage instead of falling back to 14d.
    unmount();
    cleanup();

    render(<AutoResearchThroughputChart />);

    expect(screen.getByText("Promote vs reject · last 30 days")).toBeTruthy();
    expect(
      screen
        .getByTestId("button-autoresearch-throughput-window-30d")
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      screen
        .getByTestId("button-autoresearch-throughput-window-14d")
        .getAttribute("aria-pressed"),
    ).toBe("false");
  });
});
