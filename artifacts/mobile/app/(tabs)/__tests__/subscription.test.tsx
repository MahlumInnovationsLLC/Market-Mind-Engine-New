/**
 * Component tests for the mobile subscription screen.
 *
 * Locks down the rendering branches that decide which billing CTAs
 * the user sees. A regression that hides the Subscribe button or
 * shows the Stripe portal link for an RC-purchased subscription
 * should turn one of these red instead of slipping out to users.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react-native";
import React from "react";

const mockApiFetch = jest.fn();
const mockGetOfferings = jest.fn();
const mockRevenueCatConfigured = jest.fn();
const mockUseAuth = jest.fn();
const mockUseSafeAreaInsets = jest.fn();

jest.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
}));

jest.mock("@/lib/revenuecat", () => ({
  revenueCatConfigured: () => mockRevenueCatConfigured(),
}));

jest.mock("react-native-purchases", () => ({
  __esModule: true,
  default: {
    getOfferings: (...args: unknown[]) => mockGetOfferings(...args),
    purchasePackage: jest.fn(),
    setLogLevel: jest.fn(),
    configure: jest.fn(),
    logIn: jest.fn(),
    logOut: jest.fn(),
  },
  LOG_LEVEL: { WARN: "WARN" },
}));

jest.mock("@clerk/expo", () => ({
  useAuth: () => mockUseAuth(),
}));

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => mockUseSafeAreaInsets(),
  SafeAreaProvider: ({ children }: { children: React.ReactNode }) => children,
}));

jest.mock("expo-web-browser", () => ({
  openBrowserAsync: jest.fn(),
}));

// NotificationPrefs makes its own queries — render a stub so the
// subscription tests stay focused on billing CTA branches.
jest.mock("@/components/NotificationPrefs", () => ({
  NotificationPrefs: () => null,
}));

// Same story for TelegramConcierge: it queries `/api/me/telegram` on
// mount and would otherwise pull this test out of its billing focus.
jest.mock("@/components/TelegramConcierge", () => ({
  TelegramConcierge: () => null,
}));

import SubscriptionScreen from "../subscription";

type SubInfo = {
  user_id: string;
  email: string | null;
  tier: string;
  status: string;
  is_founder: boolean;
  stripe_subscription_id: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  interval: string | null;
};

const PRICING = {
  tiers: [
    {
      id: "free",
      name: "Free",
      monthly_usd: 0,
      annual_usd: 0,
      features: [],
      monthly: { price_id: null, configured: false },
      annual: { price_id: null, configured: false },
    },
    {
      id: "pro",
      name: "Pro",
      monthly_usd: 20,
      annual_usd: 200,
      features: [],
      monthly: { price_id: "price_pro_monthly", configured: true },
      annual: { price_id: "price_pro_annual", configured: true },
    },
  ],
};

const EMPTY_PRICING = { tiers: [] };

const baseSub = (overrides: Partial<SubInfo> = {}): SubInfo => ({
  user_id: "user_test",
  email: "test@example.com",
  tier: "free",
  status: "none",
  is_founder: false,
  stripe_subscription_id: null,
  current_period_end: null,
  cancel_at_period_end: false,
  interval: null,
  ...overrides,
});

function setApiResponses(
  sub: SubInfo,
  pricing: { tiers: unknown[] } = PRICING,
): void {
  mockApiFetch.mockImplementation(async (path: string) => {
    if (path === "/api/me/subscription") return sub;
    if (path === "/api/billing/pricing") return pricing;
    throw new Error(`Unexpected apiFetch call: ${path}`);
  });
}

function renderScreen(): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <SubscriptionScreen />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  // Default: RC not configured (matches device builds without keys),
  // so plans fall back to Stripe CTAs unless a test opts in.
  mockRevenueCatConfigured.mockReturnValue(false);
  mockGetOfferings.mockResolvedValue({ current: null });
  mockUseAuth.mockReturnValue({ signOut: jest.fn(), userId: "user_test" });
  mockUseSafeAreaInsets.mockReturnValue({
    top: 0,
    bottom: 0,
    left: 0,
    right: 0,
  });
});

describe("SubscriptionScreen", () => {
  it("free tier shows upgrade CTAs and no Manage button", async () => {
    setApiResponses(baseSub({ tier: "free" }));

    renderScreen();

    // Both monthly and annual checkout CTAs render (Stripe path because
    // RC offering is null). findAllByText also waits for the plan list
    // to render after the pricing query resolves.
    const checkoutButtons = await screen.findAllByText("CHECKOUT (WEB)");
    expect(checkoutButtons.length).toBe(2);
    // The plan tier name renders for both monthly and annual cards.
    expect(screen.getAllByText("PRO").length).toBeGreaterThanOrEqual(2);
    // No upgrade-section "Pricing unavailable" copy.
    expect(screen.queryByText("Pricing unavailable.")).toBeNull();
    // Crucially: free tier means no MANAGE SUBSCRIPTION button.
    expect(screen.queryByText("MANAGE SUBSCRIPTION")).toBeNull();
  });

  it("pro tier purchased via RevenueCat hides the Manage button", async () => {
    setApiResponses(
      baseSub({
        tier: "pro",
        status: "active",
        // RC-sourced subscription ids are prefixed with "rc:".
        stripe_subscription_id: "rc:app_user_123:monthly",
        interval: "month",
        current_period_end: "2026-12-31T00:00:00Z",
      }),
    );

    renderScreen();

    // Wait for the data-driven render (the "Renews:" line proves the
    // sub query has resolved and the current-plan card has rendered).
    expect(await screen.findByText(/^Renews:/)).toBeTruthy();
    // RC-purchased subs must NEVER show the Stripe portal link —
    // sending a mobile-purchased user to Stripe is a known footgun.
    expect(screen.queryByText("MANAGE SUBSCRIPTION")).toBeNull();
  });

  it("pro tier purchased via Stripe shows the Manage button", async () => {
    setApiResponses(
      baseSub({
        tier: "pro",
        status: "active",
        stripe_subscription_id: "sub_1NXyZabc",
        interval: "month",
        current_period_end: "2026-12-31T00:00:00Z",
      }),
    );

    renderScreen();

    expect(await screen.findByText("MANAGE SUBSCRIPTION")).toBeTruthy();
  });

  it("founder tier shows the bypass label and no Manage button or upgrade CTAs", async () => {
    setApiResponses(
      baseSub({
        tier: "founder",
        status: "active",
        is_founder: true,
        // Founder bypass means no real subscription on file.
        stripe_subscription_id: null,
      }),
      EMPTY_PRICING,
    );

    renderScreen();

    // Bypass copy renders next to the tier label.
    expect(await screen.findByText(/FOUNDER BYPASS/)).toBeTruthy();
    // No Stripe portal CTA — founders don't have a real Stripe sub.
    expect(screen.queryByText("MANAGE SUBSCRIPTION")).toBeNull();
    // No upgrade CTAs (the plan list is empty for the bypass case).
    expect(screen.queryByText("CHECKOUT (WEB)")).toBeNull();
    expect(screen.queryByText("SUBSCRIBE")).toBeNull();
    expect(screen.getByText("Pricing unavailable.")).toBeTruthy();
  });

  it("free tier with a RevenueCat offering shows SUBSCRIBE and not Stripe checkout", async () => {
    setApiResponses(baseSub({ tier: "free" }));
    // Simulate a device build where RC keys are configured and the
    // current offering returns app-store packages for both intervals.
    mockRevenueCatConfigured.mockReturnValue(true);
    mockGetOfferings.mockResolvedValue({
      current: {
        identifier: "default",
        availablePackages: [
          {
            identifier: "$rc_monthly",
            product: { identifier: "pro_monthly", priceString: "$19.99" },
          },
          {
            identifier: "$rc_annual",
            product: { identifier: "pro_annual", priceString: "$199.99" },
          },
        ],
      },
    });

    renderScreen();

    // Both intervals should land on the RC SUBSCRIBE CTA, not Stripe
    // CHECKOUT (WEB) — this is the regression the task is most worried
    // about ("hides the Subscribe button").
    const subscribeButtons = await screen.findAllByText("SUBSCRIBE");
    expect(subscribeButtons.length).toBe(2);
    expect(screen.queryByText("CHECKOUT (WEB)")).toBeNull();
    // RC priceString is rendered instead of the Stripe USD fallback
    // (the price label nests "$19.99" alongside " / month" inside the
    // same parent Text, so we match a substring).
    expect(screen.getByText(/\$19\.99/)).toBeTruthy();
    expect(screen.getByText(/\$199\.99/)).toBeTruthy();
  });

  it("cancel_at_period_end renders the 'Ends' copy instead of 'Renews'", async () => {
    setApiResponses(
      baseSub({
        tier: "pro",
        status: "active",
        stripe_subscription_id: "sub_1Cancel",
        interval: "month",
        current_period_end: "2026-12-31T00:00:00Z",
        cancel_at_period_end: true,
      }),
    );

    renderScreen();

    // The matcher targets the "Ends:" copy without depending on the
    // formatted date (locale-sensitive on the test host).
    expect(await screen.findByText(/^Ends:/)).toBeTruthy();
    // And it must NOT show "Renews:" in this branch.
    expect(screen.queryByText(/^Renews:/)).toBeNull();
  });
});
