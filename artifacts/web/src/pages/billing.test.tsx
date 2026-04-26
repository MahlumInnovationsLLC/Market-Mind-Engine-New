import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { state } = vi.hoisted(() => ({
  state: {
    auth: { isSignedIn: false },
    clerk: { signOut: vi.fn() },
    sub: {
      data: undefined as unknown,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    },
    portal: { mutateAsync: vi.fn() },
    location: "/billing",
  },
}));

vi.mock("wouter", async () => {
  const actual = await vi.importActual<typeof import("wouter")>("wouter");
  return {
    ...actual,
    useLocation: () => [state.location, vi.fn()] as const,
  };
});

vi.mock("@clerk/react", () => ({
  useAuth: () => state.auth,
  useClerk: () => state.clerk,
}));

vi.mock("@workspace/api-client-react", () => ({
  ApiError: class ApiError extends Error {},
  UserSubscriptionTier: {
    free: "free",
    pro: "pro",
    elite: "elite",
    founder: "founder",
  },
  getGetMySubscriptionQueryKey: () => ["my-subscription"],
  useCreateBillingPortalSession: () => state.portal,
  useGetMySubscription: () => state.sub,
}));

vi.mock("@/lib/api", () => ({
  formatErrorMessage: (e: unknown) =>
    e instanceof Error ? e.message : String(e),
}));

vi.mock("@/lib/pending-checkout", () => ({
  clearPendingCheckoutPriceId: vi.fn(),
  getPendingCheckoutPriceId: () => null,
}));

vi.mock("@tanstack/react-query", () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

import BillingPage from "./billing";

afterEach(() => {
  cleanup();
  state.auth.isSignedIn = false;
  state.sub.data = undefined;
  state.sub.isLoading = false;
  state.sub.isError = false;
  state.location = "/billing";
});

function renderPage(path = "/billing") {
  state.location = path;
  const { hook } = memoryLocation({ path });
  return render(
    <Router hook={hook}>
      <BillingPage />
    </Router>,
  );
}

describe("BillingPage", () => {
  it("renders the sign-in-required notice when the visitor is not authenticated", () => {
    renderPage();
    expect(screen.getByTestId("text-signin-required")).toBeTruthy();
  });

  it("renders the current tier and status nodes for an authenticated Pro user", () => {
    state.auth.isSignedIn = true;
    state.sub.data = {
      tier: "pro",
      status: "active",
      current_period_end: "2026-12-31T00:00:00Z",
      cancel_at_period_end: false,
      stripe_customer_id: "cus_x",
      stripe_subscription_id: "sub_x",
    };
    renderPage();
    expect(screen.getByTestId("text-current-tier").textContent?.toLowerCase()).toContain("pro");
    expect(screen.getByTestId("text-current-status").textContent?.toLowerCase()).toContain("active");
  });

  it("renders the checkout-success notice when the URL carries ?checkout=success", () => {
    state.auth.isSignedIn = true;
    state.sub.data = { tier: "pro", status: "active" };
    renderPage("/billing?checkout=success");
    expect(screen.getByTestId("text-checkout-success")).toBeTruthy();
  });
});
