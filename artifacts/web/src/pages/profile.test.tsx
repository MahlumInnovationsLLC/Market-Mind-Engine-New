import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { state } = vi.hoisted(() => ({
  state: {
    user: { isLoaded: true, user: null as unknown },
    auth: { isSignedIn: false, getToken: vi.fn(async () => null) },
    clerk: { signOut: vi.fn() },
  },
}));

vi.mock("@clerk/react", () => ({
  useUser: () => state.user,
  useAuth: () => state.auth,
  useClerk: () => state.clerk,
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  }),
  useMutation: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
  }),
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
    setQueryData: vi.fn(),
  }),
}));

vi.mock("@workspace/api-client-react", () => ({
  ApiError: class ApiError extends Error {},
  UserSubscriptionTier: {
    free: "free",
    pro: "pro",
    elite: "elite",
    founder: "founder",
  },
  getGetMyAccessQueryKey: () => ["my-access"],
  getGetMySubscriptionQueryKey: () => ["my-subscription"],
  useGetMyAccess: () => ({
    data: undefined,
    isLoading: false,
    refetch: vi.fn(),
  }),
  useGetMySubscription: () => ({
    data: undefined,
    isLoading: false,
    refetch: vi.fn(),
  }),
  useCreateBillingPortalSession: () => ({
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    isPending: false,
  }),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(async () => ({})),
  formatErrorMessage: (e: unknown) =>
    e instanceof Error ? e.message : String(e),
}));

import ProfilePage from "./profile";

afterEach(() => {
  cleanup();
});

describe("ProfilePage", () => {
  it("renders the page-profile shell with the identity card", () => {
    const { hook } = memoryLocation({ path: "/profile" });
    render(
      <Router hook={hook}>
        <ProfilePage />
      </Router>,
    );
    expect(screen.getByTestId("page-profile")).toBeTruthy();
    expect(screen.getByTestId("card-profile-identity")).toBeTruthy();
  });
});
