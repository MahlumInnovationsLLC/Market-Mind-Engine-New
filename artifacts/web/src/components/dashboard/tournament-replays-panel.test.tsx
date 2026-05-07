import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    data: undefined as unknown,
    isLoading: false,
    error: undefined as unknown,
    isFetching: false,
    refetch: vi.fn(),
  },
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => ({
    data: state.data,
    isLoading: state.isLoading,
    error: state.error,
    isFetching: state.isFetching,
    refetch: state.refetch,
  }),
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => ({
    isSignedIn: true,
    getToken: () => Promise.resolve("token"),
  }),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

// Recharts pulls in resize observers and SVG layout — stub the bits we
// use so the chart renders without exercising real layout code in jsdom.
vi.mock("recharts", () => {
  const Stub = ({ children }: { children?: React.ReactNode }) =>
    children ?? null;
  return {
    CartesianGrid: () => null,
    Line: () => null,
    LineChart: Stub,
    ResponsiveContainer: Stub,
    Tooltip: () => null,
    XAxis: () => null,
    YAxis: () => null,
  };
});

import TournamentReplaysPanel from "./tournament-replays-panel";

afterEach(() => {
  cleanup();
  state.data = undefined;
  state.isLoading = false;
  state.error = undefined;
  state.isFetching = false;
  state.refetch = vi.fn();
});

const sampleResponse = {
  items: [
    {
      id: 2,
      event_id: "sr:tournament:pga-2",
      league: "PGA",
      event_name: "RBC Heritage",
      winner: "Scheffler",
      n_snapshots: 12,
      accuracy: 0.5833,
      brier_score: 0.2104,
      log_loss: 0.612,
      started_at: "2026-04-27T11:00:00+00:00",
      completed_at: "2026-04-27T11:05:00+00:00",
    },
    {
      id: 1,
      event_id: "sr:tournament:pga-1",
      league: "PGA",
      event_name: "The Masters",
      winner: "Rahm",
      n_snapshots: 20,
      accuracy: 0.7,
      brier_score: 0.1502,
      log_loss: 0.4123,
      started_at: "2026-04-26T11:00:00+00:00",
      completed_at: "2026-04-26T11:05:00+00:00",
    },
  ],
};

describe("TournamentReplaysPanel", () => {
  it("renders a loading skeleton while the query is in flight", () => {
    state.isLoading = true;
    render(<TournamentReplaysPanel />);
    expect(screen.getByTestId("tournament-replays-loading")).toBeTruthy();
  });

  it("renders the empty state when the endpoint returns no items", () => {
    state.data = { items: [] };
    render(<TournamentReplaysPanel />);
    expect(screen.getByTestId("tournament-replays-empty")).toBeTruthy();
  });

  it("renders an error banner when the request fails", () => {
    state.error = new Error("boom");
    render(<TournamentReplaysPanel />);
    const err = screen.getByTestId("tournament-replays-error");
    expect(err).toBeTruthy();
    expect(err.textContent).toMatch(/boom/);
  });

  it("renders one row per persisted replay newest-first with formatted metrics", () => {
    state.data = sampleResponse;
    render(<TournamentReplaysPanel />);

    expect(screen.getByTestId("tournament-replays-table")).toBeTruthy();
    expect(screen.getByTestId("tournament-replays-trend")).toBeTruthy();

    const row0 = screen.getByTestId("tournament-replays-row-0");
    expect(row0.textContent).toMatch(/RBC Heritage/);
    expect(row0.textContent).toMatch(/Scheffler/);

    expect(
      screen.getByTestId("tournament-replays-accuracy-0").textContent,
    ).toBe("58.3%");
    expect(
      screen.getByTestId("tournament-replays-brier-0").textContent,
    ).toBe("0.2104");
    expect(
      screen.getByTestId("tournament-replays-log-loss-0").textContent,
    ).toBe("0.6120");

    const row1 = screen.getByTestId("tournament-replays-row-1");
    expect(row1.textContent).toMatch(/The Masters/);
    expect(
      screen.getByTestId("tournament-replays-accuracy-1").textContent,
    ).toBe("70.0%");
  });

  it("falls back to event_id when event_name is empty and renders an em-dash for missing winner", () => {
    state.data = {
      items: [
        {
          id: 5,
          event_id: "sr:race:f1-99",
          league: "",
          event_name: "",
          winner: null,
          n_snapshots: 3,
          accuracy: 0,
          brier_score: 0.5,
          log_loss: 0.8,
          started_at: null,
          completed_at: null,
        },
      ],
    };
    render(<TournamentReplaysPanel />);
    const row = screen.getByTestId("tournament-replays-row-0");
    expect(row.textContent).toMatch(/sr:race:f1-99/);
    // Two em-dashes — one for league, one for winner; completed_at also
    // collapses to em-dash because it's null.
    expect(row.textContent).toMatch(/—/);
  });

  it("re-fetches when the operator clicks Refresh", () => {
    state.data = sampleResponse;
    render(<TournamentReplaysPanel />);
    fireEvent.click(screen.getByTestId("button-tournament-replays-refresh"));
    expect(state.refetch).toHaveBeenCalledTimes(1);
  });
});
