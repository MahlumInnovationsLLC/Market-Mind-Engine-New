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
  ev_per_trade: number | null;
};

type Totals = {
  open_count: number;
  open_cost_usd: number;
  open_max_loss_usd: number;
  realized_count: number;
  realized_wins: number;
  realized_pnl_usd: number;
  ev_per_trade: number | null;
};

type Bucket = {
  label: string;
  lo: number;
  hi: number;
  is_tail: boolean;
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
    threshold_price: number;
  };
  source?: string;
};

function zeroTotals(): Totals {
  return {
    open_count: 0,
    open_cost_usd: 0,
    open_max_loss_usd: 0,
    realized_count: 0,
    realized_wins: 0,
    realized_pnl_usd: 0,
    ev_per_trade: null,
  };
}

function emptyResponse(): TailRiskResponse {
  const lows = [0, 0.1, 0.25, 0.4, 0.6, 0.75, 0.9];
  const highs = [0.1, 0.25, 0.4, 0.6, 0.75, 0.9, 1.0];
  // Server reports `is_tail=true` only when the WHOLE bucket sits
  // below the $0.20 threshold — only the `<$0.10` bucket qualifies.
  return {
    days: 30,
    buckets: ["<$0.10", "$0.10–$0.25", "$0.25–$0.40", "$0.40–$0.60",
              "$0.60–$0.75", "$0.75–$0.90", "$0.90+"].map((label, i) => ({
      label,
      lo: lows[i],
      hi: highs[i],
      is_tail: highs[i] <= 0.2,
      rows: [] as BucketRow[],
      totals: zeroTotals(),
    })),
    totals: zeroTotals(),
    tail_concentration: {
      tail_max_loss_usd: 0,
      total_max_loss_usd: 0,
      pct: null,
      tone: "empty" as ToneType,
      threshold_price: 0.2,
    },
    source: "positions",
  };
}

function renderTile(props?: { days?: number; period?: number }): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <TailRiskExposure {...(props ?? {})} />
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

  it("respects the dashboard period selector via the `days`/`period` props", async () => {
    mockApiFetch.mockResolvedValueOnce(emptyResponse());
    renderTile({ days: 7, period: 7 });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());
    const url = mockApiFetch.mock.calls[0][0] as string;
    expect(url).toMatch(/days=7(?!\d)/);
    expect(screen.getByText(/RISK · 7D :: TAIL EXPOSURE/)).toBeTruthy();
  });

  it("renders 'NO EXPOSURE' when the server reports no open book", async () => {
    mockApiFetch.mockResolvedValueOnce(emptyResponse());
    renderTile();
    expect(
      await screen.findByTestId("tail-risk-headline-tone-empty"),
    ).toBeTruthy();
    expect(screen.getByTestId("tail-risk-empty")).toBeTruthy();
    // No grand-total row when nothing was open or realized — keeps
    // the empty-state visually clean.
    expect(screen.queryByTestId("tail-risk-totals-row")).toBeNull();
  });

  it("renders a grand-total row at the bottom (parity with the web table)", async () => {
    const r = emptyResponse();
    r.buckets[0].rows = [
      {
        vertical: "sports", open_count: 2,
        open_cost_usd: 800, open_max_loss_usd: 800,
        realized_count: 4, realized_wins: 1, realized_pnl_usd: -1200,
        ev_per_trade: -300,
      },
    ];
    r.buckets[0].totals = {
      open_count: 2, open_cost_usd: 800, open_max_loss_usd: 800,
      realized_count: 4, realized_wins: 1, realized_pnl_usd: -1200,
      ev_per_trade: -300,
    };
    r.totals = { ...r.buckets[0].totals };
    r.tail_concentration = {
      tail_max_loss_usd: 800, total_max_loss_usd: 800,
      pct: 1.0, tone: "red", threshold_price: 0.2,
    };
    mockApiFetch.mockResolvedValueOnce(r);

    renderTile();

    expect(await screen.findByTestId("tail-risk-totals-row")).toBeTruthy();
    const meta = screen.getByTestId("tail-risk-totals-meta");
    const text = meta.props.children
      .flat(Infinity)
      .filter((c: unknown) => typeof c === "string")
      .join("");
    expect(text).toContain("2 open");
    expect(text).toContain("25%");
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
        ev_per_trade: -4218,
      },
    ];
    r.buckets[0].totals = {
      open_count: 1,
      open_cost_usd: 4218.24,
      open_max_loss_usd: 4218.24,
      realized_count: 1,
      realized_wins: 0,
      realized_pnl_usd: -4218,
      ev_per_trade: -4218,
    };
    r.totals = { ...r.buckets[0].totals };
    r.tail_concentration = {
      tail_max_loss_usd: 4218.24,
      total_max_loss_usd: 4218.24,
      pct: 1.0,
      tone: "red",
      threshold_price: 0.2,
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
    // Headline copy uses the server-published threshold ($0.20), NOT
    // the prior hardcoded "$0.25" — architect rejection landed here.
    const detail = screen.getByTestId("tail-risk-headline-detail");
    expect(detail.props.children.join("")).toContain("<$0.20");
    // EV/trade for the Feb-20 bucket is surfaced inline.
    expect(screen.getByTestId("tail-risk-bucket-<$0.10-ev")).toBeTruthy();
  });

  it("classifies amber and green tones from the server response", async () => {
    const r1 = emptyResponse();
    r1.tail_concentration = {
      tail_max_loss_usd: 200,
      total_max_loss_usd: 1000,
      pct: 0.2,
      tone: "amber",
      threshold_price: 0.2,
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
      threshold_price: 0.2,
    };
    r2.totals.open_max_loss_usd = 1000;
    r2.totals.open_count = 1;
    mockApiFetch.mockResolvedValueOnce(r2);
    renderTile();
    expect(
      await screen.findByTestId("tail-risk-headline-tone-green"),
    ).toBeTruthy();
  });

  it("renders the per-vertical breakdown when a bucket has multiple verticals", async () => {
    const r = emptyResponse();
    r.buckets[0].rows = [
      {
        vertical: "sports", open_count: 2,
        open_cost_usd: 800, open_max_loss_usd: 800,
        realized_count: 5, realized_wins: 1, realized_pnl_usd: -1500,
        ev_per_trade: -300,
      },
      {
        vertical: "crypto", open_count: 1,
        open_cost_usd: 50, open_max_loss_usd: 50,
        realized_count: 0, realized_wins: 0, realized_pnl_usd: 0,
        ev_per_trade: null,
      },
    ];
    r.buckets[0].totals = {
      open_count: 3, open_cost_usd: 850, open_max_loss_usd: 850,
      realized_count: 5, realized_wins: 1, realized_pnl_usd: -1500,
      ev_per_trade: -300,
    };
    r.totals = { ...r.buckets[0].totals };
    r.tail_concentration = {
      tail_max_loss_usd: 850, total_max_loss_usd: 850,
      pct: 1.0, tone: "red", threshold_price: 0.2,
    };
    mockApiFetch.mockResolvedValueOnce(r);

    renderTile();

    // Both per-vertical sub-rows render under the bucket.
    expect(
      await screen.findByTestId("tail-risk-cell-<$0.10-sports"),
    ).toBeTruthy();
    expect(screen.getByTestId("tail-risk-cell-<$0.10-crypto")).toBeTruthy();
  });

  it("surfaces a clear error message when the request fails", async () => {
    mockApiFetch.mockRejectedValueOnce(new Error("boom"));
    renderTile();
    expect(await screen.findByTestId("tail-risk-error")).toBeTruthy();
  });
});
