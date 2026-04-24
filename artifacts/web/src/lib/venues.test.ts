import { describe, it, expect } from "vitest";
import {
  extractVenuesFromEvent,
  normalizeVenue,
  venueDisplayLabel,
} from "./venues";

describe("normalizeVenue", () => {
  it("returns null for empty/missing values", () => {
    expect(normalizeVenue(null)).toBeNull();
    expect(normalizeVenue(undefined)).toBeNull();
    expect(normalizeVenue("")).toBeNull();
    expect(normalizeVenue("   ")).toBeNull();
  });

  it("lower-cases and matches known venues", () => {
    expect(normalizeVenue("kalshi")).toBe("kalshi");
    expect(normalizeVenue("Kalshi")).toBe("kalshi");
    expect(normalizeVenue("POLYMARKET")).toBe("polymarket");
    expect(normalizeVenue("  polymarket  ")).toBe("polymarket");
  });

  it("buckets unknown venues as 'other' (no silent drop)", () => {
    expect(normalizeVenue("predictit")).toBe("other");
    expect(normalizeVenue("manifold")).toBe("other");
  });
});

describe("venueDisplayLabel", () => {
  it("renders Kalshi/Polymarket capitalised, others uppercased, missing as em-dash", () => {
    expect(venueDisplayLabel("kalshi")).toBe("Kalshi");
    expect(venueDisplayLabel("polymarket")).toBe("Polymarket");
    expect(venueDisplayLabel("predictit")).toBe("PREDICTIT");
    expect(venueDisplayLabel(null)).toBe("—");
    expect(venueDisplayLabel("")).toBe("—");
  });
});

describe("extractVenuesFromEvent", () => {
  it("returns [] for null / non-object / empty event", () => {
    expect(extractVenuesFromEvent(null)).toEqual([]);
    expect(extractVenuesFromEvent(undefined)).toEqual([]);
    expect(extractVenuesFromEvent({})).toEqual([]);
  });

  it("reads a single legacy `placement.exchange`", () => {
    expect(
      extractVenuesFromEvent({
        type: "candidate_complete",
        outcome: "placed",
        placement: { exchange: "kalshi" },
      }),
    ).toEqual(["kalshi"]);
  });

  it("reads a `placements` fan-out array, dedup against placement", () => {
    // `cycle.py` emits both `placement` (the primary placement, for
    // backwards-compat) and `placements` (the full fan-out list). The
    // helper must NOT report the primary venue twice.
    const venues = extractVenuesFromEvent({
      type: "candidate_complete",
      outcome: "placed",
      placement: { exchange: "kalshi" },
      placements: [
        { exchange: "kalshi" },
        { exchange: "polymarket" },
      ],
    });
    expect(venues).toEqual(["kalshi", "polymarket"]);
  });

  it("reads a flat `venues` array and treats it as primary-first", () => {
    const venues = extractVenuesFromEvent({
      type: "candidate_complete",
      outcome: "placed",
      venues: ["polymarket", "kalshi"],
    });
    expect(venues).toEqual(["polymarket", "kalshi"]);
  });

  it("normalises case and trims whitespace before deduping", () => {
    const venues = extractVenuesFromEvent({
      venues: [" KALSHI ", "kalshi", "Polymarket"],
    });
    expect(venues).toEqual(["kalshi", "polymarket"]);
  });

  it("preserves an unknown venue (so 'other' bucket can surface it)", () => {
    const venues = extractVenuesFromEvent({
      placements: [{ exchange: "predictit" }, { exchange: "kalshi" }],
    });
    expect(venues).toEqual(["predictit", "kalshi"]);
  });

  it("ignores non-string / malformed entries without throwing", () => {
    const venues = extractVenuesFromEvent({
      venues: [null, 42, { exchange: "kalshi" }, "polymarket"],
      placements: [null, "kalshi", { exchange: 7 }, { exchange: "polymarket" }],
      placement: { exchange: undefined },
    });
    expect(venues).toEqual(["polymarket"]);
  });
});
