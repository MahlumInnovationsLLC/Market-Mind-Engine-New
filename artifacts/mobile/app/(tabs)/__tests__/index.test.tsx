/**
 * Component tests for the mobile dashboard quota CTA.
 *
 * The free-tier quota pill turns red when a user hits 5 / 5 markets,
 * and a tappable upgrade CTA must appear directly beneath it routing
 * to the in-app subscription tab. These tests lock down the gating
 * (free tier + at-quota only) and the navigation target so a regression
 * does not silently strand free users when the API starts returning
 * HTTP 402 upgrade_required.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react-native";
import React from "react";

const mockApiFetch = jest.fn();
const mockUseSafeAreaInsets = jest.fn();
const mockRouterPush = jest.fn();

jest.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
}));

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => mockUseSafeAreaInsets(),
  SafeAreaProvider: ({ children }: { children: React.ReactNode }) => children,
}));

jest.mock("expo-router", () => ({
  useRouter: () => ({ push: mockRouterPush }),
}));

// The verticals filter hook reads from a module-level store and fires
// its own query — stub it out so the dashboard render stays focused on
// the quota CTA branches.
jest.mock("@/lib/vertical-filter", () => ({
  useSelectedVerticals: () => ({
    selected: [],
    matches: () => true,
    clear: jest.fn(),
    filterActive: false,
    hiddenActive: false,
  }),
  // The tail-risk-exposure tile (task #744) calls this — stub it out
  // here so the dashboard render keeps the quota CTA branches as the
  // sole subject of these tests.
  useEffectiveVerticals: () => [],
}));

import DashboardScreen from "../index";

type Quota = { viewed_today: number; limit: number; resets_at: string };
type SubInfo = {
  tier: string;
  status: string;
  is_founder: boolean;
  email: string | null;
  quota: Quota | null;
};

const OVERVIEW = {
  counts: { trades: 3, swarm_debates: 2, trading_cycles: 1, positions: 0 },
  latest_cycle_at: null,
  latest_trade_at: null,
  realized_pnl: 0,
  win_rate: null,
  open_positions: 0,
};

const VERTICALS = { verticals: [] };

const TAIL_RISK_EMPTY = {
  days: 30,
  buckets: [],
  totals: {
    open_count: 0,
    open_cost_usd: 0,
    open_max_loss_usd: 0,
    realized_count: 0,
    realized_wins: 0,
    realized_pnl_usd: 0,
  },
  tail_concentration: {
    tail_max_loss_usd: 0,
    total_max_loss_usd: 0,
    pct: null,
    tone: "empty",
  },
};

function setApi(sub: SubInfo): void {
  mockApiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith("/api/dashboard/overview")) return OVERVIEW;
    if (path === "/api/me/subscription") return sub;
    if (path.startsWith("/api/dashboard/verticals")) return VERTICALS;
    if (path.startsWith("/api/public/tail-risk-exposure")) return TAIL_RISK_EMPTY;
    throw new Error(`Unexpected apiFetch call: ${path}`);
  });
}

function renderScreen(): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <DashboardScreen />
    </QueryClientProvider>,
  );
}

// First render of the dashboard pulls in the tab layout, fonts, and
// react-query plumbing — on a cold worker the initial frame can land
// well above the default 5s budget. Bump the per-test timeout so a
// slow CI box does not flake the upgrade CTA assertion.
jest.setTimeout(20_000);

beforeEach(() => {
  jest.clearAllMocks();
  mockUseSafeAreaInsets.mockReturnValue({ top: 0, bottom: 0, left: 0, right: 0 });
});

describe("DashboardScreen quota upgrade CTA", () => {
  it("renders the upgrade CTA when a free user hits the quota", async () => {
    setApi({
      tier: "free",
      status: "none",
      is_founder: false,
      email: "free@example.com",
      quota: { viewed_today: 5, limit: 5, resets_at: "00:00 UTC" },
    });

    renderScreen();

    // Wait for the subscription query to resolve — once the quota pill
    // renders we know the at-quota branch has had a chance to run.
    expect(await screen.findByTestId("pill-free-quota")).toBeTruthy();
    const cta = screen.getByTestId("cta-upgrade-quota");
    expect(cta).toBeTruthy();
    fireEvent.press(cta);
    expect(mockRouterPush).toHaveBeenCalledWith("/(tabs)/subscription");
  });

  it("hides the upgrade CTA when a free user is below the quota", async () => {
    setApi({
      tier: "free",
      status: "none",
      is_founder: false,
      email: "free@example.com",
      quota: { viewed_today: 2, limit: 5, resets_at: "00:00 UTC" },
    });

    renderScreen();

    expect(await screen.findByTestId("pill-free-quota")).toBeTruthy();
    expect(screen.queryByTestId("cta-upgrade-quota")).toBeNull();
  });

  it("hides the upgrade CTA for paid tiers even if quota is reported", async () => {
    // Paid tiers should never see the upgrade banner. We exercise the
    // belt-and-suspenders gate (`tier === "free"`) by handing back a
    // stale at-quota pill alongside a pro tier — the CTA must still
    // stay hidden.
    setApi({
      tier: "pro",
      status: "active",
      is_founder: false,
      email: "pro@example.com",
      quota: { viewed_today: 5, limit: 5, resets_at: "00:00 UTC" },
    });

    renderScreen();

    expect(await screen.findByTestId("pill-free-quota")).toBeTruthy();
    expect(screen.queryByTestId("cta-upgrade-quota")).toBeNull();
  });
});
