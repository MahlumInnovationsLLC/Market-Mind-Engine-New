/**
 * Unit tests for `useSelectedVerticals` — the URL-backed multi-select
 * that drives every dashboard panel's "filter by vertical" affordance.
 *
 * The standalone hook exported here doesn't need React Query or the
 * generated client (only `useVerticalFilter` does), so it can be
 * exercised end-to-end with `renderHook` + manual URL mutations.
 *
 * Why this matters: every dashboard tab — Highlights, KPI rollups,
 * Active Positions, Pipeline tracker — calls `matches(row.vertical)`
 * to decide whether to render. A regression in the URL parsing or
 * the cross-tab sync (the module-level event bus) silently breaks
 * filtering across the whole operator console.
 */
import { act, renderHook } from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

// `useVerticalFilter` is the composite hook that pulls in
// `useGetMyAccess` / `useGetVerticalVisibility` / `useGetVerticalsOverview`
// from the generated client.  Mock them here with a mutable state bag so
// each `effectiveVerticals` test can dial in admin/hidden/registry
// independently without spinning up React Query or a real network.
const { compositeState } = vi.hoisted(() => ({
  compositeState: {
    isAdmin: false,
    hidden: [] as string[],
    registry: [] as string[],
  },
}));

vi.mock("@workspace/api-client-react", () => ({
  getGetMyAccessQueryKey: () => ["my-access"],
  getGetVerticalVisibilityQueryKey: () => ["vertical-visibility"],
  getGetVerticalsOverviewQueryKey: () => ["verticals-overview"],
  useGetMyAccess: () => ({ data: { is_admin: compositeState.isAdmin } }),
  useGetVerticalVisibility: () => ({
    data: { hidden: compositeState.hidden, pinned: [] },
  }),
  useGetVerticalsOverview: () => ({
    data: {
      verticals: compositeState.registry.map((v) => ({
        vertical: v,
        trades: 0,
      })),
    },
  }),
}));

import {
  useSelectedVerticals,
  useVerticalFilter,
} from "./use-selected-verticals";

// jsdom defaults the document origin to `http://localhost:3000` and
// rejects `replaceState` to a different origin. Stick to a relative
// path so the URL stays inside the same origin and the History API
// will accept the update.
const HOME = "/dashboard";

beforeEach(() => {
  window.history.replaceState(null, "", HOME);
  compositeState.isAdmin = false;
  compositeState.hidden = [];
  compositeState.registry = [];
});

afterEach(() => {
  window.history.replaceState(null, "", HOME);
});

function currentParam(): string | null {
  return new URL(window.location.href).searchParams.get("selectedVerticals");
}

describe("useSelectedVerticals - initial read from URL", () => {
  it("returns an empty selection when the param is absent", () => {
    const { result } = renderHook(() => useSelectedVerticals());
    expect(result.current.selected).toEqual([]);
    expect(result.current.matches("sports")).toBe(true);
  });

  it("parses a comma-separated, lower-cased list from the URL", () => {
    window.history.replaceState(
      null,
      "",
      `${HOME}?selectedVerticals=Sports,WEATHER`,
    );
    const { result } = renderHook(() => useSelectedVerticals());
    expect(result.current.selected).toEqual(["sports", "weather"]);
  });

  it("trims whitespace and drops empty entries", () => {
    window.history.replaceState(
      null,
      "",
      `${HOME}?selectedVerticals= sports , , weather `,
    );
    const { result } = renderHook(() => useSelectedVerticals());
    expect(result.current.selected).toEqual(["sports", "weather"]);
  });
});

describe("useSelectedVerticals - mutators write back to the URL", () => {
  it("toggle() adds a vertical the user hasn't selected yet", () => {
    const { result } = renderHook(() => useSelectedVerticals());
    act(() => {
      result.current.toggle("Sports");
    });
    expect(result.current.selected).toEqual(["sports"]);
    expect(currentParam()).toBe("sports");
  });

  it("toggle() removes a vertical that is already selected (case-insensitive)", () => {
    window.history.replaceState(
      null,
      "",
      `${HOME}?selectedVerticals=sports,weather`,
    );
    const { result } = renderHook(() => useSelectedVerticals());
    act(() => {
      result.current.toggle("WEATHER");
    });
    expect(result.current.selected).toEqual(["sports"]);
    expect(currentParam()).toBe("sports");
  });

  it("setAll() replaces the selection and dedupes case-insensitively", () => {
    const { result } = renderHook(() => useSelectedVerticals());
    act(() => {
      result.current.setAll(["Sports", "sports", "WEATHER"]);
    });
    expect(new Set(result.current.selected)).toEqual(
      new Set(["sports", "weather"]),
    );
  });

  it("clear() empties the selection and removes the URL param entirely", () => {
    window.history.replaceState(
      null,
      "",
      `${HOME}?selectedVerticals=sports,weather`,
    );
    const { result } = renderHook(() => useSelectedVerticals());
    act(() => {
      result.current.clear();
    });
    expect(result.current.selected).toEqual([]);
    expect(currentParam()).toBeNull();
  });
});

describe("useSelectedVerticals - matches() predicate", () => {
  it("returns true for everything when nothing is selected (the 'show all' case)", () => {
    const { result } = renderHook(() => useSelectedVerticals());
    expect(result.current.matches("sports")).toBe(true);
    expect(result.current.matches("anything")).toBe(true);
    expect(result.current.matches(null)).toBe(true);
    expect(result.current.matches(undefined)).toBe(true);
  });

  it("returns true only for selected verticals when a filter is active", () => {
    window.history.replaceState(
      null,
      "",
      `${HOME}?selectedVerticals=sports`,
    );
    const { result } = renderHook(() => useSelectedVerticals());
    expect(result.current.matches("sports")).toBe(true);
    expect(result.current.matches("Sports")).toBe(true);
    expect(result.current.matches("weather")).toBe(false);
  });

  it("returns false for null/undefined verticals when a filter is active", () => {
    window.history.replaceState(
      null,
      "",
      `${HOME}?selectedVerticals=sports`,
    );
    const { result } = renderHook(() => useSelectedVerticals());
    expect(result.current.matches(null)).toBe(false);
    expect(result.current.matches(undefined)).toBe(false);
  });

  it("isSelected() is case-insensitive", () => {
    window.history.replaceState(
      null,
      "",
      `${HOME}?selectedVerticals=sports`,
    );
    const { result } = renderHook(() => useSelectedVerticals());
    expect(result.current.isSelected("Sports")).toBe(true);
    expect(result.current.isSelected("WEATHER")).toBe(false);
  });
});

describe("useSelectedVerticals - cross-tab sync", () => {
  it("reacts to popstate (browser back/forward) updates", () => {
    const { result } = renderHook(() => useSelectedVerticals());
    expect(result.current.selected).toEqual([]);

    act(() => {
      window.history.replaceState(
        null,
        "",
        `${HOME}?selectedVerticals=sports`,
      );
      window.dispatchEvent(new Event("popstate"));
    });
    expect(result.current.selected).toEqual(["sports"]);
  });

  it("re-reads the URL when a sibling instance dispatches the change event", () => {
    const a = renderHook(() => useSelectedVerticals());
    const b = renderHook(() => useSelectedVerticals());
    act(() => {
      a.result.current.toggle("sports");
    });
    expect(a.result.current.selected).toEqual(["sports"]);
    expect(b.result.current.selected).toEqual(["sports"]);
  });
});

describe("useVerticalFilter - effectiveVerticals (server-side filter list)", () => {
  it("returns [] (no narrowing) for an admin with no chip selection", () => {
    compositeState.isAdmin = true;
    compositeState.hidden = ["weather"];
    compositeState.registry = ["sports", "crypto", "weather"];
    const { result } = renderHook(() => useVerticalFilter());
    expect(result.current.effectiveVerticals).toEqual([]);
    expect(result.current.filterActive).toBe(false);
    expect(result.current.hiddenActive).toBe(false);
  });

  it("returns [] (no narrowing) for a non-admin with no chip selection AND no hidden", () => {
    compositeState.isAdmin = false;
    compositeState.hidden = [];
    compositeState.registry = ["sports", "crypto"];
    const { result } = renderHook(() => useVerticalFilter());
    expect(result.current.effectiveVerticals).toEqual([]);
    expect(result.current.filterActive).toBe(false);
  });

  it("narrows to the visible registry minus hidden for a non-admin with no chip selection", () => {
    // The fix the architect flagged: previously the headline equity
    // curve omitted the `vertical=` param in this case, so the server
    // returned the all-vertical sum that *included* hidden P&L —
    // visibly drifting from the per-vertical breakdown table that
    // already excluded hidden rows client-side.
    compositeState.isAdmin = false;
    compositeState.hidden = ["weather"];
    compositeState.registry = ["sports", "crypto", "weather"];
    const { result } = renderHook(() => useVerticalFilter());
    expect(result.current.effectiveVerticals.sort()).toEqual([
      "crypto",
      "sports",
    ]);
    expect(result.current.hiddenActive).toBe(true);
    expect(result.current.filterActive).toBe(true);
  });

  it("honours an explicit chip selection (admin path)", () => {
    compositeState.isAdmin = true;
    compositeState.hidden = ["weather"];
    compositeState.registry = ["sports", "crypto", "weather"];
    window.history.replaceState(null, "", `${HOME}?selectedVerticals=sports`);
    const { result } = renderHook(() => useVerticalFilter());
    expect(result.current.effectiveVerticals).toEqual(["sports"]);
    expect(result.current.filterActive).toBe(true);
  });

  it("strips a hidden vertical from an explicit selection for non-admins (defends against stale URLs)", () => {
    // A non-admin can't normally pick a hidden chip from the picker,
    // but a stale URL or a copy-pasted link could carry one in.  We
    // refuse to send it server-side so a hidden vertical never re-
    // appears in the headline curve via the back door.
    compositeState.isAdmin = false;
    compositeState.hidden = ["weather"];
    compositeState.registry = ["sports", "crypto", "weather"];
    window.history.replaceState(
      null,
      "",
      `${HOME}?selectedVerticals=sports,weather`,
    );
    const { result } = renderHook(() => useVerticalFilter());
    expect(result.current.effectiveVerticals).toEqual(["sports"]);
  });

  it("falls back to [] (no narrowing) when hiddenActive but registry hasn't loaded yet", () => {
    // Documented one-render-frame fallback: the alternative would be
    // to gate the equity query on the registry load, which trades a
    // guaranteed-correct first paint for a UX latency cost.  We accept
    // the brief flash because `VerticalPicker` mounts on the same
    // surface and React Query dedupes the in-flight overview request.
    compositeState.isAdmin = false;
    compositeState.hidden = ["weather"];
    compositeState.registry = []; // overview hasn't landed yet
    const { result } = renderHook(() => useVerticalFilter());
    expect(result.current.effectiveVerticals).toEqual([]);
    expect(result.current.hiddenActive).toBe(true);
  });
});
