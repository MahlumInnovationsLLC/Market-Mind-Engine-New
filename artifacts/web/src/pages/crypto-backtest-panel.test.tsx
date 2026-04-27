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

  it("renders the result block and reliability rows after a successful Run", async () => {
    // Route by URL: history stays empty (not the focus here) and the
    // backtest endpoint returns a populated result with two reliability
    // bins so we can assert the table renders one row per bin.
    fetchMock.mockImplementation(async (input: unknown) => {
      const url = urlOf([input]);
      if (url.includes("/api/admin/crypto/backtest/history")) {
        return jsonResponse({ symbol: "ETH", timeframe: "15m", items: [] });
      }
      if (url.includes("/api/admin/crypto/backtest")) {
        return jsonResponse({
          symbol: "ETH",
          timeframe: "15m",
          lookback: 30,
          n_evaluated: 1234,
          accuracy: 0.6125,
          brier_score: 0.1875,
          log_loss: 0.5432,
          reliability: [
            {
              bin_lower: 0.0,
              bin_upper: 0.5,
              mean_predicted: 0.3,
              mean_actual: 0.32,
              count: 80,
            },
            {
              bin_lower: 0.5,
              bin_upper: 1.0,
              mean_predicted: 0.7,
              mean_actual: 0.71,
              count: 120,
            },
          ],
          started_at: "2026-04-26T10:00:00Z",
          completed_at: "2026-04-26T10:00:01Z",
        });
      }
      throw new Error(`unexpected fetch url: ${url}`);
    });

    renderPanel();

    // Commit non-default params before clicking Run so the assertion
    // proves the selector values flow into the request URL.
    fireEvent.change(screen.getByTestId("select-crypto-backtest-symbol"), {
      target: { value: "ETH" },
    });
    fireEvent.change(screen.getByTestId("select-crypto-backtest-timeframe"), {
      target: { value: "15m" },
    });
    fireEvent.change(screen.getByTestId("input-crypto-backtest-days"), {
      target: { value: "30" },
    });

    fireEvent.click(screen.getByTestId("button-crypto-backtest-run"));

    // The result block + metric tiles should render once the backtest
    // request resolves.
    await waitFor(() =>
      expect(screen.getByTestId("crypto-backtest-result")).toBeTruthy(),
    );

    const backtestCall = fetchMock.mock.calls.find(
      (c) =>
        urlOf(c).includes("/api/admin/crypto/backtest") &&
        !urlOf(c).includes("/history"),
    );
    expect(backtestCall).toBeTruthy();
    const backtestUrl = urlOf(backtestCall!);
    expect(backtestUrl).toMatch(/[?&]symbol=ETH(?:&|$)/);
    expect(backtestUrl).toMatch(/[?&]timeframe=15m(?:&|$)/);
    expect(backtestUrl).toMatch(/[?&]days=30(?:&|$)/);

    expect(screen.getByTestId("crypto-backtest-pair").textContent).toMatch(
      /ETH.*15m/,
    );
    expect(screen.getByTestId("crypto-backtest-n").textContent).toMatch(
      /1,234/,
    );
    expect(
      screen.getByTestId("crypto-backtest-accuracy").textContent,
    ).toMatch(/0\.6125/);
    expect(screen.getByTestId("crypto-backtest-brier").textContent).toMatch(
      /0\.1875/,
    );

    // One <tr> per reliability bin — the per-bin testids are indexed.
    expect(screen.getByTestId("crypto-backtest-bin-0")).toBeTruthy();
    expect(screen.getByTestId("crypto-backtest-bin-1")).toBeTruthy();
    expect(screen.queryByTestId("crypto-backtest-bin-2")).toBeNull();
    expect(
      screen.getByTestId("crypto-backtest-bin-0").textContent,
    ).toMatch(/0\.00.*0\.50/);
    // The empty-state row uses colSpan=4 + a sentinel string; assert it
    // is *not* present when bins exist.
    expect(
      screen.getByTestId("crypto-backtest-reliability").textContent,
    ).not.toMatch(/No populated reliability bins\./);
  });

  it("disables the Run button and switches its label while a backtest is in flight, then flips to Re-run", async () => {
    // Hold the backtest request open so we can observe the in-flight
    // state; let the history call resolve immediately so it doesn't
    // monopolise the button's `isFetching` flag.  The backtest query
    // is the one wired to the Run button — see `useQuery` at line 4548.
    let resolveBacktest: (value: Response) => void = () => undefined;
    const backtestPending = new Promise<Response>((resolve) => {
      resolveBacktest = resolve;
    });
    fetchMock.mockImplementation(async (input: unknown) => {
      const url = urlOf([input]);
      if (url.includes("/api/admin/crypto/backtest/history")) {
        return jsonResponse({ symbol: "BTC", timeframe: "1h", items: [] });
      }
      if (url.includes("/api/admin/crypto/backtest")) {
        return backtestPending;
      }
      throw new Error(`unexpected fetch url: ${url}`);
    });

    renderPanel();

    // Initial idle state — label is "Run", button enabled.
    const runButton = screen.getByTestId(
      "button-crypto-backtest-run",
    ) as HTMLButtonElement;
    expect(runButton.disabled).toBe(false);
    expect(runButton.textContent).toMatch(/^Run$/);

    fireEvent.click(runButton);

    // While the backtest is in flight the button is disabled and shows
    // a spinner-style label.
    await waitFor(() => {
      const btn = screen.getByTestId(
        "button-crypto-backtest-run",
      ) as HTMLButtonElement;
      expect(btn.disabled).toBe(true);
      expect(btn.textContent).toMatch(/Running/);
    });

    // Resolve the backtest so React Query transitions out of fetching.
    resolveBacktest(
      jsonResponse({
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
      }),
    );

    // Once params are committed and the run is no longer in flight the
    // label switches to "Re-run".
    await waitFor(() => {
      const btn = screen.getByTestId(
        "button-crypto-backtest-run",
      ) as HTMLButtonElement;
      expect(btn.disabled).toBe(false);
      expect(btn.textContent).toMatch(/^Re-run$/);
    });

    // The optional Refresh button only shows after a committed run.
    expect(
      screen.getByTestId("button-crypto-backtest-refresh"),
    ).toBeTruthy();
  });

  it("renders the error banner when the backtest request fails without breaking the rest of the panel", async () => {
    fetchMock.mockImplementation(async (input: unknown) => {
      const url = urlOf([input]);
      if (url.includes("/api/admin/crypto/backtest/history")) {
        return jsonResponse({ symbol: "BTC", timeframe: "1h", items: [] });
      }
      if (url.includes("/api/admin/crypto/backtest")) {
        return new Response(JSON.stringify({ detail: "kaboom" }), {
          status: 500,
          headers: { "content-type": "application/json" },
        });
      }
      throw new Error(`unexpected fetch url: ${url}`);
    });

    renderPanel();

    fireEvent.click(screen.getByTestId("button-crypto-backtest-run"));

    await waitFor(() =>
      expect(screen.getByTestId("crypto-backtest-error")).toBeTruthy(),
    );

    expect(
      screen.getByTestId("crypto-backtest-error").textContent,
    ).toMatch(/Failed to run backtest/);

    // The result block must NOT render when the backtest errored, but
    // the rest of the panel — selectors, Run button, history widget —
    // should still be present.
    expect(screen.queryByTestId("crypto-backtest-result")).toBeNull();
    expect(
      screen.getByTestId("select-crypto-backtest-symbol"),
    ).toBeTruthy();
    expect(
      screen.getByTestId("select-crypto-backtest-timeframe"),
    ).toBeTruthy();
    expect(screen.getByTestId("input-crypto-backtest-days")).toBeTruthy();
    expect(screen.getByTestId("button-crypto-backtest-run")).toBeTruthy();
    expect(screen.getByTestId("crypto-backtest-history")).toBeTruthy();
  });

  it("renders the empty-state row inside the reliability table when reliability is []", async () => {
    fetchMock.mockImplementation(async (input: unknown) => {
      const url = urlOf([input]);
      if (url.includes("/api/admin/crypto/backtest/history")) {
        return jsonResponse({ symbol: "BTC", timeframe: "1h", items: [] });
      }
      if (url.includes("/api/admin/crypto/backtest")) {
        return jsonResponse({
          symbol: "BTC",
          timeframe: "1h",
          lookback: 14,
          // Non-zero `n_evaluated` -> the "no populated reliability
          // bins" sentinel is the one that renders (the alternate
          // sentinel is gated on n_evaluated === 0).
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

    fireEvent.click(screen.getByTestId("button-crypto-backtest-run"));

    await waitFor(() =>
      expect(screen.getByTestId("crypto-backtest-reliability")).toBeTruthy(),
    );

    expect(
      screen.getByTestId("crypto-backtest-reliability").textContent,
    ).toMatch(/No populated reliability bins\./);
    // No per-bin rows when the array is empty.
    expect(screen.queryByTestId("crypto-backtest-bin-0")).toBeNull();
  });

  it("renders the n_evaluated=0 backfill hint inside the reliability table when no bars matched", async () => {
    fetchMock.mockImplementation(async (input: unknown) => {
      const url = urlOf([input]);
      if (url.includes("/api/admin/crypto/backtest/history")) {
        return jsonResponse({ symbol: "BTC", timeframe: "1h", items: [] });
      }
      if (url.includes("/api/admin/crypto/backtest")) {
        return jsonResponse({
          symbol: "BTC",
          timeframe: "1h",
          lookback: 14,
          n_evaluated: 0,
          accuracy: 0,
          brier_score: 0,
          log_loss: 0,
          reliability: [],
          started_at: "2026-04-26T10:00:00Z",
          completed_at: "2026-04-26T10:00:01Z",
        });
      }
      throw new Error(`unexpected fetch url: ${url}`);
    });

    renderPanel();

    fireEvent.click(screen.getByTestId("button-crypto-backtest-run"));

    await waitFor(() =>
      expect(screen.getByTestId("crypto-backtest-reliability")).toBeTruthy(),
    );

    expect(
      screen.getByTestId("crypto-backtest-reliability").textContent,
    ).toMatch(/backfill OHLCV or widen the days range/);
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
