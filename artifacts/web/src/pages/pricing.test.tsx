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

// ---------------------------------------------------------------------------
// Hoisted shared state used by the wouter / api-client-react / clerk mocks
// below.  Tests mutate `state` and `apiState` directly between cases.
// ---------------------------------------------------------------------------
const { state, notify } = vi.hoisted(() => {
  const s: {
    searchValue: string;
    pathValue: string;
    listeners: Set<() => void>;
  } = {
    searchValue: "",
    pathValue: "/pricing",
    listeners: new Set(),
  };
  const n = () => {
    s.listeners.forEach((l) => l());
  };
  return { state: s, notify: n };
});

const { apiState } = vi.hoisted(() => ({
  apiState: {
    pricingResult: null as unknown as {
      data: unknown;
      error: unknown;
    },
    mutateAsync: ((..._args: unknown[]) => Promise.resolve()) as (
      ...args: unknown[]
    ) => Promise<unknown>,
  },
}));

// Replace wouter with a tiny in-memory router so dismissAutoCheckout's
// `setLocation("/pricing")` actually flips the splash off and the
// pricing grid back on without us having to wire up the real history API.
vi.mock("wouter", async () => {
  const React = await import("react");
  const subscribe = (cb: () => void) => {
    state.listeners.add(cb);
    return () => {
      state.listeners.delete(cb);
    };
  };
  const useSearch = () =>
    React.useSyncExternalStore(
      subscribe,
      () => state.searchValue,
      () => state.searchValue,
    );
  const useLocation = () => {
    const path = React.useSyncExternalStore(
      subscribe,
      () => state.pathValue,
      () => state.pathValue,
    );
    const navigate = (next: string) => {
      const [p, s] = next.split("?");
      state.pathValue = p ?? "/";
      state.searchValue = s ?? "";
      notify();
    };
    return [path, navigate] as const;
  };
  const Link = ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
    [key: string]: unknown;
  }) => React.createElement("a", { href, ...rest }, children);
  return { useSearch, useLocation, Link };
});

vi.mock("@clerk/react", async () => {
  const React = await import("react");
  return {
    useAuth: () => ({ isSignedIn: true }),
    SignInButton: ({ children }: { children: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
  };
});

// `PublicNav` consults a cookie heuristic; the splash test doesn't care
// what the nav reports, so a static stub keeps jsdom happy.
vi.mock("@/lib/clerk-session", () => ({
  hasClerkSession: () => true,
}));

vi.mock("@workspace/api-client-react", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status = 500) {
      super(message);
      this.status = status;
    }
  },
  useGetBillingPricing: () => apiState.pricingResult,
  useCreateBillingCheckoutSession: () => ({
    mutateAsync: (...args: unknown[]) => apiState.mutateAsync(...args),
  }),
}));

import PricingPage from "./pricing";

const PRO_MONTHLY_PRICE_ID = "price_pro_monthly";

const fixtureCatalog = {
  tiers: [
    {
      id: "free",
      name: "Free",
      tagline: "Free tier",
      monthly_usd: 0,
      annual_usd: 0,
      features: ["Read-only telemetry"],
      cta: "Get started",
      monthly: { configured: false },
      annual: { configured: false },
    },
    {
      id: "pro",
      name: "Pro",
      tagline: "Pro tier",
      monthly_usd: 49,
      annual_usd: 490,
      features: ["Live trades"],
      cta: "Upgrade",
      monthly: { price_id: PRO_MONTHLY_PRICE_ID, configured: true },
      annual: { price_id: "price_pro_annual", configured: true },
      highlight: true,
    },
    {
      id: "elite",
      name: "Elite",
      tagline: "Elite tier",
      monthly_usd: 199,
      annual_usd: 1990,
      features: ["Everything"],
      cta: "Contact",
      monthly: { price_id: "price_elite_monthly", configured: true },
      annual: { price_id: "price_elite_annual", configured: true },
    },
  ],
  publishable_key_present: true,
  stripe_configured: true,
};

beforeEach(() => {
  state.searchValue = `checkout=${PRO_MONTHLY_PRICE_ID}`;
  state.pathValue = "/pricing";
  state.listeners.clear();
  apiState.pricingResult = { data: fixtureCatalog, error: null };
  apiState.mutateAsync = vi.fn(() => new Promise<never>(() => {}));
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("PricingPage post-sign-up auto-checkout splash", () => {
  it("shows the redirecting splash instead of the pricing grid when the user lands with ?checkout=<price_id>", async () => {
    // Mutation hangs forever so we stay in the "redirecting" state.
    const mutate = vi.fn(() => new Promise<never>(() => {}));
    apiState.mutateAsync = mutate;

    render(<PricingPage />);

    await screen.findByTestId("splash-checkout-redirect");

    // The full pricing grid (interval toggle + tier cards) must not be
    // rendered while the splash is up; otherwise the user would see
    // both the spinner AND the comparison table flash.
    expect(screen.queryByTestId("card-tier-pro")).toBeNull();
    expect(screen.queryByTestId("card-tier-elite")).toBeNull();
    expect(screen.queryByTestId("button-interval-month")).toBeNull();

    // The auto-checkout effect should have fired exactly once with the
    // price id from the URL.
    await waitFor(() => {
      expect(mutate).toHaveBeenCalledWith({
        data: { price_id: PRO_MONTHLY_PRICE_ID },
      });
    });
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("shows the error splash with the failure message and re-invokes the checkout mutation when 'Try again' is clicked", async () => {
    const mutate = vi
      .fn()
      .mockRejectedValueOnce(new Error("Stripe is on fire"))
      .mockImplementationOnce(() => new Promise<never>(() => {}));
    apiState.mutateAsync = mutate;

    render(<PricingPage />);

    // First call rejects → error splash renders with the message.
    const message = await screen.findByTestId("text-checkout-error-message");
    expect(message.textContent).toContain("Stripe is on fire");
    expect(screen.getByTestId("splash-checkout-error")).toBeTruthy();
    expect(screen.queryByTestId("splash-checkout-redirect")).toBeNull();
    expect(mutate).toHaveBeenCalledTimes(1);

    // Clicking "Try again" must re-invoke the same mutation with the
    // same price id from the URL — that is the entire recovery path.
    fireEvent.click(screen.getByTestId("button-retry-checkout"));

    await waitFor(() => {
      expect(mutate).toHaveBeenCalledTimes(2);
    });
    expect(mutate).toHaveBeenLastCalledWith({
      data: { price_id: PRO_MONTHLY_PRICE_ID },
    });
  });

  it("returns the user to the full pricing grid when 'Cancel and pick a different tier' is clicked from the redirect splash", async () => {
    apiState.mutateAsync = vi.fn(() => new Promise<never>(() => {}));

    render(<PricingPage />);

    await screen.findByTestId("splash-checkout-redirect");

    fireEvent.click(screen.getByTestId("button-cancel-redirect"));

    // Splash gone, full grid (and interval toggle) back.
    await screen.findByTestId("card-tier-pro");
    expect(screen.queryByTestId("splash-checkout-redirect")).toBeNull();
    expect(screen.getByTestId("card-tier-elite")).toBeTruthy();
    expect(screen.getByTestId("button-interval-month")).toBeTruthy();
  });

  it("returns the user to the full pricing grid when 'Pick a different tier' is clicked from the error splash", async () => {
    apiState.mutateAsync = vi
      .fn()
      .mockRejectedValueOnce(new Error("Stripe is on fire"));

    render(<PricingPage />);

    await screen.findByTestId("splash-checkout-error");

    fireEvent.click(screen.getByTestId("button-pick-different-tier"));

    await screen.findByTestId("card-tier-pro");
    expect(screen.queryByTestId("splash-checkout-error")).toBeNull();
    expect(screen.getByTestId("card-tier-elite")).toBeTruthy();
    expect(screen.getByTestId("button-interval-month")).toBeTruthy();
  });
});
