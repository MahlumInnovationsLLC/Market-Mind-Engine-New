import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { state } = vi.hoisted(() => ({
  state: {
    data: undefined as unknown,
    isLoading: false,
    isError: false,
    error: undefined as unknown,
  },
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => state,
}));

vi.mock("@workspace/api-client-react", () => ({
  customFetch: vi.fn(),
}));

import LeaguesExposurePanel from "./leagues-exposure-panel";

afterEach(() => {
  cleanup();
  state.data = undefined;
  state.isLoading = false;
  state.isError = false;
  state.error = undefined;
});

function bucket(opts: Record<string, unknown> = {}) {
  return {
    open_usd: 400,
    cap_pct: 0.05,
    cap_usd: 500,
    headroom_usd: 100,
    utilization: 0.8,
    warn: true,
    recent_alert: null,
    ...opts,
  };
}

describe("LeaguesExposurePanel cap-warn history", () => {
  it("renders 'recently paged' line for buckets with a dedupe row, including dedupe TTL", () => {
    const sentAt = new Date(Date.now() - 7 * 60_000).toISOString();
    state.data = {
      bankroll_usd: 10_000,
      warn_threshold: 0.8,
      leagues: [
        {
          league: "NFL",
          per_league: bucket({
            recent_alert: {
              last_sent_at: sentAt,
              expires_at: new Date(Date.now() + 53 * 60_000).toISOString(),
              ttl_seconds_remaining: 53 * 60,
              fired_last_cycle: false,
              suppressed_last_cycle: false,
            },
          }),
          per_matchup: [],
        },
      ],
      recent_warnings: {
        ran_at: new Date(Date.now() - 30_000).toISOString(),
        threshold: 0.8,
        ttl_seconds: 3600,
        fired_count: 0,
        suppressed_count: 0,
        tracked_buckets: 1,
      },
    };
    render(<LeaguesExposurePanel />);
    const row = screen.getByTestId("leagues-exposure-nfl-per-league-recent-alert");
    expect(row).toBeTruthy();
    expect(row.getAttribute("data-state")).toBe("prior");
    expect(row.textContent).toMatch(/Recently paged/);
    expect(row.textContent).toMatch(/dedupe expires in 53m/);
  });

  it("flags the bucket the dispatcher just fired with a 'fired this cycle' badge", () => {
    state.data = {
      bankroll_usd: 10_000,
      warn_threshold: 0.8,
      leagues: [
        {
          league: "NFL",
          per_league: bucket({
            recent_alert: {
              last_sent_at: new Date().toISOString(),
              expires_at: new Date(Date.now() + 3600_000).toISOString(),
              ttl_seconds_remaining: 3600,
              fired_last_cycle: true,
              suppressed_last_cycle: false,
            },
          }),
          per_matchup: [],
        },
      ],
      recent_warnings: {
        ran_at: new Date().toISOString(),
        threshold: 0.8,
        ttl_seconds: 3600,
        fired_count: 1,
        suppressed_count: 0,
        tracked_buckets: 1,
      },
    };
    render(<LeaguesExposurePanel />);
    const row = screen.getByTestId("leagues-exposure-nfl-per-league-recent-alert");
    expect(row.getAttribute("data-state")).toBe("fired");
    expect(row.textContent).toMatch(/FIRED THIS CYCLE/);
  });

  it("flags a suppressed-by-dedupe bucket separately so silence != quiet", () => {
    state.data = {
      bankroll_usd: 10_000,
      warn_threshold: 0.8,
      leagues: [
        {
          league: "NFL",
          per_league: bucket({
            recent_alert: {
              last_sent_at: new Date(Date.now() - 30 * 60_000).toISOString(),
              expires_at: new Date(Date.now() + 30 * 60_000).toISOString(),
              ttl_seconds_remaining: 30 * 60,
              fired_last_cycle: false,
              suppressed_last_cycle: true,
            },
          }),
          per_matchup: [],
        },
      ],
      recent_warnings: {
        ran_at: new Date().toISOString(),
        threshold: 0.8,
        ttl_seconds: 3600,
        fired_count: 0,
        suppressed_count: 1,
        tracked_buckets: 1,
      },
    };
    render(<LeaguesExposurePanel />);
    const row = screen.getByTestId("leagues-exposure-nfl-per-league-recent-alert");
    expect(row.getAttribute("data-state")).toBe("suppressed");
    expect(row.textContent).toMatch(/DEDUPE SUPPRESSED/);
    const suppressed = screen.getByTestId(
      "leagues-exposure-cap-warn-suppressed",
    );
    expect(suppressed.textContent).toMatch(/suppressed/);
  });

  it("renders a placeholder when a bucket has no recent alert", () => {
    state.data = {
      bankroll_usd: 10_000,
      warn_threshold: 0.8,
      leagues: [
        {
          league: "NFL",
          per_league: bucket({ recent_alert: null }),
          per_matchup: [],
        },
      ],
      recent_warnings: {
        ran_at: new Date().toISOString(),
        threshold: 0.8,
        ttl_seconds: 3600,
        fired_count: 0,
        suppressed_count: 0,
        tracked_buckets: 0,
      },
    };
    render(<LeaguesExposurePanel />);
    const row = screen.getByTestId("leagues-exposure-nfl-per-league-recent-alert");
    expect(row.getAttribute("data-state")).toBe("quiet");
    expect(row.textContent).toMatch(/No cap-warn dispatched recently/);
  });

  it("shows the cap-warn dispatcher summary line with fired + suppressed counts", () => {
    state.data = {
      bankroll_usd: 10_000,
      warn_threshold: 0.8,
      leagues: [
        {
          league: "NFL",
          per_league: bucket({ recent_alert: null }),
          per_matchup: [],
        },
      ],
      recent_warnings: {
        ran_at: new Date(Date.now() - 90_000).toISOString(),
        threshold: 0.8,
        ttl_seconds: 3600,
        fired_count: 2,
        suppressed_count: 1,
        tracked_buckets: 3,
      },
    };
    render(<LeaguesExposurePanel />);
    const summary = screen.getByTestId("leagues-exposure-cap-warn-summary");
    expect(summary.getAttribute("data-state")).toBe("ran");
    expect(summary.getAttribute("data-fired-count")).toBe("2");
    expect(summary.getAttribute("data-suppressed-count")).toBe("1");
    expect(summary.textContent).toMatch(/Cap-warn dispatcher/);
    expect(summary.textContent).toMatch(/last ran/);
    expect(summary.textContent).toMatch(/threshold 80%/);
  });

  it("shows a 'never ran' summary when the dispatcher hasn't run since process start", () => {
    state.data = {
      bankroll_usd: 10_000,
      warn_threshold: 0.8,
      leagues: [
        {
          league: "NFL",
          per_league: bucket({ recent_alert: null }),
          per_matchup: [],
        },
      ],
      recent_warnings: {
        ran_at: null,
        threshold: null,
        ttl_seconds: null,
        fired_count: 0,
        suppressed_count: 0,
        tracked_buckets: 0,
      },
    };
    render(<LeaguesExposurePanel />);
    const summary = screen.getByTestId("leagues-exposure-cap-warn-summary");
    expect(summary.getAttribute("data-state")).toBe("never-ran");
    expect(summary.textContent).toMatch(/has not run/);
  });
});
