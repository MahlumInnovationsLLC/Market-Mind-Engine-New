import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

const { apiState } = vi.hoisted(() => ({
  apiState: {
    debatesResult: { data: undefined, isLoading: false } as {
      data: unknown;
      isLoading: boolean;
    },
    entrantMarketsResult: { data: undefined, isLoading: false } as {
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
  useGetDebateEntrantMarkets: () => apiState.entrantMarketsResult,
  getGetDebateEntrantMarketsQueryKey: (debateId: number) => [
    "debate-entrant-markets",
    debateId,
  ],
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
  apiState.entrantMarketsResult = {
    data: { debate_id: 0, entrants: {} },
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
    apiState.entrantMarketsResult = {
      data: {
        debate_id: 555,
        traded_market_id: "KX-ATP-RG-2025-ALCARAZ",
        traded_entrant: "alcaraz",
        entrants: {
          alcaraz: {
            kalshi_market_id: "KX-ATP-RG-2025-ALCARAZ",
            mid_cents: 47,
            kalshi_url: "https://kalshi.com/markets/kx/atp-rg-2025-alcaraz",
          },
          sinner: {
            kalshi_market_id: "KX-ATP-RG-2025-SINNER",
            mid_cents: 33,
            kalshi_url: "https://kalshi.com/markets/kx/atp-rg-2025-sinner",
          },
          // Djokovic deliberately omitted: simulates a sibling ticker
          // missing from market_title_cache (e.g. delisted) so the row
          // should still render but stay non-clickable with no price.
        },
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

    // Mid prices render in cents next to the model probability for
    // entrants whose outright leg resolved.  Djokovic, omitted from
    // the markets payload, falls back to the em-dash placeholder.
    const alcarazMid = within(table).getByTestId("debate-pl-mid-555-alcaraz");
    const sinnerMid = within(table).getByTestId("debate-pl-mid-555-sinner");
    const djokovicMid = within(table).getByTestId("debate-pl-mid-555-djokovic");
    expect(alcarazMid.textContent).toBe("47¢");
    expect(sinnerMid.textContent).toBe("33¢");
    expect(djokovicMid.textContent).toBe("—");

    // Resolved rows wrap the entrant cell in a Kalshi outright link;
    // unresolved rows do not.  Links open in a new tab with secure rel.
    const alcarazLink = within(table).getByTestId(
      "debate-pl-link-555-alcaraz",
    ) as HTMLAnchorElement;
    const sinnerLink = within(table).getByTestId(
      "debate-pl-link-555-sinner",
    ) as HTMLAnchorElement;
    expect(alcarazLink.getAttribute("href")).toBe(
      "https://kalshi.com/markets/kx/atp-rg-2025-alcaraz",
    );
    expect(alcarazLink.getAttribute("target")).toBe("_blank");
    expect(alcarazLink.getAttribute("rel")).toContain("noopener");
    expect(sinnerLink.getAttribute("href")).toBe(
      "https://kalshi.com/markets/kx/atp-rg-2025-sinner",
    );
    expect(screen.queryByTestId("debate-pl-link-555-djokovic")).toBeNull();
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

describe("SwarmDebates click-to-expand transcript", () => {
  const QUANT_REASONING =
    "Model edge of +320bps with the line crossing the LV close — keep size modest until volume confirms.";
  const NARRATIVE_REASONING =
    "Public is heavy on the favourite but the underlying news flow is mixed; lean YES with low conviction.";
  const MODERATOR_SYNTHESIS_TEXT =
    "Quant carries a clear edge while narrative concurs with low conviction; consensus YES at 0.61, band 0.55–0.66.";

  beforeEach(() => {
    apiState.debatesResult = {
      data: {
        items: [
          makeDebate({
            id: 900,
            redacted: false,
            matchup: "NYK vs BOS",
            recommended_action: "BUY YES",
            consensus_probability: 0.61,
            confidence_band_low: 0.55,
            confidence_band_high: 0.66,
            agreement_rate: 0.83,
            analyst_verdicts: [
              {
                persona: "quant",
                provider: "openai",
                model: "gpt-5",
                fallback_used: false,
                probability: 0.63,
                confidence: 0.7,
                lean: "YES",
                weight: 0.6,
                reasoning: QUANT_REASONING,
                latency_ms: 1840,
              },
              {
                persona: "narrative",
                provider: "anthropic",
                model: "claude-4",
                fallback_used: false,
                probability: 0.59,
                confidence: 0.45,
                lean: "YES",
                weight: 0.4,
                reasoning: NARRATIVE_REASONING,
                latency_ms: 2110,
              },
            ],
            moderator_synthesis: {
              model: "gpt-5",
              recommended_action: "BUY YES",
              synthesis: MODERATOR_SYNTHESIS_TEXT,
              gatekeeper: {
                model: "gpt-5",
                approved: true,
                recommended_action: "BUY YES",
                veto_reason: null,
              },
            },
          }),
          makeDebate({
            id: 901,
            redacted: false,
            matchup: "GS vs LAL",
            recommended_action: "PASS",
            analyst_verdicts: [
              {
                persona: "quant",
                provider: "openai",
                model: "gpt-5",
                fallback_used: false,
                probability: 0.51,
                lean: "PASS",
                reasoning: "Edge collapses against the post-news line.",
              },
            ],
            moderator_synthesis: {
              model: "gpt-5",
              recommended_action: "PASS",
              synthesis: "Marginal edge — recommend pass.",
              gatekeeper: null,
            },
          }),
          makeDebate({
            id: 902,
            redacted: true,
            matchup: "Locked vs Locked",
          }),
        ],
      },
      isLoading: false,
    };
  });

  it("does not render the transcript by default", () => {
    render(<SwarmDebates />);
    expect(screen.queryByTestId("debate-transcript-900")).toBeNull();
    expect(screen.queryByTestId("debate-moderator-900")).toBeNull();
    expect(screen.queryByText(QUANT_REASONING)).toBeNull();
  });

  it("expands a row on click and reveals the moderator synthesis + per-persona reasoning", () => {
    render(<SwarmDebates />);

    const toggle = screen.getByTestId("debate-toggle-900");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    // Transcript region appears, with both the moderator narrative and
    // each persona's reasoning rendered verbatim — that's the whole
    // point of the feature: show "why the swarm came to their
    // agreement".
    const transcript = screen.getByTestId("debate-transcript-900");
    expect(transcript).toBeTruthy();
    const moderator = within(transcript).getByTestId("debate-moderator-900");
    expect(moderator.textContent).toContain(MODERATOR_SYNTHESIS_TEXT);
    expect(within(transcript).getByText(QUANT_REASONING)).toBeTruthy();
    expect(within(transcript).getByText(NARRATIVE_REASONING)).toBeTruthy();

    // Both persona blocks are present and tagged by persona name so
    // future consumers (analytics, screen-readers) can target them.
    expect(
      within(transcript).getByTestId("debate-persona-900-quant"),
    ).toBeTruthy();
    expect(
      within(transcript).getByTestId("debate-persona-900-narrative"),
    ).toBeTruthy();

    // Gatekeeper card surfaces approve/veto with model + final action.
    const gk = within(transcript).getByTestId("debate-gatekeeper-900");
    expect(gk.textContent).toMatch(/APPROVED/);
  });

  it("collapses the transcript when the same row is clicked twice", () => {
    render(<SwarmDebates />);

    const toggle = screen.getByTestId("debate-toggle-900");
    fireEvent.click(toggle);
    expect(screen.getByTestId("debate-transcript-900")).toBeTruthy();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("debate-transcript-900")).toBeNull();
  });

  it("only one row stays expanded at a time (single-accordion)", () => {
    render(<SwarmDebates />);

    fireEvent.click(screen.getByTestId("debate-toggle-900"));
    expect(screen.getByTestId("debate-transcript-900")).toBeTruthy();

    fireEvent.click(screen.getByTestId("debate-toggle-901"));
    expect(screen.getByTestId("debate-transcript-901")).toBeTruthy();
    // Opening row 901 collapses row 900 — keeps the panel scannable
    // when several debates are in flight at once.
    expect(screen.queryByTestId("debate-transcript-900")).toBeNull();
  });

  it("redacted rows do not render the toggle button (transcript is server-stripped)", () => {
    render(<SwarmDebates />);
    // The Pro upsell stays visible, but there is no toggle to expand —
    // there's nothing in the payload to reveal.
    expect(screen.queryByTestId("debate-toggle-902")).toBeNull();
    expect(screen.getByTestId("upsell-redacted-902")).toBeTruthy();
  });

  it("shows VETOED + reason when the gatekeeper rejects", () => {
    apiState.debatesResult = {
      data: {
        items: [
          makeDebate({
            id: 910,
            redacted: false,
            recommended_action: "PASS",
            analyst_verdicts: [],
            moderator_synthesis: {
              model: "gpt-5",
              recommended_action: "PASS",
              synthesis: "Quant flagged a model edge but bankroll is over cap.",
              gatekeeper: {
                model: "gpt-5",
                approved: false,
                recommended_action: "PASS",
                veto_reason: "Bankroll guardrail tripped: exposure > 12%.",
              },
            },
          }),
        ],
      },
      isLoading: false,
    };
    render(<SwarmDebates />);
    fireEvent.click(screen.getByTestId("debate-toggle-910"));
    const gk = screen.getByTestId("debate-gatekeeper-910");
    expect(gk.textContent).toMatch(/VETOED/);
    expect(gk.textContent).toMatch(/Bankroll guardrail tripped/);
  });

  it("renders gatekeeper as UNKNOWN (not VETOED) when approved is missing", () => {
    // Tri-state defensive rendering: a payload with `gatekeeper: {}` (no
    // `approved` field) must NOT be painted as a veto — that would
    // mislead the operator.
    apiState.debatesResult = {
      data: {
        items: [
          makeDebate({
            id: 911,
            redacted: false,
            recommended_action: "BUY YES",
            analyst_verdicts: [],
            moderator_synthesis: {
              model: "gpt-5",
              recommended_action: "BUY YES",
              synthesis: "Quant + narrative aligned.",
              gatekeeper: { model: "gpt-5" },
            },
          }),
        ],
      },
      isLoading: false,
    };
    render(<SwarmDebates />);
    fireEvent.click(screen.getByTestId("debate-toggle-911"));
    const gk = screen.getByTestId("debate-gatekeeper-911");
    expect(gk.textContent).toMatch(/UNKNOWN/);
    expect(gk.textContent).not.toMatch(/VETOED/);
    expect(gk.textContent).not.toMatch(/APPROVED/);
  });
});
