import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

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

  it("renders the per-entrant Plackett-Luce table for tournament debates", () => {
    apiState.debatesResult = {
      data: {
        items: [
          makeDebate({
            id: 555,
            league: "ATP",
            matchup: "ATP — Roland Garros 2025",
            market_type: "outright",
            redacted: false,
            game_context: {
              kalshi_market_id: "KX-ATP-RG-2025",
              plackett_luce_sketch: {
                model: "plackett_luce",
                entrant: "alcaraz",
                entrant_rating: 2200,
                entrant_probability: 0.45,
                field_size: 3,
                ratings: { alcaraz: 2200, sinner: 2150, djokovic: 2100 },
                probabilities: { alcaraz: 0.45, sinner: 0.35, djokovic: 0.2 },
              },
            },
          }),
          makeDebate({
            id: 556,
            league: "NBA",
            matchup: "Team A vs Team B",
            redacted: false,
            game_context: { kalshi_market_id: "KX-NBA-001" },
          }),
        ],
      },
      isLoading: false,
    };

    render(<SwarmDebates />);

    // Sketch only renders for the ATP debate.
    const table = screen.getByTestId("debate-pl-sketch-555");
    expect(table).toBeTruthy();
    expect(screen.queryByTestId("debate-pl-sketch-556")).toBeNull();

    // Three rows for three entrants — one for each entry in the sketch.
    const alcaraz = within(table).getByTestId("debate-pl-row-555-alcaraz");
    const sinner = within(table).getByTestId("debate-pl-row-555-sinner");
    const djokovic = within(table).getByTestId("debate-pl-row-555-djokovic");

    // Traded entrant is highlighted; the others are not.
    expect(alcaraz.getAttribute("data-traded")).toBe("true");
    expect(sinner.getAttribute("data-traded")).toBe("false");
    expect(djokovic.getAttribute("data-traded")).toBe("false");
    expect(within(alcaraz).getByTestId("debate-pl-traded-badge-555")).toBeTruthy();

    // Probabilities are formatted as percentages and the displayed numbers
    // sum to 100% — i.e. probabilities sum to 1.0 to within rendering precision.
    const cells = within(table).getAllByText(/\d+\.\d%/);
    const sumPct = cells.reduce(
      (acc, el) => acc + Number.parseFloat(el.textContent ?? "0"),
      0,
    );
    expect(Math.round(sumPct * 10)).toBe(1000);

    // Default sort is by probability descending: Alcaraz (45%) → Sinner (35%) → Djokovic (20%).
    const orderedRows = within(table).getAllByTestId(/^debate-pl-row-555-/);
    expect(orderedRows.map((r) => r.getAttribute("data-testid"))).toEqual([
      "debate-pl-row-555-alcaraz",
      "debate-pl-row-555-sinner",
      "debate-pl-row-555-djokovic",
    ]);

    // Sorting by rating ascending puts the lowest rating (Djokovic, 2100) first.
    const ratingHeader = within(table).getByTestId("debate-pl-sort-rating-555");
    fireEvent.click(ratingHeader);
    fireEvent.click(ratingHeader);
    const afterRatingSort = within(table).getAllByTestId(/^debate-pl-row-555-/);
    expect(afterRatingSort[0].getAttribute("data-testid")).toBe(
      "debate-pl-row-555-djokovic",
    );
    expect(afterRatingSort[2].getAttribute("data-testid")).toBe(
      "debate-pl-row-555-alcaraz",
    );
  });

  it("hides the Plackett-Luce table when game_context is missing or stripped", () => {
    apiState.debatesResult = {
      data: {
        items: [
          // Pro caller, but the underlying debate was a binary moneyline
          // and so carries no plackett_luce_sketch.
          makeDebate({ id: 700, redacted: false, game_context: null }),
          // Sub-Pro caller — the API redacted the game_context entirely.
          makeDebate({ id: 701, redacted: true }),
        ],
      },
      isLoading: false,
    };

    render(<SwarmDebates />);

    expect(screen.queryByTestId("debate-pl-sketch-700")).toBeNull();
    expect(screen.queryByTestId("debate-pl-sketch-701")).toBeNull();
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
