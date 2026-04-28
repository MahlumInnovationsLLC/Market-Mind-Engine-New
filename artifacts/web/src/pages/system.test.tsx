import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

vi.mock("@clerk/react", () => ({
  useAuth: () => ({ isSignedIn: true, getToken: vi.fn(async () => null) }),
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

// Mutable so a single test can flip the admin flag without re-hoisting
// the entire mock module.
const _accessState: { is_admin: boolean } = { is_admin: false };
vi.mock("@workspace/api-client-react", () => ({
  getGetMyAccessQueryKey: () => ["my-access"],
  useGetMyAccess: () => ({ data: { is_admin: _accessState.is_admin }, isLoading: false }),
  customFetch: vi.fn(async () => ({})),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(async () => ({})),
  formatErrorMessage: (e: unknown) =>
    e instanceof Error ? e.message : String(e),
}));

vi.mock("@/hooks/use-focus-from-query", () => ({
  useFocusFromQuery: () => undefined,
}));

vi.mock("@/pages/health", () => ({
  HealthPanel: () => <div data-testid="health-panel-stub" />,
}));

vi.mock("@/pages/settings-notifications", () => ({
  NotificationPrefsPanel: () => <div data-testid="prefs-stub" />,
}));

vi.mock("@/pages/admin", () => ({
  AuditLogPanel: () => <div />,
  CryptoAccuracyPanel: () => <div />,
  CryptoBacktestPanel: () => <div />,
  EloUpdaterPanel: () => <div />,
  GatekeeperModelPanel: () => <div />,
  GatekeeperTuningPanel: () => <div data-testid="gatekeeper-tuning-stub" />,
  GithubMirrorPanel: () => <div />,
  LeadsPanel: () => <div />,
  ModelsPanel: () => <div />,
  PersonasPanel: () => <div />,
  RcWebhookPanel: () => <div />,
  RiskKnobsPanel: () => <div />,
  RuntimePanel: () => <div />,
  TechAiPromotionPanel: () => <div />,
  TriggerCyclePanel: () => <div />,
  UserManagementPanel: () => <div />,
  VerticalHaltsPanel: () => <div />,
  VerticalVisibilityPanel: () => <div />,
}));

vi.mock("@/components/telemetry/live-execute-toggle", () => ({
  default: () => <div />,
}));
vi.mock("@/components/telemetry/emergency-stop", () => ({
  default: () => <div />,
}));
vi.mock("@/components/telemetry/access-requests", () => ({
  default: () => <div />,
}));
vi.mock("@/components/intelligence/autoresearch-panel", () => ({
  default: () => <div />,
}));

import SystemPage from "./system";

afterEach(() => {
  cleanup();
});

describe("SystemPage", () => {
  it("renders the panel-system-health stub on the default tab", () => {
    const { hook } = memoryLocation({ path: "/system" });
    const { getByTestId } = render(
      <Router hook={hook}>
        <SystemPage />
      </Router>,
    );
    expect(getByTestId("panel-system-health")).toBeTruthy();
    expect(getByTestId("health-panel-stub")).toBeTruthy();
  });

  it("activates the Settings tab when ?tab=settings is in the URL", () => {
    const { hook } = memoryLocation({ path: "/system?tab=settings" });
    const { getByTestId } = render(
      <Router hook={hook}>
        <SystemPage />
      </Router>,
    );
    expect(getByTestId("panel-system-settings")).toBeTruthy();
  });

  it("renders the Swarm tuning panel for admins on ?tab=swarm-tuning", () => {
    _accessState.is_admin = true;
    try {
      const { hook } = memoryLocation({ path: "/system?tab=swarm-tuning" });
      const { getByTestId } = render(
        <Router hook={hook}>
          <SystemPage />
        </Router>,
      );
      expect(getByTestId("panel-system-swarm-tuning")).toBeTruthy();
      expect(getByTestId("gatekeeper-tuning-stub")).toBeTruthy();
    } finally {
      _accessState.is_admin = false;
    }
  });
});
