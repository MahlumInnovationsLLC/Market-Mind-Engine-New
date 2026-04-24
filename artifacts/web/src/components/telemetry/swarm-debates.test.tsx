import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

const { apiState } = vi.hoisted(() => ({
  apiState: {
    debatesResult: { data: undefined, isLoading: false } as {
      data: unknown;
      isLoading: boolean;
    },
  },
}));

vi.mock("wouter", async () => {
  const React = await import("react");
  const Link = ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
    [key: string]: unknown;
  }) => React.createElement("a", { href, ...rest }, children);
  return { Link };
});

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({
    invalidateQueries: () => undefined,
  }),
}));

vi.mock("@workspace/api-client-react", () => ({
  useGetRecentDebates: () => apiState.debatesResult,
  getGetRecentDebatesQueryKey: (params: unknown) => ["recent-debates", params],
}));

vi.mock("@/hooks/use-websocket", () => ({
  useWebSocket: () => ({ status: "open" }),
}));

vi.mock("@/components/delayed-signals-banner", () => ({
  default: () => null,
}));

import SwarmDebates from "./swarm-debates";

function makeDebate(overrides: Record<string, unknown>) {
  return {
    id: 1,
    league: "NBA",
    matchup: "Team A vs Team B",
    market_type: "h2h",
    consensus_probability: 0.55,
    confidence_band_low: 0.5,
    confidence_band_high: 0.6,
    agreement_rate: 0.8,
    approved: true,
    recommended_action: "BUY",
    cost_estimate: 0.123,
    latency_ms: 1234,
    created_at: "2026-04-24T12:00:00Z",
    analyst_verdicts: [],
    moderator_synthesis: null,
    redacted: false,
    ...overrides,
  };
}

beforeEach(() => {
  apiState.debatesResult = {
    data: {
      items: [
        makeDebate({ id: 101, redacted: true, matchup: "Free A vs Free B" }),
        makeDebate({ id: 202, redacted: false, matchup: "Pro A vs Pro B" }),
        makeDebate({ id: 303, redacted: true, matchup: "Free C vs Free D" }),
        makeDebate({ id: 404, redacted: null, matchup: "Null vs Null" }),
      ],
    },
    isLoading: false,
  };
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SwarmDebates redacted upsell", () => {
  it("renders the Pro upsell only on redacted items and links to /pricing", () => {
    render(<SwarmDebates />);

    // Redacted items get exactly one upsell each.
    const upsell101 = screen.getByTestId("upsell-redacted-101");
    const upsell303 = screen.getByTestId("upsell-redacted-303");
    expect(upsell101).toBeTruthy();
    expect(upsell303).toBeTruthy();
    expect(screen.getAllByTestId(/^upsell-redacted-/)).toHaveLength(2);

    // Non-redacted items (false and null) must not render an upsell.
    expect(screen.queryByTestId("upsell-redacted-202")).toBeNull();
    expect(screen.queryByTestId("upsell-redacted-404")).toBeNull();

    // Each upsell points to /pricing via its UPGRADE link.
    const link101 = within(upsell101).getByTestId("link-upgrade-101");
    const link303 = within(upsell303).getByTestId("link-upgrade-303");
    expect(link101.getAttribute("href")).toBe("/pricing");
    expect(link303.getAttribute("href")).toBe("/pricing");

    // And there are exactly two upgrade links overall — one per redacted card.
    const allUpgradeLinks = screen.getAllByTestId(/^link-upgrade-/);
    expect(allUpgradeLinks).toHaveLength(2);
    for (const link of allUpgradeLinks) {
      expect(link.getAttribute("href")).toBe("/pricing");
    }
  });

  it("renders no upsell at all when every item is unredacted (Pro+ caller)", () => {
    apiState.debatesResult = {
      data: {
        items: [
          makeDebate({ id: 1, redacted: false }),
          makeDebate({ id: 2, redacted: false }),
          makeDebate({ id: 3, redacted: false }),
        ],
      },
      isLoading: false,
    };

    render(<SwarmDebates />);

    expect(screen.queryAllByTestId(/^upsell-redacted-/)).toHaveLength(0);
    expect(screen.queryAllByTestId(/^link-upgrade-/)).toHaveLength(0);
  });
});
