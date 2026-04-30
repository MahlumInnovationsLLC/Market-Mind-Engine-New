/**
 * Component tests for the mobile tail-risk exposure tile (task #744).
 *
 * Locks down the rendering branches that mirror the web tile: the
 * server-classified tone (RED / WATCH / CONTAINED / NO EXPOSURE), the
 * Feb-20 NBA bucket regression, and the empty / loading / error
 * states.  Also asserts the URL is built with the operator's filters
 * (placedBy + sorted vertical CSV) so a regression in the cache-key
 * shape would turn this red instead of slipping into production.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react-native";
import React from "react";

const mockApiFetch = jest.fn();

jest.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
  ApiError: class ApiError extends Error {},
}));

jest.mock("@/lib/placed-by-filter", () => ({
  usePlacedBy: () => ({ placedBy: "system", setPlacedBy: jest.fn() }),
}));

jest.mock("@/lib/vertical-filter", () => ({
  useSelectedVerticals: () => ({
    selected: ["sports", "crypto"],
    toggle: jest.fn(),
    clear: jest.fn(),
    matches: () => true,
    filterActive: true,
    hiddenActive: false,
  }),
  // Mirrors the production behavior — non-admin selection with no
  // hidden-vertical suppression so the URL contract test asserts
  // exactly the operator's chip selection makes it onto the wire.
  useEffectiveVerticals: () => ["sports", "crypto"],
}));

import TailRiskExposure from "../TailRiskExposure";

type ToneType = "red" | "amber" | "green" | "empty";

type BucketRow = {
  vertical: string;
  open_count: number;
  open_cost_usd: number;
  open_max_loss_usd: number;
  realized_count: number;
  realized_wins: number;
  realized_pnl_usd: number;
};

type Totals = {
  open_count: number;
  open_cost_usd: number;
  open_max_loss_usd: number;
  realized_count: number;
  realized_wins: number;
  realized_pnl_usd: number;
};

type Bucket = {
  label: string;
  lo: number;
  hi: number;
  rows: BucketRow[];
  totals: Totals;
};

type TailRiskResponse = {
  days: number;
  buckets: Bucket[];
  totals: Totals;
  tail_concentration: {
    tail_max_loss_usd: number;
    total_max_loss_usd: number;
    pct: number | null;
    tone: ToneType;
  };
};

function zeroTotals(): Totals {
  return {
    open_count: 0,
    open_cost_usd: 0,
    open_max_loss_usd: 0,
    realized_count: 0,
    realized_wins: 0,
    realized_pnl_usd: 0,
  };
}

function emptyResponse(): TailRiskResponse {
  return {
    days: 30,
    buckets: [
      ...["<$0.10", "$0.10–$0.25", "$0.25–$0.40", "$0.40–$0.60",
          "$0.60–$0.75", "$0.75–$0.90", "$0.90+"].map((label, i) => ({
        label,
        lo: i === 0 ? 0 : [0, 0.1, 0.25, 0.4, 0.6, 0.75, 0.9][i],
        hi: [0.1, 0.25, 0.4, 0.6, 0.75, 0.9, 1.0][i],
        rows: [] as BucketRow[],
        totals: zeroTotals(),
      })),
    ],
    totals: zeroTotals(),
    tail_concentration: {
      tail_max_loss_usd: 0,
      total_max_loss_usd: 0,
      pct: null,
      tone: "empty" as ToneType,
    },
  };
}

function renderTile(): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TailRiskExposure />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe("TailRiskExposure (mobile)", () => {
  it("threads placedBy + sorted vertical CSV into the request URL", async () => {
    mockApiFetch.mockResolvedValueOnce(emptyResponse());
    renderTile();
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());
    const url = mockApiFetch.mock.calls[0][0] as string;
    expect(url).toMatch(/^\/api\/public\/tail-risk-exposure\?/);
    expect(url).toMatch(/days=30/);
    expect(url).toMatch(/placed_by=system/);
    // Verticals sorted alphabetically before joining → stable cache key.
    expect(url).toMatch(/vertical=crypto%2Csports/);
  });

  it("renders 'NO EXPOSURE' when the server reports no open book", async () => {
    mockApiFetch.mockResolvedValueOnce(emptyResponse());
    renderTile();
    expect(
      await screen.findByTestId("tail-risk-headline-tone-empty"),
    ).toBeTruthy();
    expect(screen.getByTestId("tail-risk-empty")).toBeTruthy();
  });

  it("renders the Feb-20 NBA regression bucket and red headline tone", async () => {
    const r = emptyResponse();
    r.buckets[0].rows = [
      {
        vertical: "sports",
        open_count: 1,
        open_cost_usd: 4218.24,
        open_max_loss_usd: 4218.24,
        realized_count: 1,
        realized_wins: 0,
        realized_pnl_usd: -4218,
      },
    ];
    r.buckets[0].totals = {
      open_count: 1,
      open_cost_usd: 4218.24,
      open_max_loss_usd: 4218.24,
      realized_count: 1,
      realized_wins: 0,
      realized_pnl_usd: -4218,
    };
    r.totals = { ...r.buckets[0].totals };
    r.tail_concentration = {
      tail_max_loss_usd: 4218.24,
      total_max_loss_usd: 4218.24,
      pct: 1.0,
      tone: "red",
    };
    mockApiFetch.mockResolvedValueOnce(r);

    renderTile();

    expect(
      await screen.findByTestId("tail-risk-headline-tone-red"),
    ).toBeTruthy();
    // Bucket testID encodes the label for stable selection.
    expect(screen.getByTestId("tail-risk-bucket-<$0.10")).toBeTruthy();
    // Other buckets with no activity are filtered out of the mobile view.
    expect(screen.queryByTestId("tail-risk-bucket-$0.40–$0.60")).toBeNull();
  });

  it("classifies amber and green tones from the server response", async () => {
    const r1 = emptyResponse();
    r1.tail_concentration = {
      tail_max_loss_usd: 200,
      total_max_loss_usd: 1000,
      pct: 0.2,
      tone: "amber",
    };
    r1.totals.open_max_loss_usd = 1000;
    r1.totals.open_count = 1;
    mockApiFetch.mockResolvedValueOnce(r1);
    const { unmount } = renderTile();
    expect(
      await screen.findByTestId("tail-risk-headline-tone-amber"),
    ).toBeTruthy();
    unmount();

    const r2 = emptyResponse();
    r2.tail_concentration = {
      tail_max_loss_usd: 50,
      total_max_loss_usd: 1000,
      pct: 0.05,
      tone: "green",
    };
    r2.totals.open_max_loss_usd = 1000;
    r2.totals.open_count = 1;
    mockApiFetch.mockResolvedValueOnce(r2);
    renderTile();
    expect(
      await screen.findByTestId("tail-risk-headline-tone-green"),
    ).toBeTruthy();
  });

  it("surfaces a clear error message when the request fails", async () => {
    mockApiFetch.mockRejectedValueOnce(new Error("boom"));
    renderTile();
    expect(await screen.findByTestId("tail-risk-error")).toBeTruthy();
  });
});
