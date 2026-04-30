import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { state } = vi.hoisted(() => ({
  state: {
    placedBy: "all" as "all" | "manual" | "system",
    verticals: [] as string[],
    fetchResponses: [] as Array<{ ok: boolean; status?: number; body?: unknown }>,
    lastUrl: "" as string,
  },
}));

vi.mock("@/lib/api", () => ({
  resolveApiUrl: (u: string) => u,
}));

vi.mock("@/components/dashboard/placed-by-context", () => ({
  usePlacedBy: () => ({ placedBy: state.placedBy, setPlacedBy: vi.fn() }),
}));

vi.mock("@/hooks/use-selected-verticals", () => ({
  useVerticalFilter: () => ({
    selected: state.verticals,
    effectiveVerticals: state.verticals,
    matches: () => true,
    filterActive: state.verticals.length > 0,
    hiddenActive: false,
  }),
}));

import TailRiskExposurePanel from "./tail-risk-exposure-panel";

beforeEach(() => {
  state.placedBy = "all";
  state.verticals = [];
  state.fetchResponses = [];
  state.lastUrl = "";
  global.fetch = vi.fn(async (url: string) => {
    state.lastUrl = url;
    const next = state.fetchResponses.shift() ?? { ok: true, body: emptyBody() };
    return {
      ok: next.ok,
      status: next.status ?? (next.ok ? 200 : 500),
      json: async () => next.body,
    } as Response;
  }) as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function emptyBody() {
  return {
    days: 30,
    buckets: [
      {
        label: "<$0.10", lo: 0.0, hi: 0.1, rows: [],
        totals: zeroTotals(),
      },
      {
        label: "$0.10–$0.25", lo: 0.1, hi: 0.25, rows: [],
        totals: zeroTotals(),
      },
      {
        label: "$0.25–$0.40", lo: 0.25, hi: 0.4, rows: [],
        totals: zeroTotals(),
      },
      {
        label: "$0.40–$0.60", lo: 0.4, hi: 0.6, rows: [],
        totals: zeroTotals(),
      },
      {
        label: "$0.60–$0.75", lo: 0.6, hi: 0.75, rows: [],
        totals: zeroTotals(),
      },
      {
        label: "$0.75–$0.90", lo: 0.75, hi: 0.9, rows: [],
        totals: zeroTotals(),
      },
      {
        label: "$0.90+", lo: 0.9, hi: 1.0, rows: [],
        totals: zeroTotals(),
      },
    ],
    totals: zeroTotals(),
    tail_concentration: {
      tail_max_loss_usd: 0,
      total_max_loss_usd: 0,
      pct: null,
      tone: "empty",
    },
    placed_by: "all",
    verticals: [],
  };
}

function zeroTotals() {
  return {
    open_count: 0,
    open_cost_usd: 0,
    open_max_loss_usd: 0,
    realized_count: 0,
    realized_wins: 0,
    realized_pnl_usd: 0,
  };
}

function renderPanel() {
  // New client per render so cache isolation between tests is total.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TailRiskExposurePanel />
    </QueryClientProvider>,
  );
}

describe("TailRiskExposurePanel", () => {
  it("shows the loading copy before data arrives", async () => {
    // Resolve never — keep the query in flight.
    global.fetch = vi.fn(() => new Promise(() => {})) as unknown as typeof fetch;
    renderPanel();
    expect(await screen.findByTestId("tail-risk-loading")).toBeTruthy();
  });

  it("threads placedBy + vertical CSV into the request URL", async () => {
    state.placedBy = "system";
    state.verticals = ["sports", "crypto"];
    state.fetchResponses = [{ ok: true, body: emptyBody() }];
    renderPanel();
    await waitFor(() => expect(state.lastUrl).toMatch(/tail-risk-exposure/));
    expect(state.lastUrl).toMatch(/days=30/);
    expect(state.lastUrl).toMatch(/placed_by=system/);
    // Verticals are sorted before joining so the cache key is stable.
    expect(state.lastUrl).toMatch(/vertical=crypto%2Csports/);
  });

  it("renders the empty headline as 'NO EXPOSURE' when no open book", async () => {
    state.fetchResponses = [{ ok: true, body: emptyBody() }];
    renderPanel();
    const tone = await screen.findByTestId("tail-risk-headline-tone");
    expect(tone.textContent).toBe("NO EXPOSURE");
    const value = screen.getByTestId("tail-risk-headline-value");
    // No exposure → pct=null renders as the em-dash placeholder.
    expect(value.textContent).toBe("—");
    expect(value.getAttribute("data-tone")).toBe("empty");
  });

  it("renders the bucket table + Feb-20 regression shape and headline tone=red", async () => {
    const body = emptyBody();
    body.buckets[0] = {
      label: "<$0.10", lo: 0.0, hi: 0.1,
      rows: [
        {
          vertical: "sports",
          open_count: 1, open_cost_usd: 4218.24, open_max_loss_usd: 4218.24,
          realized_count: 1, realized_wins: 0, realized_pnl_usd: -4218,
        },
      ],
      totals: {
        open_count: 1, open_cost_usd: 4218.24, open_max_loss_usd: 4218.24,
        realized_count: 1, realized_wins: 0, realized_pnl_usd: -4218,
      },
    };
    body.buckets[3] = {
      label: "$0.40–$0.60", lo: 0.4, hi: 0.6,
      rows: [
        {
          vertical: "sports",
          open_count: 1, open_cost_usd: 5.5, open_max_loss_usd: 5.5,
          realized_count: 2, realized_wins: 1, realized_pnl_usd: 47,
        },
      ],
      totals: {
        open_count: 1, open_cost_usd: 5.5, open_max_loss_usd: 5.5,
        realized_count: 2, realized_wins: 1, realized_pnl_usd: 47,
      },
    };
    body.totals = {
      open_count: 2, open_cost_usd: 4223.74, open_max_loss_usd: 4223.74,
      realized_count: 3, realized_wins: 1, realized_pnl_usd: -4171,
    };
    body.tail_concentration = {
      tail_max_loss_usd: 4218.24,
      total_max_loss_usd: 4223.74,
      pct: 4218.24 / 4223.74,
      tone: "red",
    };
    state.fetchResponses = [{ ok: true, body }];

    renderPanel();

    const tone = await screen.findByTestId("tail-risk-headline-tone");
    expect(tone.textContent).toBe("ELEVATED");
    const value = screen.getByTestId("tail-risk-headline-value");
    expect(value.getAttribute("data-tone")).toBe("red");
    // 99.87% rounded to 1 dp.
    expect(value.textContent).toMatch(/99\.[89]%/);

    // First bucket row has the tail flag and a non-zero max-loss.
    const b0 = screen.getByTestId("tail-risk-bucket-0");
    expect(b0.getAttribute("data-tail")).toBe("true");
    expect(b0.textContent).toContain("$4,218");

    // Mid bucket has tail=false.
    const b3 = screen.getByTestId("tail-risk-bucket-3");
    expect(b3.getAttribute("data-tail")).toBe("false");

    // Vertical breakdown only includes the tail buckets — sports gets
    // the Feb-20 sized number.
    const sports = screen.getByTestId("tail-risk-vertical-sports");
    expect(sports.textContent).toContain("$4,218");
  });

  it("surfaces an error message when the request fails", async () => {
    state.fetchResponses = [{ ok: false, status: 500, body: {} }];
    renderPanel();
    expect(await screen.findByTestId("tail-risk-error")).toBeTruthy();
  });
});
