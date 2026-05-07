import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const { queryState } = vi.hoisted(() => ({
  queryState: {
    data: undefined as unknown,
    isLoading: false,
    isError: false,
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  SportradarLeaderboardsRetentionStatusHealth: {
    ok: "ok",
    stale: "stale",
    error: "error",
    disabled: "disabled",
  },
  useGetSportradarLeaderboardsRetentionStatus: () => queryState,
  getGetSportradarLeaderboardsRetentionStatusQueryKey: () => [
    "sportradar-leaderboards-retention",
  ],
}));

import SportradarLeaderboardsRetentionPanel from "./sportradar-leaderboards-retention-panel";

function baseStatus(overrides: Record<string, unknown> = {}) {
  return {
    health: "ok",
    enabled: true,
    running: true,
    last_tick_at: 1_700_000_000,
    seconds_since_last_tick: 30,
    last_deleted: 0,
    total_deleted: 1234,
    interval_seconds: 3600,
    started_at: 1_699_990_000,
    last_error: null,
    policy: {
      min_snapshots: 5,
      retention_days: 30,
    },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  queryState.data = undefined;
  queryState.isLoading = false;
  queryState.isError = false;
});

describe("SportradarLeaderboardsRetentionPanel trimming indicator", () => {
  it("hides the TRIMMING badge when the most recent sweep deleted 0 rows", () => {
    queryState.data = baseStatus({ last_deleted: 0 });
    render(<SportradarLeaderboardsRetentionPanel />);
    expect(
      screen.queryByTestId("sportradar-leaderboards-retention-trimming-badge"),
    ).toBeNull();
  });

  it("hides the TRIMMING badge when last_deleted is null", () => {
    queryState.data = baseStatus({ last_deleted: null });
    render(<SportradarLeaderboardsRetentionPanel />);
    expect(
      screen.queryByTestId("sportradar-leaderboards-retention-trimming-badge"),
    ).toBeNull();
  });

  it("shows the TRIMMING badge whenever the most recent sweep deleted rows", () => {
    queryState.data = baseStatus({ last_deleted: 42 });
    render(<SportradarLeaderboardsRetentionPanel />);
    const badge = screen.getByTestId(
      "sportradar-leaderboards-retention-trimming-badge",
    );
    expect(badge.textContent).toMatch(/TRIMMING/);
  });
});
