/**
 * Component tests for the mobile pipeline screen's Kalshi crypto
 * candidates panel.
 *
 * The web operator console renders the realised YES/NO/void outcome,
 * the settled timestamp, and a settled-today/yesterday rollup for
 * Kalshi crypto predictions. The mobile pipeline tab now mirrors that
 * surface so an operator on the go can see the same state without
 * opening a laptop. These tests lock down:
 *
 *   - the settled-today / yesterday rollup rendering
 *   - the realised outcome badge (YES / NO / VOID) on settled rows
 *   - the pending fallback when settlement hasn't happened yet
 *   - the empty-state when the discovery feed has nothing queued
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react-native";
import React from "react";

const mockApiFetch = jest.fn();
const mockUseSafeAreaInsets = jest.fn();

jest.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
}));

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => mockUseSafeAreaInsets(),
  SafeAreaProvider: ({ children }: { children: React.ReactNode }) => children,
}));

import PipelineScreen from "../pipeline";

type CryptoCandidate = {
  ticker: string | null;
  symbol: string | null;
  title: string | null;
  mid_price: number | null;
  close_time: string | null;
  updated_at: string | null;
  settled_at: string | null;
  outcome: string | null;
  settle_price: number | null;
};

type CryptoResponse = {
  count: number;
  items: CryptoCandidate[];
  limit: number;
  settled?: { today: number; yesterday: number } | null;
  error?: string | null;
};

function setApi(crypto: CryptoResponse): void {
  mockApiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith("/api/dashboard/recent-cycles")) {
      return { items: [] };
    }
    if (path.startsWith("/api/dashboard/recent-debates")) {
      return { items: [], delayed: false };
    }
    if (path.startsWith("/api/feeds/kalshi-crypto-candidates")) {
      return crypto;
    }
    throw new Error(`Unexpected apiFetch call: ${path}`);
  });
}

function renderScreen(): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <PipelineScreen />
    </QueryClientProvider>,
  );
}

jest.setTimeout(20_000);

beforeEach(() => {
  jest.clearAllMocks();
  mockUseSafeAreaInsets.mockReturnValue({
    top: 0,
    bottom: 0,
    left: 0,
    right: 0,
  });
});

describe("PipelineScreen Kalshi crypto candidates panel", () => {
  it("renders the settled-today / yesterday rollup", async () => {
    setApi({
      count: 0,
      items: [],
      limit: 25,
      settled: { today: 3, yesterday: 7 },
    });

    renderScreen();

    // Rollup should reflect the API payload exactly so the mobile
    // surface stays in lockstep with the web operator console.
    expect(await screen.findByTestId("kalshi-crypto-settled-today")).toHaveTextContent("3");
    expect(screen.getByTestId("kalshi-crypto-settled-yesterday")).toHaveTextContent("7");
    expect(screen.getByTestId("kalshi-crypto-candidates-empty")).toBeTruthy();
  });

  it("renders the realised outcome and settled timestamp for settled rows", async () => {
    setApi({
      count: 2,
      items: [
        {
          ticker: "KXBTC-26APR2700-T100000",
          symbol: "BTC",
          title: "BTC > 100k by 27 Apr",
          mid_price: 0.42,
          close_time: "2026-04-27T20:00:00+00:00",
          updated_at: "2026-04-27T19:50:00+00:00",
          settled_at: "2026-04-27T20:01:00+00:00",
          outcome: "yes",
          settle_price: 1.0,
        },
        {
          ticker: "KXETH-26APR2700-T5000",
          symbol: "ETH",
          title: "ETH > 5k by 27 Apr",
          mid_price: 0.18,
          close_time: "2026-04-28T20:00:00+00:00",
          updated_at: "2026-04-27T19:55:00+00:00",
          settled_at: null,
          outcome: null,
          settle_price: null,
        },
      ],
      limit: 25,
      settled: { today: 1, yesterday: 0 },
    });

    renderScreen();

    // Settled row: realised outcome badge + the settled timestamp
    // that the kalshi_crypto_settlements settler stamped.
    const outcome0 = await screen.findByTestId("kalshi-crypto-candidate-0-outcome");
    expect(outcome0).toHaveTextContent("YES");
    const settledAt0 = screen.getByTestId("kalshi-crypto-candidate-0-settled-at");
    expect(settledAt0).toHaveTextContent("settled 2026-04-27 20:01:00Z");
    expect(screen.getByTestId("kalshi-crypto-candidate-0-settle-price")).toHaveTextContent(
      "settle 100¢",
    );

    // Pending row: PENDING badge and the row's last-updated timestamp
    // (no realised settle price yet).
    const outcome1 = screen.getByTestId("kalshi-crypto-candidate-1-outcome");
    expect(outcome1).toHaveTextContent("PENDING");
    expect(screen.getByTestId("kalshi-crypto-candidate-1-settled-at")).toHaveTextContent(
      "updated 2026-04-27 19:55:00Z",
    );
    expect(screen.getByTestId("kalshi-crypto-candidate-1-settle-price")).toHaveTextContent(
      "settle —",
    );
  });

  it("renders a VOID badge when the settler stamped no realised side", async () => {
    setApi({
      count: 1,
      items: [
        {
          ticker: "KXBTC-26APR2700-T200000",
          symbol: "BTC",
          title: "BTC > 200k by 27 Apr",
          mid_price: 0.01,
          close_time: "2026-04-27T20:00:00+00:00",
          updated_at: "2026-04-27T19:50:00+00:00",
          settled_at: "2026-04-27T20:05:00+00:00",
          outcome: "void",
          settle_price: 0.0,
        },
      ],
      limit: 25,
      settled: { today: 1, yesterday: 0 },
    });

    renderScreen();

    const outcome = await screen.findByTestId("kalshi-crypto-candidate-0-outcome");
    expect(outcome).toHaveTextContent("VOID");
  });
});
