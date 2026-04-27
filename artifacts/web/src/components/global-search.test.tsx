import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Router } from "wouter";
import { memoryLocation } from "wouter/memory-location";

const { apiState } = vi.hoisted(() => ({
  apiState: {
    result: { data: undefined, isFetching: false } as {
      data: unknown;
      isFetching: boolean;
    },
    access: { data: { is_admin: false } } as {
      data: { is_admin: boolean } | null;
    },
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  useSearchAll: () => apiState.result,
  getSearchAllQueryKey: (params: unknown) => ["search-all", params],
  useGetMyAccess: () => apiState.access,
  getGetMyAccessQueryKey: () => ["get-my-access"],
}));

beforeAll(() => {
  // jsdom hasn't shipped these primitives that cmdk / radix-dialog rely on.
  if (typeof Element.prototype.scrollIntoView !== "function") {
    Element.prototype.scrollIntoView = function () {
      return undefined;
    };
  }
  if (typeof window.PointerEvent === "undefined") {
    // @ts-expect-error -- minimal shim
    window.PointerEvent = class extends MouseEvent {};
  }
  if (typeof Element.prototype.hasPointerCapture !== "function") {
    // @ts-expect-error -- minimal shim
    Element.prototype.hasPointerCapture = () => false;
  }
  if (typeof globalThis.ResizeObserver === "undefined") {
    class RO {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    // @ts-expect-error -- minimal shim
    globalThis.ResizeObserver = RO;
  }
});

import GlobalSearch from "./global-search";

beforeEach(() => {
  apiState.access = { data: { is_admin: false } };
  apiState.result = {
    data: {
      trades: [
        {
          id: 7,
          market_id: "MKT_X",
          market_title: "Sample market",
          side: "yes",
          action: "buy",
          executed_at: "2026-04-24T12:00:00Z",
        },
      ],
      debates: [
        {
          id: 99,
          matchup: "A vs B",
          league: "NBA",
          recommended_action: "BUY",
          consensus_probability: 0.6,
          created_at: "2026-04-24T12:00:00Z",
        },
      ],
      cycles: [
        {
          id: 42,
          vertical: "nba",
          picks_evaluated: 5,
          trades_placed: 1,
          started_at: "2026-04-24T12:00:00Z",
        },
      ],
      users: [
        {
          clerk_user_id: "user_abc|123",
          email: "ops@example.com",
          display_name: "Ops User",
          approved: true,
          is_admin: false,
          last_seen_at: "2026-04-24T12:00:00Z",
        },
      ],
      audit: [
        {
          id: 314,
          action: "set_approved",
          actor_email: "admin@example.com",
          target_email: "ops@example.com",
          created_at: "2026-04-24T12:00:00Z",
        },
      ],
      billing: [
        {
          kind: "event",
          provider: "revenuecat",
          event_id: "evt_abc",
          event_type: "RENEWAL",
          last_failed_at: "2026-04-24T12:00:00Z",
          last_error: "boom: 502",
          attempts: 4,
        },
        {
          kind: "customer",
          provider: "stripe",
          user_id: "user_stripe|1",
          email: "billing@example.com",
          tier: "pro",
          status: "active",
          stripe_customer_id: "cus_xyz",
          updated_at: "2026-04-24T12:00:00Z",
        },
        {
          kind: "customer",
          provider: "revenuecat",
          user_id: null,
          app_user_id: "rc_app_42",
          email: null,
          tier: "elite",
          status: "INITIAL_PURCHASE",
          updated_at: "2026-04-24T12:30:00Z",
        },
      ],
    },
    isFetching: false,
  };
});

afterEach(() => {
  cleanup();
});

function renderSearch(open: boolean = true) {
  const hook = memoryLocation({ path: "/somewhere", record: true });
  const utils = render(
    <Router hook={hook.hook}>
      <GlobalSearch />
    </Router>,
  );
  if (open) {
    // Open the palette via the documented custom event.
    fireEvent(window, new Event("axiom:open-search"));
  }
  return { ...utils, history: hook.history };
}

describe("GlobalSearch navigation targets", () => {
  it("routes trade hits to /trading with a focus param matching the trade row", () => {
    const { history } = renderSearch();
    // Type enough to trigger the debounced query (mock returns immediately).
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "sample" } });

    const item = screen.getByTestId("search-trade-7");
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe("/trading?focus=trade-7");
  });

  it("routes debate hits to /app with a focus param matching the debate row", () => {
    const { history } = renderSearch();
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "team" } });

    const item = screen.getByTestId("search-debate-99");
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe("/app?focus=debate-99");
  });

  it("routes cycle hits to /app with a focus param matching the cycle row", () => {
    const { history } = renderSearch();
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "cycle" } });

    const item = screen.getByTestId("search-cycle-42");
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe("/app?focus=cycle-42");
  });

  it("routes user hits to /system?tab=users with the clerk_user_id encoded in the focus param", () => {
    const { history } = renderSearch();
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "ops" } });

    // Note the clerk id contains a `|` (the standard Clerk dev-mode
    // separator) so we expect the focus value to be URL-encoded.
    const item = screen.getByTestId("search-user-user_abc|123");
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe(
      `/system?tab=users&focus=${encodeURIComponent("user-user_abc|123")}`,
    );
  });

  it("routes audit hits to /system?tab=audit with a focus param matching the audit row", () => {
    const { history } = renderSearch();
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "set_approved" } });

    const item = screen.getByTestId("search-audit-314");
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe(
      "/system?tab=audit&focus=audit-314",
    );
  });

  it("routes billing event hits to /system?tab=controls with the rc-event focus param", () => {
    const { history } = renderSearch();
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "evt" } });

    const item = screen.getByTestId("search-billing-event-evt_abc");
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe(
      `/system?tab=controls&focus=${encodeURIComponent("rc-event-evt_abc")}`,
    );
  });

  it("routes billing customer hits with a known clerk id to /system?tab=users with the user focus param", () => {
    const { history } = renderSearch();
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "billing" } });

    // Stripe customer carries a clerk-style user_id with a `|` (the
    // standard Clerk dev-mode separator) — focus value must be encoded.
    const item = screen.getByTestId(
      "search-billing-customer-stripe-user_stripe|1",
    );
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe(
      `/system?tab=users&focus=${encodeURIComponent("user-user_stripe|1")}`,
    );
  });

  it("routes anonymous RC customer hits (no clerk id) to /system?tab=settings", () => {
    const { history } = renderSearch();
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value: "rc_app" } });

    // RC mobile-only customer has app_user_id but no clerk user id —
    // there's nothing to highlight on /system?tab=users, so the
    // palette routes to the settings tab so the operator can look the
    // customer up by hand.
    const item = screen.getByTestId(
      "search-billing-customer-revenuecat-rc_app_42",
    );
    fireEvent.click(item);

    expect(history[history.length - 1]).toBe("/system?tab=settings");
  });
});

describe("GlobalSearch admin-only hint", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    apiState.result = {
      data: {
        trades: [],
        debates: [],
        cycles: [],
        users: [],
        audit: [],
      },
      isFetching: false,
    };
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function typeQuery(value: string) {
    const input = screen.getByTestId("input-global-search");
    fireEvent.change(input, { target: { value } });
    // Push past the 200ms debounce inside GlobalSearch so the empty branch
    // (which is gated on `debounced.length >= 2`) actually renders.
    act(() => {
      vi.advanceTimersByTime(250);
    });
  }

  it("shows the admin-only hint to non-admins when an email-shaped query returns nothing", () => {
    renderSearch();
    typeQuery("ops@example.com");

    const hint = screen.getByTestId("search-admin-only-hint");
    expect(hint.textContent ?? "").toMatch(/admin-only/i);
  });

  it("shows the admin-only hint when the query starts with a known audit-action prefix", () => {
    renderSearch();
    typeQuery("set_admin");

    expect(screen.getByTestId("search-admin-only-hint")).toBeTruthy();
  });

  it("does not show the hint for a generic empty query", () => {
    renderSearch();
    typeQuery("zzzz");

    expect(screen.queryByTestId("search-admin-only-hint")).toBeNull();
  });

  it("does not show the hint to admin callers even when they get no results", () => {
    apiState.access = { data: { is_admin: true } };
    renderSearch();
    typeQuery("ops@example.com");

    expect(screen.queryByTestId("search-admin-only-hint")).toBeNull();
  });

  it("suppresses the hint while the access record is still loading", () => {
    // Mimic the in-flight state of useGetMyAccess before any data arrives
    // — admins shouldn't briefly see a misleading "admin-only" note.
    apiState.access = { data: null };
    renderSearch();
    typeQuery("ops@example.com");

    expect(screen.queryByTestId("search-admin-only-hint")).toBeNull();
  });
});
