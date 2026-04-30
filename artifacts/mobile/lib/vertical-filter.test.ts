/**
 * Unit tests for the pure vertical-selection helpers.
 *
 * The hook (`useSelectedVerticals`) needs React + React Query + the API
 * fetcher to render and is exercised by the Playwright e2e suite. The
 * standalone setters / getters / mutators below are pure-ish (only the
 * module-level singleton is touched) so they're easy to cover here.
 *
 * `useEffectiveVerticals` is rendered with React + React Query so we
 * can lock down the hidden-vertical suppression contract that pairs the
 * mobile tail-risk tile with the web tile (task #744).  Without this
 * coverage a future change to the hook could silently leak hidden
 * vertical exposure into a non-admin operator's headline on phone while
 * the web headline stayed correctly suppressed.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react-native";
import React from "react";

const mockApiFetch = jest.fn();
jest.mock("@/lib/api", () => ({
  apiFetch: (...args: unknown[]) => mockApiFetch(...args),
  ApiError: class ApiError extends Error {},
}));

import {
  clearSelectedVerticals,
  getSelectedVerticals,
  setSelectedVerticals,
  toggleSelectedVertical,
  useEffectiveVerticals,
} from "@/lib/vertical-filter";

beforeEach(() => {
  clearSelectedVerticals();
});

describe("setSelectedVerticals", () => {
  it("lower-cases, dedupes, and sorts the input", () => {
    setSelectedVerticals(["Sports", "WEATHER", "sports", "econ"]);
    expect(getSelectedVerticals()).toEqual(["econ", "sports", "weather"]);
  });

  it("replaces the previous selection rather than merging", () => {
    setSelectedVerticals(["sports"]);
    setSelectedVerticals(["weather"]);
    expect(getSelectedVerticals()).toEqual(["weather"]);
  });
});

describe("toggleSelectedVertical", () => {
  it("adds a vertical when it isn't selected", () => {
    toggleSelectedVertical("Sports");
    expect(getSelectedVerticals()).toEqual(["sports"]);
  });

  it("removes a vertical that's already selected (case-insensitive)", () => {
    setSelectedVerticals(["sports", "weather"]);
    toggleSelectedVertical("WEATHER");
    expect(getSelectedVerticals()).toEqual(["sports"]);
  });
});

describe("clearSelectedVerticals", () => {
  it("empties the selection", () => {
    setSelectedVerticals(["sports", "weather"]);
    clearSelectedVerticals();
    expect(getSelectedVerticals()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// useEffectiveVerticals (task #744 — web/mobile contract parity)
// ---------------------------------------------------------------------------

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children);
}

function mockEndpoints(opts: {
  isAdmin: boolean;
  hidden: string[];
  knownVerticals: string[];
}) {
  mockApiFetch.mockImplementation((path: string) => {
    if (path === "/api/me/access") {
      return Promise.resolve({ is_admin: opts.isAdmin });
    }
    if (path === "/api/dashboard/vertical-visibility") {
      return Promise.resolve({ hidden: opts.hidden, pinned: [] });
    }
    if (path.startsWith("/api/dashboard/verticals?")) {
      return Promise.resolve({
        verticals: opts.knownVerticals.map((v) => ({ vertical: v })),
      });
    }
    return Promise.reject(new Error(`unexpected path: ${path}`));
  });
}

describe("useEffectiveVerticals (task #744 — hidden-vertical suppression)", () => {
  beforeEach(() => {
    mockApiFetch.mockReset();
    clearSelectedVerticals();
  });

  it("returns [] for an admin with no chip selection (no narrowing)", async () => {
    mockEndpoints({
      isAdmin: true,
      hidden: ["weather"],
      knownVerticals: ["sports", "weather", "crypto"],
    });
    const { result } = renderHook(() => useEffectiveVerticals(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => {
      expect(mockApiFetch).toHaveBeenCalled();
    });
    // Admin sees everything — empty array means "no server-side filter".
    expect(result.current).toEqual([]);
  });

  it("returns the chip selection verbatim for an admin", async () => {
    setSelectedVerticals(["sports", "crypto"]);
    mockEndpoints({
      isAdmin: true,
      hidden: ["weather"],
      knownVerticals: ["sports", "weather", "crypto"],
    });
    const { result } = renderHook(() => useEffectiveVerticals(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => {
      expect(result.current.sort()).toEqual(["crypto", "sports"]);
    });
  });

  it("narrows non-admin to the visible set when no chip is selected", async () => {
    mockEndpoints({
      isAdmin: false,
      hidden: ["weather"],
      knownVerticals: ["sports", "weather", "crypto"],
    });
    const { result } = renderHook(() => useEffectiveVerticals(), {
      wrapper: makeWrapper(),
    });
    // Wait for the registry overview *and* visibility to both resolve.
    await waitFor(() => {
      expect(result.current.length).toBe(2);
    });
    expect(result.current.sort()).toEqual(["crypto", "sports"]);
  });

  it("strips hidden verticals from a non-admin's stale chip selection", async () => {
    setSelectedVerticals(["sports", "weather"]);
    mockEndpoints({
      isAdmin: false,
      hidden: ["weather"],
      knownVerticals: ["sports", "weather", "crypto"],
    });
    const { result } = renderHook(() => useEffectiveVerticals(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => {
      expect(result.current).toEqual(["sports"]);
    });
  });
});
