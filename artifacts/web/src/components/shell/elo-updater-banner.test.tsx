import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { state } = vi.hoisted(() => ({
  state: {
    auth: { isSignedIn: true, getToken: vi.fn(async () => "token") },
    snapshot: { data: undefined as unknown },
  },
}));

vi.mock("@clerk/react", () => ({
  useAuth: () => state.auth,
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: () => state.snapshot,
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(async () => ({})),
}));

import { EloUpdaterBanner } from "./elo-updater-banner";

afterEach(() => {
  cleanup();
  state.snapshot.data = undefined;
});

function renderBanner() {
  const { hook } = memoryLocation({ path: "/" });
  return render(
    <Router hook={hook}>
      <EloUpdaterBanner />
    </Router>,
  );
}

describe("EloUpdaterBanner", () => {
  it("renders nothing while the snapshot is loading", () => {
    state.snapshot.data = undefined;
    const { queryByTestId } = renderBanner();
    expect(queryByTestId("banner-elo-updater")).toBeNull();
  });

  it("renders nothing when the elo_updater card is ok", () => {
    state.snapshot.data = {
      cards: [
        {
          key: "elo_updater",
          status: "ok",
          detail: "last cycle 12s ago",
          extras: { reason: null, age_seconds: 12 },
        },
      ],
    };
    const { queryByTestId } = renderBanner();
    expect(queryByTestId("banner-elo-updater")).toBeNull();
  });

  it("renders nothing when no elo_updater card is present", () => {
    state.snapshot.data = {
      cards: [{ key: "ws_backpressure", status: "ok", detail: "", extras: {} }],
    };
    const { queryByTestId } = renderBanner();
    expect(queryByTestId("banner-elo-updater")).toBeNull();
  });

  it("renders a stalled-mode banner with the cycle age", () => {
    state.snapshot.data = {
      cards: [
        {
          key: "elo_updater",
          status: "degraded",
          detail: "stalled: last cycle 600s ago (>180s threshold)",
          extras: { reason: "stalled", age_seconds: 600 },
        },
      ],
    };
    const { getByTestId } = renderBanner();
    const banner = getByTestId("banner-elo-updater");
    expect(banner).toBeTruthy();
    expect(banner.textContent ?? "").toContain("TEAM RATING UPDATER DEGRADED");
    const summary = getByTestId("text-elo-updater-summary").textContent ?? "";
    expect(summary).toContain("Stalled");
    expect(summary).toContain("600s");
  });

  it("renders an errors-mode banner with the error count", () => {
    state.snapshot.data = {
      cards: [
        {
          key: "elo_updater",
          status: "degraded",
          detail: "last cycle returned 3 error(s): boom",
          extras: { reason: "errors", age_seconds: 12 },
        },
      ],
    };
    const { getByTestId } = renderBanner();
    const summary = getByTestId("text-elo-updater-summary").textContent ?? "";
    expect(summary).toContain("3 error");
  });

  it("links to the controls tab with the elo-updater focus anchor", () => {
    state.snapshot.data = {
      cards: [
        {
          key: "elo_updater",
          status: "degraded",
          detail: "stalled: last cycle 600s ago",
          extras: { reason: "stalled", age_seconds: 600 },
        },
      ],
    };
    const { getByTestId } = renderBanner();
    const link = getByTestId("link-elo-updater-panel") as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(
      "/system?tab=controls&focus=elo-updater",
    );
  });

  it("falls back to the server detail when the reason is unknown", () => {
    state.snapshot.data = {
      cards: [
        {
          key: "elo_updater",
          status: "degraded",
          detail: "elo updater loop is not running",
          extras: {},
        },
      ],
    };
    const { getByTestId } = renderBanner();
    const summary = getByTestId("text-elo-updater-summary").textContent ?? "";
    expect(summary).toContain("elo updater loop is not running");
  });
});
