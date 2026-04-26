/**
 * Component tests for the verticals tab's persona-override badges.
 *
 * Mirrors the web dashboard banner + per-card chip from
 * artifacts/web/src/components/dashboard/vertical-cards.tsx so admins
 * looking at the mobile app can see at a glance which verticals are
 * running with non-default personas. Non-admins must never see the
 * banner / chip — they cannot read the admin-only toggles endpoint.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react-native";
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

jest.mock("@/lib/vertical-filter", () => ({
  useSelectedVerticals: () => ({
    selected: [],
    toggle: jest.fn(),
    clear: jest.fn(),
    matches: () => true,
    filterActive: false,
    hiddenActive: false,
  }),
  setSelectedVerticals: jest.fn(),
}));

import VerticalsScreen from "../verticals";

const VERTICALS = {
  verticals: [
    {
      vertical: "nba",
      description: "NBA props",
      pinned: false,
      hidden: false,
      enabled: true,
      pnl: 100,
      win_rate: 0.55,
      brier: 0.21,
      sharpe: 1.1,
      open_positions: 2,
      trades: 12,
      edge_predicted_bps: 30,
      edge_realized_bps: 20,
      last_cycle: null,
    },
    {
      vertical: "nfl",
      description: "NFL props",
      pinned: false,
      hidden: false,
      enabled: true,
      pnl: -50,
      win_rate: 0.48,
      brier: 0.24,
      sharpe: 0.4,
      open_positions: 1,
      trades: 8,
      edge_predicted_bps: 15,
      edge_realized_bps: 10,
      last_cycle: null,
    },
  ],
};

const TOGGLES_TWO_OFF = {
  items: [
    {
      vertical: "nba",
      vertical_label: "NBA",
      persona_profile: "default",
      personas: [
        { id: "p1", display: "Hawk", enabled: false },
        { id: "p2", display: "Owl", enabled: false },
        { id: "p3", display: "Dove", enabled: true },
      ],
    },
    {
      vertical: "nfl",
      vertical_label: "NFL",
      persona_profile: "default",
      personas: [
        { id: "p1", display: "Hawk", enabled: true },
        { id: "p2", display: "Owl", enabled: true },
      ],
    },
  ],
};

function setApi({
  isAdmin,
  toggles,
}: {
  isAdmin: boolean;
  toggles?: typeof TOGGLES_TWO_OFF;
}): void {
  mockApiFetch.mockImplementation(async (path: string) => {
    if (path === "/api/me/access") return { is_admin: isAdmin };
    if (path.startsWith("/api/dashboard/verticals")) return VERTICALS;
    if (path === "/api/admin/persona-toggles") {
      if (!toggles) {
        throw new Error("persona-toggles called when none provided");
      }
      return toggles;
    }
    throw new Error(`Unexpected apiFetch call: ${path}`);
  });
}

function renderScreen(): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <VerticalsScreen />
    </QueryClientProvider>,
  );
}

jest.setTimeout(20_000);

beforeEach(() => {
  jest.clearAllMocks();
  mockUseSafeAreaInsets.mockReturnValue({ top: 0, bottom: 0, left: 0, right: 0 });
});

describe("VerticalsScreen persona-override badges", () => {
  it("renders the summary banner and per-card badge for admins", async () => {
    setApi({ isAdmin: true, toggles: TOGGLES_TWO_OFF });
    renderScreen();

    // Two queries fire serially (access, then personas). Wait for the
    // toggles request to complete so we know the banner branch has had
    // a chance to render before asserting.
    await waitFor(() => {
      expect(
        mockApiFetch.mock.calls.find(
          (c) => c[0] === "/api/admin/persona-toggles",
        ),
      ).toBeDefined();
    });
    expect(await screen.findByTestId("persona-override-summary")).toBeTruthy();
    expect(screen.getByTestId("persona-override-chip-nba")).toBeTruthy();
    // Only nba has disabled personas — nfl chip must not exist.
    expect(screen.queryByTestId("persona-override-chip-nfl")).toBeNull();
    expect(await screen.findByTestId("badge-persona-override-nba")).toBeTruthy();
    expect(screen.queryByTestId("badge-persona-override-nfl")).toBeNull();
  });

  it("hides the banner and badges for non-admin users", async () => {
    setApi({ isAdmin: false });
    renderScreen();

    // The vertical cards still render — wait for them so we know the
    // dashboard finished its first pass before asserting the absence
    // of the admin-only banner / badge.
    expect(await screen.findByTestId("card-vertical-nba")).toBeTruthy();
    expect(screen.queryByTestId("persona-override-summary")).toBeNull();
    expect(screen.queryByTestId("badge-persona-override-nba")).toBeNull();
    // Sanity check: we never hit the admin-only endpoint.
    expect(
      mockApiFetch.mock.calls.find(
        (c) => c[0] === "/api/admin/persona-toggles",
      ),
    ).toBeUndefined();
  });

  it("hides the banner for admins when no personas are disabled", async () => {
    setApi({
      isAdmin: true,
      toggles: {
        items: [
          {
            vertical: "nba",
            vertical_label: "NBA",
            persona_profile: "default",
            personas: [{ id: "p1", display: "Hawk", enabled: true }],
          },
        ],
      },
    });
    renderScreen();

    await waitFor(() => {
      expect(
        mockApiFetch.mock.calls.find(
          (c) => c[0] === "/api/admin/persona-toggles",
        ),
      ).toBeDefined();
    });
    expect(await screen.findByTestId("card-vertical-nba")).toBeTruthy();
    expect(screen.queryByTestId("persona-override-summary")).toBeNull();
    expect(screen.queryByTestId("badge-persona-override-nba")).toBeNull();
  });
});
