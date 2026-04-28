import { describe, it, expect, afterEach, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    eventsData: undefined as unknown,
    eventsLoading: false,
    eventsError: undefined as unknown,
    timelineData: undefined as unknown,
    timelineLoading: false,
    timelineError: undefined as unknown,
  },
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const [name] = queryKey as string[];
    if (name === "dashboard-tournament-leaderboards") {
      return {
        data: state.eventsData,
        isLoading: state.eventsLoading,
        isError: state.eventsError !== undefined,
        error: state.eventsError,
      };
    }
    return {
      data: state.timelineData,
      isLoading: state.timelineLoading,
      isError: state.timelineError !== undefined,
      error: state.timelineError,
    };
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  customFetch: vi.fn(),
}));

import TournamentTimelinePanel from "./tournament-timeline-panel";

afterEach(() => {
  cleanup();
  state.eventsData = undefined;
  state.eventsLoading = false;
  state.eventsError = undefined;
  state.timelineData = undefined;
  state.timelineLoading = false;
  state.timelineError = undefined;
});

const recentEvents = {
  events: [
    {
      event_id: "sr:tournament:pga-1",
      league: "PGA",
      name: "The Masters",
      status: "inprogress",
      fetched_at: "2026-04-27T12:05:00+00:00",
    },
    {
      event_id: "sr:race:f1-1",
      league: "F1",
      name: "Miami GP",
      status: "live",
      fetched_at: "2026-04-27T12:10:00+00:00",
    },
  ],
};

const timeline = {
  event_id: "sr:tournament:pga-1",
  league: "PGA",
  name: "The Masters",
  status: "inprogress",
  snapshots: [
    {
      fetched_at: "2026-04-27T12:00:00+00:00",
      entries: [
        { position: 1, name: "Rahm", probability: 0.55 },
        { position: 2, name: "Scheffler", probability: 0.3 },
        { position: 3, name: "Morikawa", probability: 0.15 },
      ],
    },
    {
      fetched_at: "2026-04-27T12:05:00+00:00",
      entries: [
        { position: 1, name: "Scheffler", probability: 0.6 },
        { position: 2, name: "Rahm", probability: 0.25 },
        { position: 3, name: "Morikawa", probability: 0.15 },
      ],
    },
    {
      fetched_at: "2026-04-27T12:10:00+00:00",
      entries: [
        { position: 1, name: "Morikawa", probability: 0.7 },
        { position: 2, name: "Scheffler", probability: 0.2 },
        { position: 3, name: "Rahm", probability: 0.1 },
      ],
    },
  ],
};

describe("TournamentTimelinePanel", () => {
  it("populates the event dropdown from the recent-events feed", () => {
    state.eventsData = recentEvents;
    state.timelineData = timeline;
    render(<TournamentTimelinePanel />);
    const select = screen.getByTestId(
      "select-tournament-timeline-event",
    ) as HTMLSelectElement;
    const optionLabels = Array.from(select.options).map((o) => o.textContent);
    expect(optionLabels).toEqual([
      "PGA · The Masters",
      "F1 · Miami GP",
    ]);
    // First event auto-selected so the panel renders without operator
    // input.
    expect(select.value).toBe("sr:tournament:pga-1");
  });

  it("defaults to the latest snapshot and shows position-ordered roster + PL probabilities", () => {
    state.eventsData = recentEvents;
    state.timelineData = timeline;
    render(<TournamentTimelinePanel />);

    expect(
      screen.getByTestId("text-tournament-timeline-tick-position").textContent,
    ).toMatch(/Snapshot 3 \/ 3/);
    expect(
      screen.getByTestId("text-tournament-timeline-tick-fetched-at").textContent,
    ).toMatch(/2026-04-27 12:10:00Z/);

    // Latest snapshot — Morikawa leads the field.
    const row0 = screen.getByTestId("row-tournament-timeline-entry-0");
    expect(row0.textContent).toMatch(/Morikawa/);
    expect(
      screen.getByTestId("text-tournament-timeline-prob-0").textContent,
    ).toBe("70.0%");
    expect(
      screen.getByTestId("text-tournament-timeline-prob-1").textContent,
    ).toBe("20.0%");
    expect(
      screen.getByTestId("text-tournament-timeline-prob-2").textContent,
    ).toBe("10.0%");
  });

  it("scrubs through earlier snapshots when the slider moves", () => {
    state.eventsData = recentEvents;
    state.timelineData = timeline;
    render(<TournamentTimelinePanel />);

    const slider = screen.getByTestId(
      "input-tournament-timeline-slider",
    ) as HTMLInputElement;
    expect(slider.max).toBe("2");
    expect(slider.value).toBe("2");

    fireEvent.change(slider, { target: { value: "0" } });
    expect(
      screen.getByTestId("text-tournament-timeline-tick-position").textContent,
    ).toMatch(/Snapshot 1 \/ 3/);
    expect(
      screen.getByTestId("text-tournament-timeline-tick-fetched-at").textContent,
    ).toMatch(/2026-04-27 12:00:00Z/);
    // First snapshot — Rahm leads.
    expect(
      screen.getByTestId("row-tournament-timeline-entry-0").textContent,
    ).toMatch(/Rahm/);
    expect(
      screen.getByTestId("text-tournament-timeline-prob-0").textContent,
    ).toBe("55.0%");
  });

  it("renders the empty state when no recent events have snapshots", () => {
    state.eventsData = { events: [] };
    render(<TournamentTimelinePanel />);
    expect(screen.getByTestId("empty-tournament-timeline-events")).toBeTruthy();
  });

  it("renders an empty timeline message when the chosen event has no snapshots", () => {
    state.eventsData = recentEvents;
    state.timelineData = {
      event_id: "sr:tournament:pga-1",
      league: "PGA",
      name: "The Masters",
      status: "inprogress",
      snapshots: [],
    };
    render(<TournamentTimelinePanel />);
    expect(screen.getByTestId("empty-tournament-timeline")).toBeTruthy();
  });

  it("surfaces a probability of '—' when a snapshot was too thin to price", () => {
    state.eventsData = recentEvents;
    state.timelineData = {
      event_id: "sr:tournament:pga-1",
      league: "PGA",
      name: "The Masters",
      status: "inprogress",
      snapshots: [
        {
          fetched_at: "2026-04-27T12:00:00+00:00",
          entries: [
            { position: 1, name: "Lone Wolf", probability: null },
          ],
        },
      ],
    };
    render(<TournamentTimelinePanel />);
    expect(
      screen.getByTestId("text-tournament-timeline-prob-0").textContent,
    ).toBe("—");
  });

  it("renders every snapshot returned by the API — no silent client-side cap", () => {
    state.eventsData = recentEvents;
    const longHistory = Array.from({ length: 500 }, (_, i) => ({
      fetched_at: new Date(2026, 3, 27, 0, i, 0).toISOString(),
      entries: [
        { position: 1, name: "Rahm", probability: 0.5 },
        { position: 2, name: "Scheffler", probability: 0.5 },
      ],
    }));
    state.timelineData = {
      event_id: "sr:tournament:pga-1",
      league: "PGA",
      name: "The Masters",
      status: "inprogress",
      snapshots: longHistory,
    };
    render(<TournamentTimelinePanel />);
    const slider = screen.getByTestId(
      "input-tournament-timeline-slider",
    ) as HTMLInputElement;
    expect(slider.max).toBe("499");
    expect(
      screen.getByTestId("text-tournament-timeline-tick-position").textContent,
    ).toMatch(/Snapshot 500 \/ 500/);
  });

  it("surfaces an error banner when the timeline request fails", () => {
    state.eventsData = recentEvents;
    state.timelineError = new Error("boom");
    render(<TournamentTimelinePanel />);
    expect(
      screen.getByTestId("text-tournament-timeline-error").textContent,
    ).toMatch(/boom/);
  });
});
