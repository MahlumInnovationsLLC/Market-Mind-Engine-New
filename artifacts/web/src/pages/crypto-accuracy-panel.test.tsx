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

// Same recharts stub the sibling crypto-backtest-panel test uses — admin.tsx
// pulls recharts in at the top level even though CryptoAccuracyPanel itself
// doesn't render any charts.
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

// Same proxy-mock as the sibling crypto-backtest-panel test — admin.tsx
// imports several @workspace/api-client-react hooks at the top level for
// other panels which CryptoAccuracyPanel doesn't use.  The mock just
// has to make those imports resolve.
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
        return {};
      },
    },
  );
});

vi.mock("@/components/realtime/websocket-provider", () => ({
  usePipelineSubscription: () => undefined,
}));

import { CryptoAccuracyPanel } from "./admin";

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
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <CryptoAccuracyPanel />
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

describe("CryptoAccuracyPanel", () => {
  it("calls /api/admin/crypto/accuracy on mount", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ league: "CRYPTO", windows: [] }),
    );

    renderPanel();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const url = urlOf(fetchMock.mock.calls[0]);
    expect(url).toBe("/api/admin/crypto/accuracy");
  });

  it("renders a loading skeleton while the fetch is in flight", () => {
    fetchMock.mockImplementation(
      () => new Promise<Response>(() => undefined),
    );

    renderPanel();

    expect(screen.getByTestId("crypto-accuracy-loading")).toBeTruthy();
    expect(screen.queryByTestId("crypto-accuracy-empty")).toBeNull();
    expect(screen.queryByTestId("crypto-accuracy-error")).toBeNull();
  });

  it("renders the empty-state when the report has no windows", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ league: "CRYPTO", windows: [] }),
    );

    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId("crypto-accuracy-empty")).toBeTruthy(),
    );
  });

  it("renders the error fallback when the endpoint returns a 5xx", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "boom" }), {
        status: 500,
        headers: { "content-type": "application/json" },
      }),
    );

    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId("crypto-accuracy-error")).toBeTruthy(),
    );
    expect(
      screen.getByTestId("crypto-accuracy-error").textContent,
    ).toMatch(/Failed to load accuracy report/);
  });

  it("renders summary tiles + per-(symbol, timeframe) slice table for each window", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        league: "CRYPTO",
        windows: [
          {
            window_hours: 24,
            n_settled: 12,
            n_void: 1,
            n_unscored: 2,
            overall: {
              symbol: null,
              timeframe: null,
              n: 9,
              accuracy: 0.6667,
              brier: 0.21,
              log_loss: 0.62,
              yes_count: 5,
              no_count: 4,
            },
            slices: [
              {
                symbol: "BTC",
                timeframe: "1h",
                n: 6,
                accuracy: 0.6667,
                brier: 0.2,
                log_loss: 0.6,
                yes_count: 4,
                no_count: 2,
              },
              {
                symbol: "ETH",
                timeframe: "5m",
                n: 3,
                accuracy: 0.6667,
                brier: 0.23,
                log_loss: 0.65,
                yes_count: 1,
                no_count: 2,
              },
            ],
          },
          {
            window_hours: 168,
            n_settled: 0,
            n_void: 0,
            n_unscored: 0,
            overall: {
              symbol: null,
              timeframe: null,
              n: 0,
              accuracy: null,
              brier: null,
              log_loss: null,
              yes_count: 0,
              no_count: 0,
            },
            slices: [],
          },
        ],
      }),
    );

    renderPanel();

    // 24h window — populated
    await waitFor(() =>
      expect(screen.getByTestId("crypto-accuracy-window-24")).toBeTruthy(),
    );
    expect(screen.getByTestId("crypto-accuracy-n-24").textContent).toMatch(
      /^9$/,
    );
    expect(
      screen.getByTestId("crypto-accuracy-accuracy-24").textContent,
    ).toMatch(/0\.6667/);
    expect(
      screen.getByTestId("crypto-accuracy-brier-24").textContent,
    ).toMatch(/0\.2100/);
    expect(
      screen.getByTestId("crypto-accuracy-log-loss-24").textContent,
    ).toMatch(/0\.6200/);
    // Coverage counters (void / unscored) are visible.
    expect(
      screen.getByTestId("crypto-accuracy-void-24").textContent,
    ).toMatch(/void 1/);
    expect(
      screen.getByTestId("crypto-accuracy-unscored-24").textContent,
    ).toMatch(/unscored 2/);
    // Per-(symbol, timeframe) rows render.
    expect(screen.getByTestId("crypto-accuracy-slice-24-0")).toBeTruthy();
    expect(
      screen.getByTestId("crypto-accuracy-slice-24-0").textContent,
    ).toMatch(/BTC · 1h/);
    expect(screen.getByTestId("crypto-accuracy-slice-24-1")).toBeTruthy();
    expect(
      screen.getByTestId("crypto-accuracy-slice-24-1").textContent,
    ).toMatch(/ETH · 5m/);

    // 7d window — empty bucket falls back to "no scored rows" copy and
    // metric tiles render the em-dash placeholder rather than zero.
    expect(screen.getByTestId("crypto-accuracy-window-168")).toBeTruthy();
    expect(
      screen.getByTestId("crypto-accuracy-accuracy-168").textContent,
    ).toMatch(/—/);
    expect(
      screen.getByTestId("crypto-accuracy-slices-empty-168"),
    ).toBeTruthy();
  });

  it("re-fetches when the operator clicks Refresh", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ league: "CRYPTO", windows: [] }),
    );

    renderPanel();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByTestId("button-crypto-accuracy-refresh"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });
});
