import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { authState } = vi.hoisted(() => ({
  authState: {
    isSignedIn: true as boolean,
    token: "test-token" as string | null,
  },
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => ({
    isSignedIn: authState.isSignedIn,
    getToken: () => Promise.resolve(authState.token),
  }),
}));

// recharts' ResponsiveContainer needs a sized parent which jsdom can't
// provide; the panel only cares that the chart wrapper div renders, so
// stub the recharts pieces it pulls in to plain passthroughs.  This keeps
// the test focused on CryptoBacktestPanel's data-flow + branch logic.
vi.mock("recharts", () => {
  const Stub = ({ children }: { children?: React.ReactNode }) =>
    children ?? null;
  return {
    CartesianGrid: () => null,
    Line: () => null,
    LineChart: Stub,
    ResponsiveContainer: Stub,
    Tooltip: () => null,
    XAxis: () => null,
    YAxis: () => null,
  };
});

// Stub the api-client-react module — admin.tsx imports several hooks at
// the top level for other panels.  CryptoBacktestPanel itself does not
// touch them, but the imports must resolve so the page module loads.
vi.mock("@workspace/api-client-react", () => {
  const noopHook = () => ({
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    refetch: () => undefined,
    mutate: () => undefined,
    mutateAsync: () => Promise.resolve(undefined),
  });
  const noopKey = () => ["noop"];
  return new Proxy(
    {},
    {
      get: (_target, prop) => {
        const name = String(prop);
        if (name.startsWith("get") && name.endsWith("QueryKey")) return noopKey;
        if (name.startsWith("use")) return noopHook;
        // Constants (e.g. enum-like objects) just resolve to an empty obj.
        return {};
      },
    },
  );
});

// The websocket provider opens a real WS in jsdom otherwise.
vi.mock("@/components/realtime/websocket-provider", () => ({
  usePipelineSubscription: () => undefined,
}));

import { CryptoBacktestPanel } from "./admin";

type FetchMock = ReturnType<typeof vi.fn>;
let originalFetch: typeof fetch;
let fetchMock: FetchMock;

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

function renderPanel() {
  // Each test gets its own QueryClient so cache state never bleeds
  // between cases; retries off so failures surface immediately.
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <CryptoBacktestPanel />
    </QueryClientProvider>,
  );
}

function urlOf(call: unknown[]): string {
  const first = call[0];
  if (typeof first === "string") return first;
  if (first instanceof URL) return first.toString();
  if (first instanceof Request) return first.url;
  return String(first);
}

beforeEach(() => {
  authState.isSignedIn = true;
  authState.token = "test-token";
  originalFetch = globalThis.fetch;
  fetchMock = vi.fn();
  globalThis.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
  cleanup();
});

describe("CryptoBacktestPanel — history trend chart", () => {
  it("renders the loading skeleton while the history query is in flight", () => {
    // History fetch never resolves -> stays in `isLoading` for the
    // duration of the assertion.
    fetchMock.mockImplementation(
      () => new Promise<Response>(() => undefined),
    );

    renderPanel();

    expect(
      screen.getByTestId("crypto-backtest-history-loading"),
    ).toBeTruthy();
    // The other two history branches must NOT render simultaneously.
    expect(screen.queryByTestId("crypto-backtest-history-empty")).toBeNull();
    expect(screen.queryByTestId("crypto-backtest-history-chart")).toBeNull();
    expect(screen.queryByTestId("crypto-backtest-history-error")).toBeNull();
  });

  it("calls /api/admin/crypto/backtest/history with the currently-selected symbol + timeframe", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ symbol: "BTC", timeframe: "1h", items: [] }),
    );

    renderPanel();

    // Wait for the history fetch to fire — apiFetch awaits getToken
    // first, so the call lands on the next microtask tick.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = urlOf(fetchMock.mock.calls[0]);
    expect(url).toMatch(/^\/api\/admin\/crypto\/backtest\/history\?/);
    // BTC + 1h are the panel's default selector values.
    expect(url).toMatch(/[?&]symbol=BTC(?:&|$)/);
    expect(url).toMatch(/[?&]timeframe=1h(?:&|$)/);
    expect(url).toMatch(/[?&]limit=50(?:&|$)/);
  });

  it("re-issues the history query with the new pair when the operator changes the symbol selector", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ symbol: "BTC", timeframe: "1h", items: [] }),
    );

    renderPanel();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(urlOf(fetchMock.mock.calls[0])).toMatch(/symbol=BTC/);

    // Flip to ETH — queryKey depends on `symbol` so a new fetch must fire.
    fireEvent.change(screen.getByTestId("select-crypto-backtest-symbol"), {
      target: { value: "ETH" },
    });

    await waitFor(() => {
      const ethCalls = fetchMock.mock.calls.filter((c) =>
        urlOf(c).includes("symbol=ETH"),
      );
      expect(ethCalls.length).toBeGreaterThan(0);
    });
  });

  it("renders the empty-state fallback when the history payload has no items", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ symbol: "BTC", timeframe: "1h", items: [] }),
    );

    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId("crypto-backtest-history-empty")).toBeTruthy(),
    );
    expect(screen.queryByTestId("crypto-backtest-history-loading")).toBeNull();
    expect(screen.queryByTestId("crypto-backtest-history-error")).toBeNull();
    expect(screen.queryByTestId("crypto-backtest-history-chart")).toBeNull();
  });

  it("renders the chart wrapper when the history payload returns at least one row", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        symbol: "BTC",
        timeframe: "1h",
        items: [
          {
            symbol: "BTC",
            timeframe: "1h",
            lookback: 14,
            n_evaluated: 240,
            accuracy: 0.55,
            brier_score: 0.21,
            log_loss: 0.62,
            started_at: "2026-04-24T10:00:00Z",
            completed_at: "2026-04-24T10:00:01Z",
          },
          {
            symbol: "BTC",
            timeframe: "1h",
            lookback: 14,
            n_evaluated: 245,
            accuracy: 0.57,
            brier_score: 0.20,
            log_loss: 0.61,
            started_at: "2026-04-25T10:00:00Z",
            completed_at: "2026-04-25T10:00:01Z",
          },
        ],
      }),
    );

    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId("crypto-backtest-history-chart")).toBeTruthy(),
    );
    // Header counter reflects the number of rows the panel was handed.
    expect(
      screen.getByTestId("crypto-backtest-history").textContent,
    ).toMatch(/2 runs/);
    expect(screen.queryByTestId("crypto-backtest-history-empty")).toBeNull();
    expect(screen.queryByTestId("crypto-backtest-history-error")).toBeNull();
    expect(screen.queryByTestId("crypto-backtest-history-loading")).toBeNull();
  });

  it("renders the error fallback when the history endpoint returns a 5xx", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "boom" }), {
        status: 500,
        headers: { "content-type": "application/json" },
      }),
    );

    renderPanel();

    await waitFor(() =>
      expect(
        screen.getByTestId("crypto-backtest-history-error"),
      ).toBeTruthy(),
    );
    expect(
      screen.getByTestId("crypto-backtest-history-error").textContent,
    ).toMatch(/Failed to load backtest history/);
    expect(screen.queryByTestId("crypto-backtest-history-empty")).toBeNull();
    expect(screen.queryByTestId("crypto-backtest-history-chart")).toBeNull();
    expect(screen.queryByTestId("crypto-backtest-history-loading")).toBeNull();
  });

  it("refetches the history endpoint after a successful Run completes", async () => {
    // Route requests by URL: the backtest endpoint returns a single
    // result row, the history endpoint always returns an empty list
    // (we only care that it gets called *again* after Run completes).
    fetchMock.mockImplementation(async (input: unknown) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input instanceof Request
              ? input.url
              : String(input);
      if (url.includes("/api/admin/crypto/backtest/history")) {
        return jsonResponse({ symbol: "BTC", timeframe: "1h", items: [] });
      }
      if (url.includes("/api/admin/crypto/backtest")) {
        return jsonResponse({
          symbol: "BTC",
          timeframe: "1h",
          lookback: 14,
          n_evaluated: 100,
          accuracy: 0.55,
          brier_score: 0.21,
          log_loss: 0.62,
          reliability: [],
          started_at: "2026-04-26T10:00:00Z",
          completed_at: "2026-04-26T10:00:01Z",
        });
      }
      throw new Error(`unexpected fetch url: ${url}`);
    });

    renderPanel();

    // Wait for the initial history fetch to land.
    await waitFor(() => {
      const historyCalls = fetchMock.mock.calls.filter((c) =>
        urlOf(c).includes("/api/admin/crypto/backtest/history"),
      );
      expect(historyCalls.length).toBeGreaterThanOrEqual(1);
    });
    const initialHistoryCalls = fetchMock.mock.calls.filter((c) =>
      urlOf(c).includes("/api/admin/crypto/backtest/history"),
    ).length;

    // Click Run -> kicks the backtest query.  Once it resolves, the
    // history queryKey changes (it includes `data?.completed_at`),
    // which causes the history query to refetch.
    fireEvent.click(screen.getByTestId("button-crypto-backtest-run"));

    await waitFor(() => {
      const backtestCalls = fetchMock.mock.calls.filter(
        (c) =>
          urlOf(c).includes("/api/admin/crypto/backtest") &&
          !urlOf(c).includes("/history"),
      );
      expect(backtestCalls.length).toBeGreaterThanOrEqual(1);
    });

    await waitFor(() => {
      const historyCallsAfter = fetchMock.mock.calls.filter((c) =>
        urlOf(c).includes("/api/admin/crypto/backtest/history"),
      ).length;
      expect(historyCallsAfter).toBeGreaterThan(initialHistoryCalls);
    });
  });
});
