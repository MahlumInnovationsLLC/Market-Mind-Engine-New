/**
 * Unit tests for the pure vertical-selection helpers.
 *
 * The hook (`useSelectedVerticals`) needs React + React Query + the API
 * fetcher to render and is exercised by the Playwright e2e suite. The
 * standalone setters / getters / mutators below are pure-ish (only the
 * module-level singleton is touched) so they're easy to cover here.
 */
import {
  clearSelectedVerticals,
  getSelectedVerticals,
  setSelectedVerticals,
  toggleSelectedVertical,
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
