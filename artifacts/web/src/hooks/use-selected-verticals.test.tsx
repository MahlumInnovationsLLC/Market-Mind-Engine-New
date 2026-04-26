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
} from "vitest";

import { useSelectedVerticals } from "./use-selected-verticals";

// jsdom defaults the document origin to `http://localhost:3000` and
// rejects `replaceState` to a different origin. Stick to a relative
// path so the URL stays inside the same origin and the History API
// will accept the update.
const HOME = "/dashboard";

beforeEach(() => {
  window.history.replaceState(null, "", HOME);
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
