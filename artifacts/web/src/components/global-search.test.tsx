import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { apiState } = vi.hoisted(() => ({
  apiState: {
    result: { data: undefined, isFetching: false } as {
      data: unknown;
      isFetching: boolean;
    },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  useSearchAll: () => apiState.result,
  getSearchAllQueryKey: (params: unknown) => ["search-all", params],
}));

beforeAll(() => {
  // jsdom hasn't shipped these primitives that cmdk / radix-dialog rely on.
  if (typeof Element.prototype.scrollIntoView !== "function") {
    Element.prototype.scrollIntoView = function () {
      return undefined;
    };
  }
  if (typeof window.PointerEvent === "undefined") {
    // @ts-expect-error -- minimal shim
    window.PointerEvent = class extends MouseEvent {};
  }
  if (typeof Element.prototype.hasPointerCapture !== "function") {
    // @ts-expect-error -- minimal shim
    Element.prototype.hasPointerCapture = () => false;
  }
  if (typeof globalThis.ResizeObserver === "undefined") {
    class RO {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    // @ts-expect-error -- minimal shim
    globalThis.ResizeObserver = RO;
  }
});

import GlobalSearch from "./global-search";

beforeEach(() => {
  apiState.result = {
    data: {
      trades: [
        {
          id: 7,
          market_id: "MKT_X",
          market_title: "Sample market",
          side: "yes",
          action: "buy",
          executed_at: "2026-04-24T12:00:00Z",
        },
      ],
      debates: [
        {
          id: 99,
          matchup: "A vs B",
          league: "NBA",
          recommended_action: "BUY",
          consensus_probability: 0.6,
          created_at: "2026-04-24T12:00:00Z",
        },
      ],
      cycles: [
        {
          id: 42,
          vertical: "nba",
          picks_evaluated: 5,
          trades_placed: 1,
          started_at: "2026-04-24T12:00:00Z",
        },
      ],
    },
    isFetching: false,
  };
});

afterEach(() => {
  cleanup();
});

function renderSearch(open: boolean = true) {
  const hook = memoryLocation({ path: "/somewhere", record: true });
  const utils = render(
    <Router hook={hook.hook}>
      <GlobalSearch />
    </Router>,
  );
  if (open) {
    // Open the palette via the documented custom event.
    fireEvent(window, new Event("axiom:open-search"));
  }
  return { ...utils, history: hook.history };
}

describe("GlobalSearch navigation targets", () => {
  it("routes trade hits to /trading with a focus param matching the trade row", () => {
    const { history } = renderSearch();
    // Type enough to trigger the debounced query (mock returns immediately).
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "sample" } });

    const item = screen.getByTestId("search-trade-7");
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe("/trading?focus=trade-7");
  });

  it("routes debate hits to /app with a focus param matching the debate row", () => {
    const { history } = renderSearch();
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "team" } });

    const item = screen.getByTestId("search-debate-99");
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe("/app?focus=debate-99");
  });

  it("routes cycle hits to /app with a focus param matching the cycle row", () => {
    const { history } = renderSearch();
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "cycle" } });

    const item = screen.getByTestId("search-cycle-42");
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe("/app?focus=cycle-42");
  });
});
