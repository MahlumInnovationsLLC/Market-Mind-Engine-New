import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

vi.mock("@clerk/react", () => ({
  useAuth: () => ({ isSignedIn: false, getToken: vi.fn(async () => null) }),
}));

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(async () => ({})),
  ApiError: class ApiError extends Error {},
  formatErrorMessage: (e: unknown) =>
    e instanceof Error ? e.message : String(e),
}));

import { RecentAlertsPanel } from "./settings-notifications";

afterEach(() => {
  cleanup();
});

const baseItem = {
  alert_type: "live_trade",
  label: "Live trade fill",
  scope: "user" as const,
  dedupe_key: "evt-1",
  channels: [
    { kind: "push" as const, pref_column: "notify_live_trade", opted_in: true },
  ],
  attempted: true,
};

describe("RecentAlertsPanel quiet-hours annotation (task #629)", () => {
  it("annotates rows that landed inside the saved quiet-hours window with the zone offset", () => {
    // 2026-04-26 04:00 UTC -> 00:00 in America/New_York (EDT) — inside
    // the 22 -> 7 overnight window.
    const item = {
      ...baseItem,
      sent_at: "2026-04-26T04:00:00Z",
    };
    render(
      <RecentAlertsPanel
        items={[item]}
        loading={false}
        error={null}
        onReload={vi.fn()}
        quiet={{
          start_hour: 22,
          end_hour: 7,
          zone: "America/New_York",
        }}
      />,
    );
    const note = screen.getByTestId("history-quiet-0");
    expect(note.textContent).toContain("Muted by quiet hours");
    expect(note.textContent).toContain("America/New_York");
    expect(note.textContent).toMatch(/GMT/);
  });

  it("does not annotate rows that landed outside the quiet-hours window", () => {
    // 18:30 UTC -> 14:30 NYC, well outside [22, 7).
    const item = {
      ...baseItem,
      sent_at: "2026-04-26T18:30:00Z",
    };
    render(
      <RecentAlertsPanel
        items={[item]}
        loading={false}
        error={null}
        onReload={vi.fn()}
        quiet={{
          start_hour: 22,
          end_hour: 7,
          zone: "America/New_York",
        }}
      />,
    );
    expect(screen.queryByTestId("history-quiet-0")).toBeNull();
  });

  it("skips annotation when no quiet-hours zone is configured", () => {
    const item = {
      ...baseItem,
      sent_at: "2026-04-26T04:00:00Z",
    };
    render(
      <RecentAlertsPanel
        items={[item]}
        loading={false}
        error={null}
        onReload={vi.fn()}
        quiet={{ start_hour: null, end_hour: null, zone: null }}
      />,
    );
    expect(screen.queryByTestId("history-quiet-0")).toBeNull();
  });
});
