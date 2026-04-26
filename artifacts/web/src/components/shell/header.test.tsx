import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { state } = vi.hoisted(() => ({
  state: {
    user: { isLoaded: true, user: null as unknown },
    auth: { isSignedIn: false, getToken: vi.fn(async () => null) },
    clerk: { signOut: vi.fn() },
    health: { data: undefined as unknown, isLoading: false },
    sub: { data: undefined as unknown, isLoading: false },
  },
}));

vi.mock("@clerk/react", () => ({
  useUser: () => state.user,
  useAuth: () => state.auth,
  useClerk: () => state.clerk,
}));

vi.mock("@workspace/api-client-react", () => ({
  useHealthFull: () => state.health,
  getHealthFullQueryKey: () => ["health-full"],
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => state.sub,
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(async () => ({})),
}));

vi.mock("@/components/realtime/alerts-bell", () => ({
  AlertsBell: () => <div data-testid="alerts-bell-stub" />,
}));

vi.mock("@/components/ui/sidebar", () => ({
  SidebarTrigger: () => <button data-testid="sidebar-trigger-stub" />,
}));

import { ShellHeader } from "./header";

afterEach(() => {
  cleanup();
});

describe("ShellHeader", () => {
  it("renders the header chrome with the alerts bell and sidebar trigger", () => {
    const { hook } = memoryLocation({ path: "/" });
    const { getByTestId } = render(
      <Router hook={hook}>
        <ShellHeader operatorEmail={null} />
      </Router>,
    );
    expect(getByTestId("alerts-bell-stub")).toBeTruthy();
    expect(getByTestId("sidebar-trigger-stub")).toBeTruthy();
  });

  it("renders the operator email in the user menu when supplied", () => {
    const { hook } = memoryLocation({ path: "/" });
    state.auth.isSignedIn = true;
    state.user.user = { primaryEmailAddress: { emailAddress: "ops@axiom.io" } };
    const { getByText } = render(
      <Router hook={hook}>
        <ShellHeader operatorEmail="ops@axiom.io" />
      </Router>,
    );
    expect(getByText("ops@axiom.io")).toBeTruthy();
  });
});
