/**
 * Unit tests for the localStorage-backed "remember which price the
 * visitor wanted before they signed in" helpers.
 *
 * The marketing pricing page calls `setPendingCheckoutPriceId` when an
 * unauthenticated visitor clicks an upgrade tier, then the post-signup
 * landing page reads it back via `getPendingCheckoutPriceId` and
 * resumes the checkout. The wrapper deliberately swallows storage
 * errors (private mode / quota / SSR) so the visible UX never crashes
 * when localStorage is unavailable — those branches are the most
 * important thing this file pins down.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearPendingCheckoutPriceId,
  getPendingCheckoutPriceId,
  setPendingCheckoutPriceId,
} from "./pending-checkout";

const STORAGE_KEY = "axiom:pending_checkout_price_id";

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("setPendingCheckoutPriceId / getPendingCheckoutPriceId", () => {
  it("round-trips a price id through localStorage", () => {
    setPendingCheckoutPriceId("price_pro_monthly");
    expect(getPendingCheckoutPriceId()).toBe("price_pro_monthly");
  });

  it("overwrites the previously stored value", () => {
    setPendingCheckoutPriceId("price_pro_monthly");
    setPendingCheckoutPriceId("price_pro_annual");
    expect(getPendingCheckoutPriceId()).toBe("price_pro_annual");
  });

  it("returns null when nothing has been stored", () => {
    expect(getPendingCheckoutPriceId()).toBeNull();
  });

  it("uses the documented storage key so cross-tab reads keep working", () => {
    // Pin the key explicitly: anything reading the raw localStorage key
    // (e.g. a manual debug recovery, or an integration test in another
    // package) breaks silently if we ever rename it.
    setPendingCheckoutPriceId("price_pro_monthly");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("price_pro_monthly");
  });
});

describe("clearPendingCheckoutPriceId", () => {
  it("removes the stored price id", () => {
    setPendingCheckoutPriceId("price_pro_monthly");
    clearPendingCheckoutPriceId();
    expect(getPendingCheckoutPriceId()).toBeNull();
  });

  it("is a no-op when nothing is stored", () => {
    expect(() => clearPendingCheckoutPriceId()).not.toThrow();
    expect(getPendingCheckoutPriceId()).toBeNull();
  });
});

describe("error swallowing (private mode / quota / SSR fallback)", () => {
  it("does not throw when setItem rejects (e.g. quota exceeded)", () => {
    vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(() => setPendingCheckoutPriceId("price_x")).not.toThrow();
  });

  it("returns null and does not throw when getItem rejects", () => {
    vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(getPendingCheckoutPriceId()).toBeNull();
  });

  it("does not throw when removeItem rejects", () => {
    vi.spyOn(window.localStorage, "removeItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(() => clearPendingCheckoutPriceId()).not.toThrow();
  });
});
